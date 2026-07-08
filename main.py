"""
Jubra Traffic Pro - Main Entry Point (nodriver Edition)
"""
import asyncio
import signal
import logging
import argparse
import sys
import threading
from pathlib import Path
from utils.logger import LoggerFactory

ring_buffer = LoggerFactory.setup(
    log_level       = "INFO",
    log_dir         = "logs",
    json_output     = True,
    console_output  = True,
    file_output     = True,
)
logger = logging.getLogger("main")

async def build_application(args: argparse.Namespace) -> dict:
    """Bootstrap all subsystems."""
    from core.config_manager    import ConfigManager
    from core.event_bus         import EventBusRegistry
    from core.session_manager   import SessionManager
    from engines.proxy.proxy_engine     import ProxyEngine, RotationStrategy
    from engines.proxy.proxy_rotator    import ProxyRotator
    from engines.browser.browser_farm   import BrowserFarm
    from engines.fingerprint.fingerprint_engine import FingerprintEngine
    from engines.fingerprint.tls_spoofer       import TLSSpoofer
    from traffic.traffic_orchestrator   import TrafficOrchestrator
    from traffic.organic_traffic        import OrganicTrafficEngine
    from traffic.social_traffic         import SocialTrafficEngine
    from traffic.direct_traffic         import DirectTrafficEngine
    from traffic.referral_traffic       import ReferralTrafficEngine
    from analytics.ga4_simulator        import GA4Simulator
    from security.captcha_solver        import CaptchaSolver
    from security.detection_evader      import DetectionEvader
    from monitoring.self_healing        import SelfHealingEngine
    from monitoring.metrics_collector   import MetricsCollector
    from monitoring.performance_monitor import PerformanceMonitor
    from utils.user_agent_rotator       import UserAgentRotator
    components = {}
    
    # ── 1. Event Bus ─────────────────────────────────────
    logger.info("Starting Event Bus...")
    bus = EventBusRegistry.create(
        name            = "default",
        worker_count    = 8,
        max_queue_size  = 50000,
        enable_dedup    = True,
    )
    await bus.start()
    components["event_bus"] = bus
    
    # ── 2. Config ─────────────────────────────────────────
    logger.info("Loading configuration...")
    config = ConfigManager(
        config_path         = args.config,
        enable_hot_reload   = True,
        event_bus           = bus,
        auto_create         = True,
    )
    config.load()
    await config.start_hot_reload()
    components["config"] = config
    
    # GUI startup should not open any browser/pre-warm window.
    # Browsers will be created lazily only when a real session requests one.
    gui_lazy_browser_startup = bool(getattr(args, "gui", False)) and bool(
        config.get("gui.lazy_browser_startup", True)
    )
    
    # ── 3. Proxy Engine ───────────────────────────────────
    logger.info("Initializing Proxy Engine...")
    proxy_engine = ProxyEngine(
        config              = config,
        event_bus           = bus,
        health_check_interval = config.get(
            "proxy.health_check_interval", 60.0
        ),
    )
    proxy_file = config.get("proxy.pool_file", "data/proxies.txt")
    if Path(proxy_file).exists():
        loaded = await proxy_engine.load_from_file(
            proxy_file, validate=True
        )
        logger.info(f"Loaded {loaded} proxies")
    await proxy_engine.start()
    components["proxy_engine"] = proxy_engine
    
    # ── 4. Browser Farm (nodriver) ────────────────────────
    logger.info("Initializing Browser Farm (nodriver)...")
    browser_pool_size     = config.get("browser.pool_size", 5)
    browser_warmup_count  = config.get("browser.warmup_count", 2)
    browser_min_available = config.get("browser.min_available", 2)
    if gui_lazy_browser_startup:
        browser_warmup_count  = 0
        browser_min_available = 0
        logger.info(
            "GUI lazy startup enabled: browser pre-warm disabled "
            "to prevent an extra blank window at launch."
        )
    browser_farm = BrowserFarm(
        config          = config,
        event_bus       = bus,
        pool_size       = browser_pool_size,
        min_available   = browser_min_available,
        warmup_count    = browser_warmup_count,
        recycle_after   = config.get("browser.recycle_after", 50),
        crash_recovery  = config.get("browser.crash_recovery", True),
    )
    await browser_farm.start()
    components["browser_farm"] = browser_farm
    
    # ── 5. Fingerprint Engine ─────────────────────────────
    logger.info("Initializing Fingerprint Engine...")
    fp_engine = FingerprintEngine(
        config          = config,
        event_bus       = bus,
        mutation_rate   = config.get("fingerprint.mutation_rate", 0.15),
    )
    components["fingerprint_engine"] = fp_engine
    
    # ── 6. Session Manager ────────────────────────────────
    logger.info("Initializing Session Manager...")
    session_manager = SessionManager(
        config              = config,
        event_bus           = bus,
        max_sessions        = config.get("general.max_workers", 10) * 2,
        session_ttl         = config.get("general.session_timeout", 300.0),
        enable_recycling    = True,
    )
    await session_manager.start()
    components["session_manager"] = session_manager
    
    # ── 7. Analytics ──────────────────────────────────────
    logger.info("Initializing Analytics...")
    ga4 = GA4Simulator(
        config          = config,
        event_bus       = bus,
        send_events     = config.get("analytics.ga4_enabled", False),
    )
    components["ga4_simulator"] = ga4
    
    # ── 8. CAPTCHA Solver ─────────────────────────────────
    logger.info("Initializing CAPTCHA Solver...")
    captcha_solver = CaptchaSolver(
        config          = config,
        event_bus       = bus,
        budget_usd      = config.get("security.captcha_budget", 10.0),
    )
    components["captcha_solver"] = captcha_solver
    
    # ── 9. Detection Evader (nodriver) ────────────────────
    logger.info("Initializing Detection Evader...")
    detection_evader = DetectionEvader(
        config      = config,
        event_bus   = bus,
    )
    components["detection_evader"] = detection_evader
    
    # ── 10. Traffic Engines ───────────────────────────────
    logger.info("Initializing Traffic Engines (nodriver)...")
    target_urls     = config.get("traffic.target_urls", [])
    target_domain   = ""
    if target_urls:
        from urllib.parse import urlparse
        target_domain = urlparse(target_urls[0]).netloc
    organic_engine  = OrganicTrafficEngine(
        config          = config,
        event_bus       = bus,
        target_domain   = target_domain,
        target_urls     = target_urls,
        keywords_file   = config.get(
            "traffic.keywords_file", "data/keywords.json"
        ),
    )
    social_engine   = SocialTrafficEngine(
        config          = config,
        event_bus       = bus,
        target_urls     = target_urls,
    )
    direct_engine   = DirectTrafficEngine(
        config          = config,
        event_bus       = bus,
        target_urls     = target_urls,
    )
    referral_engine = ReferralTrafficEngine(
        config          = config,
        event_bus       = bus,
        target_urls     = target_urls,
    )
    components["organic_engine"]    = organic_engine
    components["social_engine"]     = social_engine
    components["direct_engine"]     = direct_engine
    components["referral_engine"]   = referral_engine
    
    # ── 11. Traffic Orchestrator ──────────────────────────
    logger.info("Initializing Traffic Orchestrator...")
    orchestrator = TrafficOrchestrator(
        config              = config,
        session_manager     = session_manager,
        browser_farm        = browser_farm,
        proxy_engine        = proxy_engine,
        fingerprint_engine  = fp_engine,
        event_bus           = bus,
        max_concurrent      = config.get("general.max_workers", 10),
    )
    orchestrator.register_engine("organic",  organic_engine)
    orchestrator.register_engine("social",   social_engine)
    orchestrator.register_engine("direct",   direct_engine)
    orchestrator.register_engine("referral", referral_engine)
    components["traffic_orchestrator"] = orchestrator
    
    # ── 12. Metrics Collector ─────────────────────────────
    logger.info("Initializing Metrics Collector...")
    metrics = MetricsCollector(
        config              = config,
        event_bus           = bus,
        collection_interval = config.get(
            "monitoring.metrics_interval", 5.0
        ),
    )
    await metrics.start()
    components["metrics_collector"] = metrics
    
    # ── 13. Performance Monitor ───────────────────────────
    perf_monitor = PerformanceMonitor(
        config              = config,
        metrics_collector   = metrics,
        sample_interval     = 5.0,
    )
    await perf_monitor.start()
    components["performance_monitor"] = perf_monitor
    
    # ── 14. Self-Healing ──────────────────────────────────
    logger.info("Initializing Self-Healing Engine...")
    healer = SelfHealingEngine(
        config          = config,
        event_bus       = bus,
        check_interval  = 15.0,
    )
    healer.register_component("browser_farm",           browser_farm)
    healer.register_component("proxy_engine",           proxy_engine)
    healer.register_component("session_manager",        session_manager)
    healer.register_component("traffic_orchestrator",   orchestrator)
    await healer.start()
    components["self_healing"] = healer
    
    logger.info(
        f"✅ All {len(components)} subsystems ready! "
        f"(Engine: nodriver)"
    )
    return components

