"""Entry point for the Polymarket Settlement Arbitrage Bot.

Usage:
    python -m bot.main                  # uses config.yaml in current directory
    python -m bot.main path/to/config.yaml
"""

import asyncio
import signal
import sys
import logging

from bot.config import load_config, validate_config
from bot.logger import setup_logging, log_event
from bot.market_monitor import MarketMonitor
from bot.order_manager import OrderManager
from bot.redeemer import Redeemer
from bot.risk_manager import RiskManager
from bot.settlement_tracker import SettlementTracker
from bot.dry_run_report import DryRunReporter
from bot.dashboard import Dashboard
from bot.daily_summary import DailySummary
from bot.preflight import PreflightError, run_preflight

logger = logging.getLogger("arb_bot")


async def main(config_path: str = "config.yaml"):
    """Run the arbitrage bot."""
    # Load and validate config
    config = load_config(config_path)
    setup_logging(config.log_file, config.log_level)

    errors = validate_config(config)
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        sys.exit(1)

    # Pre-flight health checks
    logger.info("Running pre-flight health checks...")
    try:
        await run_preflight(config)
    except PreflightError as e:
        logger.error("Pre-flight failed: %s", e)
        sys.exit(1)

    # Log startup info
    logger.info("=" * 60)
    logger.info("Polymarket Settlement Arbitrage Bot")
    logger.info("=" * 60)
    logger.info("Mode: %s", "DRY RUN (paper trading)" if config.dry_run else "LIVE TRADING")
    logger.info("Price threshold: $%.2f", config.price_threshold)
    logger.info("Max position/market: $%.0f", config.max_position_per_market)
    logger.info("Max total deployed: $%.0f", config.max_total_deployed)
    logger.info("Max open positions: %d", config.max_open_positions)
    logger.info("Blocked categories: %s", ", ".join(config.blocked_categories))
    logger.info("Order TTL: %ds", config.order_ttl_seconds)
    logger.info("Gamma poll interval: %ds", config.polling_interval_seconds)
    logger.info("Watchlist scan interval: %ds", config.watchlist_scan_interval_seconds)
    logger.info("End date grace: %dmin", config.end_date_grace_minutes)
    logger.info("CLOB rate limit: %d req/min", config.clob_rate_limit_per_min)
    logger.info("Gamma rate limit: %d req/min", config.gamma_rate_limit_per_min)
    logger.info("Daily loss limit: $%.0f", config.daily_loss_limit)
    logger.info("Consecutive failure limit: %d", config.consecutive_failure_limit)
    logger.info("RPC resolution poll: %ds (batch=%d)",
                 config.rpc_poll_interval_seconds, config.rpc_poll_batch_size)
    logger.info("CT Framework: %s", config.ct_framework_address)
    logger.info("Settlement thresholds: %s", config.settlement_timeout_hours)
    logger.info("=" * 60)

    # Initialize components
    order_mgr = OrderManager(config)
    order_mgr.sync_wallet_balance()
    order_mgr.sync_open_positions()
    order_mgr.sync_redeemed_positions()
    redeemer = Redeemer(config, order_mgr)
    monitor = MarketMonitor(config, on_opportunity=order_mgr.handle_opportunity)

    # Initialize risk manager and wire to all components
    risk_mgr = RiskManager(config)
    await risk_mgr.load_blacklist()
    order_mgr.risk_manager = risk_mgr
    monitor.risk_manager = risk_mgr
    redeemer.risk_manager = risk_mgr
    logger.info("Risk manager initialized (blacklist=%d entries)",
                 risk_mgr.get_daily_stats()["blacklist_size"])

    # Initialize settlement tracker and wire to all components
    tracker = SettlementTracker(config, order_mgr)
    tracker.risk_manager = risk_mgr
    risk_mgr.settlement_tracker = tracker
    redeemer.settlement_tracker = tracker
    logger.info("Settlement tracker initialized")

    # Initialize dry-run reporter and wire to components
    reporter = DryRunReporter(config)
    reporter.settlement_tracker = tracker
    reporter.order_manager = order_mgr
    risk_mgr.dry_run_reporter = reporter
    monitor.dry_run_reporter = reporter
    logger.info("Dry-run reporter initialized")

    # Initialize daily summary task
    daily_summary = DailySummary(config)
    daily_summary.risk_manager = risk_mgr
    daily_summary.settlement_tracker = tracker
    daily_summary.order_manager = order_mgr
    logger.info("Daily summary task initialized (fires at midnight UTC)")

    # Initialize dashboard (optional)
    dashboard = None
    if config.dashboard_enabled:
        dashboard = Dashboard(config, order_mgr, risk_mgr, tracker, reporter)
        logger.info("Dashboard enabled (refresh=%ds)", config.dashboard_refresh_seconds)

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Run all tasks concurrently
    tasks = [
        asyncio.create_task(monitor.run_gamma_poller(), name="gamma_poller"),
        asyncio.create_task(monitor.run_websocket(), name="websocket"),
        asyncio.create_task(monitor.run_watchlist_scanner(), name="watchlist_scanner"),
        asyncio.create_task(order_mgr.monitor_open_orders(), name="order_monitor"),
        asyncio.create_task(redeemer.check_and_redeem_settled(), name="redeemer"),
        asyncio.create_task(risk_mgr.run_periodic_stats(order_mgr.get_summary), name="risk_stats"),
        asyncio.create_task(tracker.run_rpc_resolution_monitor(), name="rpc_resolution_monitor"),
        asyncio.create_task(tracker.run_clob_settlement_monitor(), name="clob_settlement_monitor"),
        asyncio.create_task(tracker.run_anomaly_monitor(), name="anomaly_monitor"),
        asyncio.create_task(daily_summary.run_daily_summary(), name="daily_summary"),
        asyncio.create_task(shutdown_event.wait(), name="shutdown_wait"),
    ]

    if dashboard:
        tasks.insert(-1, asyncio.create_task(
            dashboard.run_dashboard(), name="dashboard"))

    # Wait for shutdown signal or any task to fail
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Check if a task failed (not the shutdown wait)
    for task in done:
        if task.get_name() != "shutdown_wait" and task.exception():
            logger.error("Task %s failed: %s", task.get_name(), task.exception())

    # Cancel remaining tasks
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Cleanup
    await monitor.close()

    # Final summary
    log_event(logger, "DAILY_STATS_FINAL", "Final daily stats at shutdown",
              details=risk_mgr.get_daily_stats())
    log_event(logger, "SETTLEMENT_STATS_FINAL", "Final settlement stats at shutdown",
              details=tracker.get_settlement_stats())
    summary = order_mgr.get_summary()
    logger.info("Shutdown complete. Final state: %s", summary)

    # Generate dry-run report
    if config.dry_run:
        reporter.save_report()


if __name__ == "__main__":
    config_file = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    asyncio.run(main(config_file))
