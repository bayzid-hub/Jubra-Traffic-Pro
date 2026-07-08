"""
Jubra Traffic Pro - VPN Chain Manager
Handles multi-hop VPN connections and system-level 
IP rotation fallback.
"""

import asyncio
import subprocess
import logging
import platform

logger = logging.getLogger(__name__)

class VPNChainManager:
    """
    Manages system-level VPN connections (OpenVPN/WireGuard).
    Used as an extra layer of security over proxies.
    """

    def __init__(self, config_path: str = ""):
        self.config_path = config_path
        self.current_vpn = None
        self._os_type = platform.system()

    async def connect(self, config_file: str):
        """Connect to a specific VPN node."""
        logger.info(f"[VPN] Connecting to {config_file}...")
        
        if self._os_type == "Linux":
            # Command for OpenVPN on Linux
            cmd = ["sudo", "openvpn", "--config", config_file, "--daemon"]
            proc = subprocess.Popen(cmd)
            self.current_vpn = proc
            await asyncio.sleep(5) # Wait for handshake
            return True
        
        logger.warning(f"VPN control not implemented for {self._os_type}")
        return False

    async def disconnect(self):
        """Disconnect current VPN."""
        if self._os_type == "Linux":
            subprocess.run(["sudo", "killall", "openvpn"])
            self.current_vpn = None
            logger.info("[VPN] Disconnected")