async def run_campaign(
    components: dict,
    args:       argparse.Namespace,
) -> None:
    """Create and run a traffic campaign."""
    config      = components["config"]
    orchestrator = components["traffic_orchestrator"]
    target_urls = (
        args.targets.split(",")
        if args.targets
        else config.get("traffic.target_urls", [])
    )
    if not target_urls:
        logger.error("No target URLs! Use --targets or set in config")
        return
    total   = args.sessions or config.get("traffic.sessions_per_hour", 30)
    rate    = args.rate     or config.get("traffic.sessions_per_hour", 30)
    logger.info(
        f"Creating campaign: {total} sessions "
        f"@ {rate}/hr → {target_urls}"
    )
    campaign = await orchestrator.create_campaign(
        name                = args.campaign_name or "CLI Campaign",
        target_urls         = target_urls,
        total_sessions      = total,
        sessions_per_hour   = rate,
        organic_ratio       = config.get("traffic.organic_ratio", 0.60),
        social_ratio        = config.get("traffic.social_ratio",  0.15),
        direct_ratio        = config.get("traffic.direct_ratio",  0.15),
        referral_ratio      = config.get("traffic.referral_ratio", 0.10),
        bounce_rate         = config.get("traffic.bounce_rate",   0.35),
    )
    await orchestrator.start_campaign(campaign.campaign_id)
    while campaign.is_active and not campaign.is_complete:
        await asyncio.sleep(10.0)
        logger.info(
            f"Progress: {campaign.sessions_launched}/"
            f"{campaign.total_sessions} | "
            f"success={campaign.success_rate:.1%} | "
            f"detected={campaign.sessions_detected}"
        )
    logger.info(
        f"Campaign done: "
        f"✅ {campaign.sessions_completed} | "
        f"❌ {campaign.sessions_failed} | "
        f"🚫 {campaign.sessions_detected} detected"
    )

