"""Local browser proxy bridge.

Chrome cannot reliably pass username/password to authenticated SOCKS5 proxies
through --proxy-server. This bridge exposes a local unauthenticated HTTP proxy
for Chrome and forwards each request through the upstream authenticated SOCKS5
proxy using a direct SOCKS5 handshake. It is intentionally small and scoped to
browser page-load verification.
"""

import asyncio
import logging
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class LocalProxyBridgeError(RuntimeError):
    """Raised when the local bridge cannot start or connect upstream."""


class LocalProxyBridge:
    """Unauthenticated local HTTP proxy -> authenticated upstream SOCKS5.

    Supports HTTPS CONNECT and basic HTTP absolute-form requests. The bridge is
    bound to 127.0.0.1 only, so it is not exposed to the local network.
    """

    def __init__(
        self,
        upstream_url: str,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
        connect_timeout: float = 15.0,
        idle_timeout: float = 45.0,
    ) -> None:
        self.upstream_url = upstream_url
        self.bind_host = bind_host or "127.0.0.1"
        self.bind_port = int(bind_port or 0)
        self.connect_timeout = float(connect_timeout or 15.0)
        self.idle_timeout = float(idle_timeout or 45.0)
        self._server: Optional[asyncio.AbstractServer] = None
        self._port: Optional[int] = None
        self._tasks = set()
        self._closed = False
        self._parsed = urlparse(upstream_url)
        if self._parsed.scheme.lower() not in {"socks5", "socks5h"}:
            raise LocalProxyBridgeError(
                f"LocalProxyBridge only supports SOCKS5 upstreams, got {self._parsed.scheme!r}"
            )
        if not self._parsed.hostname or not self._parsed.port:
            raise LocalProxyBridgeError("Invalid upstream SOCKS5 proxy URL")

    @property
    def port(self) -> int:
        if self._port is None:
            raise LocalProxyBridgeError("Bridge is not started")
        return self._port

    @property
    def proxy_url(self) -> str:
        return f"http://{self.bind_host}:{self.port}"

    @property
    def upstream_masked(self) -> str:
        auth = "***:***@" if self._parsed.username else ""
        return f"{self._parsed.scheme}://{auth}{self._parsed.hostname}:{self._parsed.port}"

    @staticmethod
    def _is_benign_connection_reset(exc: BaseException | None) -> bool:
        if exc is None:
            return False
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            winerror = getattr(exc, "winerror", None)
            return winerror in (64, 10054)
        if isinstance(exc, OSError):
            winerror = getattr(exc, "winerror", None)
            return winerror in (64, 10054)
        return False

    @classmethod
    def _install_windows_reset_exception_filter(cls) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if getattr(loop, "_jtp_local_bridge_reset_filter", False):
            return
        previous_handler = loop.get_exception_handler()

        def handler(loop, context):
            exc = context.get("exception")
            message = str(context.get("message", ""))
            if cls._is_benign_connection_reset(exc):
                logger.debug(
                    "[LocalProxyBridge] Suppressed benign socket close: %s | %s",
                    exc,
                    message,
                )
                return
            if previous_handler is not None:
                previous_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(handler)
        setattr(loop, "_jtp_local_bridge_reset_filter", True)

    async def start(self) -> "LocalProxyBridge":
        self._install_windows_reset_exception_filter()
        if self._server is not None:
            return self
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.bind_host,
            port=self.bind_port,
        )
        sockets = self._server.sockets or []
        if not sockets:
            raise LocalProxyBridgeError("Local bridge failed to bind a port")
        self._port = int(sockets[0].getsockname()[1])
        logger.info(
            "[LocalProxyBridge] Started: %s -> %s",
            self.proxy_url,
            self.upstream_masked,
        )
        return self

    async def close(self) -> None:
        self._closed = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("[LocalProxyBridge] Stopped: %s", self.bind_host)

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task:
            self._tasks.add(task)
        upstream_writer = None
        try:
            request_line = await asyncio.wait_for(
                client_reader.readline(), timeout=self.idle_timeout
            )
            if not request_line:
                return
            method, target, version = self._parse_request_line(request_line)
            headers = await self._read_headers(client_reader)
            if method.upper() == "CONNECT":
                host, port = self._parse_connect_target(target)
                upstream_reader, upstream_writer = await self._open_socks5_connection(host, port)
                client_writer.write(
                    b"HTTP/1.1 200 Connection Established\r\n"
                    b"Proxy-Agent: JTP-LocalProxyBridge\r\n\r\n"
                )
                await client_writer.drain()
                await self._tunnel(client_reader, client_writer, upstream_reader, upstream_writer)
            else:
                host, port, path = self._parse_http_target(target, headers)
                upstream_reader, upstream_writer = await self._open_socks5_connection(host, port)
                upstream_writer.write(
                    f"{method} {path} {version}\r\n".encode("latin-1", errors="ignore")
                )
                upstream_writer.write(self._filter_proxy_headers(headers))
                upstream_writer.write(b"\r\n")
                await upstream_writer.drain()
                await self._pipe(upstream_reader, client_writer)
        except Exception as exc:
            logger.debug("[LocalProxyBridge] Client request failed: %s", exc)
            try:
                client_writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await client_writer.drain()
            except Exception:
                pass
        finally:
            if upstream_writer is not None:
                try:
                    upstream_writer.close()
                    await upstream_writer.wait_closed()
                except Exception:
                    pass
            try:
                client_writer.close()
                await client_writer.wait_closed()
            except Exception:
                pass
            if task:
                self._tasks.discard(task)

    @staticmethod
    def _parse_request_line(line: bytes) -> Tuple[str, str, str]:
        text = line.decode("latin-1", errors="ignore").strip()
        parts = text.split()
        if len(parts) != 3:
            raise LocalProxyBridgeError(f"Invalid proxy request line: {text!r}")
        return parts[0], parts[1], parts[2]

    async def _read_headers(self, reader: asyncio.StreamReader) -> bytes:
        chunks = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=self.idle_timeout)
            if line in {b"\r\n", b"\n", b""}:
                break
            chunks.append(line)
            if sum(len(c) for c in chunks) > 64 * 1024:
                raise LocalProxyBridgeError("Proxy header block too large")
        return b"".join(chunks)

    @staticmethod
    def _parse_connect_target(target: str) -> Tuple[str, int]:
        if ":" not in target:
            return target, 443
        host, port = target.rsplit(":", 1)
        return host.strip("[]"), int(port)

    @staticmethod
    def _parse_http_target(target: str, headers: bytes) -> Tuple[str, int, str]:
        parsed = urlparse(target)
        if parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            return parsed.hostname, port, path
        host = ""
        for raw in headers.splitlines():
            if raw.lower().startswith(b"host:"):
                host = raw.split(b":", 1)[1].decode("latin-1", errors="ignore").strip()
                break
        if not host:
            raise LocalProxyBridgeError("HTTP request missing Host header")
        if ":" in host:
            hostname, port_text = host.rsplit(":", 1)
            return hostname.strip("[]"), int(port_text), target or "/"
        return host, 80, target or "/"

    @staticmethod
    def _filter_proxy_headers(headers: bytes) -> bytes:
        blocked = {b"proxy-connection", b"proxy-authenticate", b"proxy-authorization"}
        output = []
        for line in headers.splitlines():
            name = line.split(b":", 1)[0].strip().lower()
            if name in blocked:
                continue
            output.append(line + b"\r\n")
        return b"".join(output)

    async def _open_socks5_connection(
        self,
        target_host: str,
        target_port: int,
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._parsed.hostname, self._parsed.port),
            timeout=self.connect_timeout,
        )
        try:
            username = self._parsed.username or ""
            password = self._parsed.password or ""
            if username or password:
                writer.write(b"\x05\x01\x02")
            else:
                writer.write(b"\x05\x01\x00")
            await writer.drain()
            resp = await asyncio.wait_for(reader.readexactly(2), timeout=self.connect_timeout)
            if resp[0] != 0x05:
                raise LocalProxyBridgeError("Invalid SOCKS5 greeting response")
            if resp[1] == 0xFF:
                raise LocalProxyBridgeError("SOCKS5 upstream rejected auth methods")
            if resp[1] == 0x02:
                u = username.encode("utf-8")
                p = password.encode("utf-8")
                if len(u) > 255 or len(p) > 255:
                    raise LocalProxyBridgeError("SOCKS5 credentials too long")
                writer.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
                await writer.drain()
                auth_resp = await asyncio.wait_for(
                    reader.readexactly(2), timeout=self.connect_timeout
                )
                if auth_resp != b"\x01\x00":
                    raise LocalProxyBridgeError("SOCKS5 username/password auth failed")
            elif resp[1] != 0x00:
                raise LocalProxyBridgeError(f"Unsupported SOCKS5 auth method: {resp[1]}")

            host_bytes = target_host.encode("idna")
            if len(host_bytes) > 255:
                raise LocalProxyBridgeError("Target host too long for SOCKS5")
            port_bytes = int(target_port).to_bytes(2, "big")
            writer.write(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + port_bytes)
            await writer.drain()
            head = await asyncio.wait_for(reader.readexactly(4), timeout=self.connect_timeout)
            if head[0] != 0x05 or head[1] != 0x00:
                raise LocalProxyBridgeError(f"SOCKS5 connect failed, code={head[1] if head else 'unknown'}")
            atyp = head[3]
            if atyp == 0x01:
                await reader.readexactly(4)
            elif atyp == 0x03:
                ln = await reader.readexactly(1)
                await reader.readexactly(ln[0])
            elif atyp == 0x04:
                await reader.readexactly(16)
            else:
                raise LocalProxyBridgeError("Invalid SOCKS5 bind address type")
            await reader.readexactly(2)
            return reader, writer
        except Exception:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            raise

    async def _tunnel(
        self,
        a_reader: asyncio.StreamReader,
        a_writer: asyncio.StreamWriter,
        b_reader: asyncio.StreamReader,
        b_writer: asyncio.StreamWriter,
    ) -> None:
        tasks = [
            asyncio.create_task(self._pipe(a_reader, b_writer)),
            asyncio.create_task(self._pipe(b_reader, a_writer)),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # A browser normally closes one side of the CONNECT tunnel as soon
            # as a page finishes loading or the browser process exits. Cancel
            # the opposite pipe and consume its exception so asyncio does not
            # emit "Task exception was never retrieved" for normal socket
            # shutdowns on Windows.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        while True:
            try:
                data = await asyncio.wait_for(
                    reader.read(65536),
                    timeout=self.idle_timeout,
                )
            except asyncio.CancelledError:
                return
            except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError, OSError):
                # Normal tunnel shutdown: Chrome/upstream may close idle or
                # finished connections abruptly, especially on Windows where it
                # surfaces as WinError 64/10054. Treat it as a clean close.
                return
            if not data:
                return
            try:
                writer.write(data)
                await writer.drain()
            except asyncio.CancelledError:
                return
            except (ConnectionResetError, BrokenPipeError, OSError):
                return
