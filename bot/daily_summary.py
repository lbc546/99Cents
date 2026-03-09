"""Daily summary logger — fires at midnight UTC.

Logs a comprehensive daily report with P&L, trades, alerts, and settlement
stats. Writes to both the bot log (JSON) and a standalone daily summary file.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from bot.config import BotConfig
from bot.logger import log_event

logger = logging.getLogger("arb_bot")


class DailySummary:
    """Generates and logs a daily summary at midnight UTC."""

    def __init__(self, config: BotConfig):
        self.config = config
        self.risk_manager = None
        self.settlement_tracker = None
        self.order_manager = None

    async def run_daily_summary(self) -> None:
        """Async task: wait until midnight UTC, log summary, repeat."""
        logger.info("Daily summary task started")

        while True:
            # Calculate seconds until next midnight UTC
            now = datetime.now(timezone.utc)
            tomorrow = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            if tomorrow <= now:
                # Already past midnight, target next day
                tomorrow = tomorrow.replace(day=tomorrow.day + 1)

            wait_seconds = (tomorrow - now).total_seconds()
            logger.info("Daily summary scheduled in %.0f seconds", wait_seconds)

            await asyncio.sleep(wait_seconds)

            try:
                self._generate_and_log()
            except Exception:
                logger.exception("Error generating daily summary")

    def _generate_and_log(self) -> None:
        """Build and log the daily summary."""
        yesterday = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Collect data
        risk_stats = self.risk_manager.get_daily_stats() if self.risk_manager else {}
        sett_stats = (self.settlement_tracker.get_settlement_stats()
                      if self.settlement_tracker else {})
        order_summary = self.order_manager.get_summary() if self.order_manager else {}

        # Build summary text
        lines = [
            "=" * 60,
            "DAILY SUMMARY — %s" % yesterday,
            "=" * 60,
            "",
            "P&L:",
            "  Gross profit:  $%.2f" % risk_stats.get("gross_profit", 0),
            "  Gas costs:    -$%.2f" % risk_stats.get("gas_costs", 0),
            "  Net profit:    $%.2f" % risk_stats.get("net_profit", 0),
            "",
            "Opportunities:",
            "  Seen:     %d" % risk_stats.get("opportunities_seen", 0),
            "  Filtered: %d" % risk_stats.get("opportunities_filtered", 0),
            "  Traded:   %d" % risk_stats.get("opportunities_traded", 0),
            "",
            "Settlements:",
            "  Total logged: %d" % sett_stats.get("total_trades_logged", 0),
            "  Flagged:      %d" % sett_stats.get("flagged_trades", 0),
            "  Pending:      %d" % sett_stats.get("pending_resolutions", 0),
            "",
            "Capital:",
            "  Deployed:     $%.0f" % order_summary.get("total_deployed", 0),
            "  Open pos:     %d" % order_summary.get("open_positions", 0),
            "  Utilization:  %.1f%%" % (
                risk_stats.get("capital_utilization_rate", 0) * 100),
            "",
            "Alerts:",
        ]

        # Circuit breaker
        if risk_stats.get("circuit_breaker_open"):
            lines.append("  CIRCUIT BREAKER OPEN: %s" %
                         risk_stats.get("circuit_breaker_reason", ""))
        else:
            lines.append("  Circuit breaker: OK")

        # Consecutive failures
        failures = risk_stats.get("consecutive_failures", 0)
        if failures > 0:
            lines.append("  Consecutive failures: %d" % failures)

        # WebSocket
        if not risk_stats.get("ws_connected", True):
            lines.append("  WebSocket: DISCONNECTED")

        # Category breakdown
        by_cat = sett_stats.get("by_category", {})
        if by_cat:
            lines.append("")
            lines.append("Category Breakdown:")
            for cat, data in sorted(by_cat.items()):
                lines.append("  %s: %d trades, avg settle %.1fh, velocity %.2f" % (
                    cat, data["trade_count"],
                    data["avg_settlement_hours"],
                    data["capital_velocity"]))

        lines.append("=" * 60)

        summary_text = "\n".join(lines)

        # Log as structured event
        log_event(logger, "DAILY_SUMMARY", summary_text,
                  details={
                      "date": yesterday,
                      "risk_stats": risk_stats,
                      "settlement_stats": sett_stats,
                      "order_summary": order_summary,
                  })

        # Write to standalone file
        summary_dir = os.path.dirname(self.config.log_file) or "."
        summary_path = os.path.join(summary_dir, "daily_summary_%s.log" % yesterday)
        try:
            with open(summary_path, "w") as f:
                f.write(summary_text + "\n")
            logger.info("Daily summary written to %s", summary_path)
        except Exception:
            logger.exception("Failed to write daily summary file")