async def shutdown(components: dict) -> None:
    """Gracefully shutdown all subsystems."""
    logger.info("Shutting down Jubra Traffic Pro...")
    order = [
        "self_healing",
        "traffic_orchestrator",
        "performance_monitor",
        "metrics_collector",
        "session_manager",
        "browser_farm",
        "proxy_engine",
        "event_bus",
    ]
    for name in order:
        comp = components.get(name)
        if not comp:
            continue
        try:
            logger.info(f"Stopping: {name}")
            if hasattr(comp, "stop_all"):
                await comp.stop_all()
            elif hasattr(comp, "stop"):
                await comp.stop()
        except Exception as exc:
            logger.error(f"Error stopping {name}: {exc}")
    config = components.get("config")
    if config:
        await config.stop_hot_reload()
    logger.info("Shutdown complete ✅")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Jubra Traffic Pro (nodriver Edition)",
    )
    parser.add_argument(
        "--config", "-c",
        default="config/default_config.yaml",
    )
    parser.add_argument("--targets", "-t", default=None)
    parser.add_argument("--sessions", "-s", type=int, default=None)
    parser.add_argument("--rate", "-r", type=int, default=None)
    parser.add_argument("--campaign-name", default="Default Campaign")
    parser.add_argument("--gui",      action="store_true")
    parser.add_argument("--debug",    action="store_true")
    parser.add_argument("--dry-run",  action="store_true")
    return parser.parse_args()

def run_gui_in_main_thread(components, ring_buffer, async_loop):
    from gui.main_window import launch_gui
    launch_gui(components, ring_buffer, async_loop)

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

def main():
    args = parse_args()
    
    if len(sys.argv) == 1:
        args.gui = True
        
    if args.debug:
        LoggerFactory.set_level("DEBUG")
        
    logger.info(
        "\n"
        "╔══════════════════════════════════════════╗\n"
        "║         Jubra Traffic Pro v1.0            ║\n"
        "║   Professional Web Analytics Solution     ║\n"
        "╚══════════════════════════════════════════╝"
    )

    exit_code = 0
    background_loop = asyncio.new_event_loop()
    
    async_thread = threading.Thread(
        target=start_background_loop, 
        args=(background_loop,), 
        daemon=True
    )
    async_thread.start()

    components = {}
    try:
        # Initialize components in the background loop
        future = asyncio.run_coroutine_threadsafe(
            build_application(args), 
            background_loop
        )
        components = future.result()

        if args.dry_run:
            logger.info("Dry run complete - all systems OK")
            return 0

        if args.gui:
            try:
                run_gui_in_main_thread(components, ring_buffer, background_loop)
            except ImportError:
                logger.error("PyQt6 not installed. pip install PyQt6")
                return 1
        else:
             # run CLI version in background loop
             future = asyncio.run_coroutine_threadsafe(
                 run_campaign(components, args),
                 background_loop
             )
             future.result()
             
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    except Exception as exc:
        logger.critical(f"Fatal: {exc}", exc_info=True)
        exit_code = 1
    finally:
        if components:
           # shutdown in background loop
           future = asyncio.run_coroutine_threadsafe(
               shutdown(components),
               background_loop
           )
           future.result()
           
        background_loop.call_soon_threadsafe(background_loop.stop)
        async_thread.join()
        
    return exit_code

if __name__ == "__main__":
    sys.exit(main())
