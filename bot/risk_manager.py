"""Risk management module: blacklist, circuit breakers, daily tracking.

Layers on top of existing order-level controls (position limits, profitability
gate) with system-level protections based on data analysis risk findings:
  - 1.5% dispute rate -> full position loss
  - 33% surprise wins -> high false positive risk (handled by filters.py)
  - 4.8hr median settlement -> slow capital velocity
  - Declining opportunity trend (27->13/day) -> capital preservation matters

Blacklist TTL policy:
  - dispute_detected -> permanent (disputes = fundamental market issues)
  - settlement_timeout -> 7-day expiry (market may eventually settle)
  - manual -> permanent (user explicitly chose to blacklist)

Circuit breakers:
  - Dispute on open position -> immediate pause + blacklist
  - 3 consecutive failed transactions -> pause
  - Daily loss > $15 -> pause
  - WebSocket disconnected -> pause (resume on reconnect)
  - Capital floor breached ($100) -> pause

UMA dispute detection safety:
  - Only check positions >2.5hrs old (UMA challenge window must expire first)
  - Empty dispute fields are normal during first 2hrs post-resolution
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from bot.config import BotConfig
from bot.logger import log_event

logger = logging.getLogger("arb_bot")


class RiskManager:
    """System-level risk management: blacklist, circuit breakers, daily stats."""

    def __init__(self, config: BotConfig, blacklist_path: str = "data/blacklist.json"):
        self.config = config
        self._blacklist_path = blacklist_path
        self._blacklist: dict[str, dict] = {}

        # Circuit breaker state
        self._circuit_open: bool = False
        self._circuit_reason: str = ""
        self._ws_connected: bool = True

        # Daily tracking (reset at midnight UTC)
        self._daily_reset_date: str = ""
        self._daily_opps_seen: int = 0
        self._daily_opps_filtered: int = 0
        self._daily_opps_traded: int = 0
        self._daily_gross_profit: float = 0.0
        self._daily_gas_costs: float = 0.0
        self._daily_net_profit: float = 0.0
        self._daily_fill_sizes: list[float] = []
        self._daily_capital_samples: list[float] = []

        # Consecutive failure tracking
        self._consecutive_failures: int = 0

        # Settlement tracker reference (set by main.py)
        self.settlement_tracker = None

        # Dry-run reporter reference (set by main.py)
        self.dry_run_reporter = None

    # ------------------------------------------------------------------
    # Blacklist
    # ------------------------------------------------------------------

    async def load_blacklist(self) -> None:
        """Load blacklist from JSON file and merge manual config entries."""
        try:
            data = await asyncio.to_thread(self._read_blacklist_sync)
            self._blacklist = data.get("markets", {})
        except FileNotFoundError:
            self._blacklist = {}
        except Exception:
            logger.exception("Failed to load blacklist from %s", self._blacklist_path)
            self._blacklist = {}

        # Merge manual entries from config
        for market_id in self.config.blacklist_manual:
            if not isinstance(market_id, str) or not market_id.strip():
                continue
            mid = market_id.strip()
            # Validate format: warn if suspicious
            if len(mid) < 5:
                logger.warning("Suspicious manual blacklist entry (too short): %s", mid)
            if mid not in self._blacklist:
                self._blacklist[mid] = {
                    "reason": "manual",
                    "added_at": datetime.now(timezone.utc).isoformat(),
                    "question": "",
                    "source": "config",
                }

        # Clean expired entries
        self._clean_expired()

        logger.info("Blacklist loaded: %d entries (%d manual)",
                     len(self._blacklist), len(self.config.blacklist_manual))

    def _read_blacklist_sync(self) -> dict:
        """Synchronous file read for asyncio.to_thread."""
        with open(self._blacklist_path, "r") as f:
            return json.load(f)

    async def _save_blacklist(self) -> None:
        """Persist blacklist to disk atomically."""
        self._clean_expired()
        try:
            await asyncio.to_thread(self._save_blacklist_sync)
        except Exception:
            logger.exception("Failed to save blacklist")

    def _save_blacklist_sync(self) -> None:
        """Synchronous atomic write."""
        data = {"markets": dict(self._blacklist)}
        tmp_path = self._blacklist_path + ".tmp"
        os.makedirs(os.path.dirname(self._blacklist_path) or ".", exist_ok=True)
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self._blacklist_path)

    def _clean_expired(self) -> None:
        """Remove expired blacklist entries in-place."""
        now = datetime.now(timezone.utc)
        expired = []
        for mid, entry in self._blacklist.items():
            expires_at = entry.get("expires_at")
            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(expires_at)
                    if exp_dt < now:
                        expired.append(mid)
                except (ValueError, TypeError):
                    pass
        for mid in expired:
            del self._blacklist[mid]

    async def blacklist_market(self, market_id: str, reason: str,
                                question: str = "", source: str = "") -> None:
        """Add a market to the blacklist and persist."""
        now = datetime.now(timezone.utc)
        entry = {
            "reason": reason,
            "added_at": now.isoformat(),
            "question": question,
            "source": source,
        }

        # TTL policy: settlement_timeout gets 7-day expiry, others permanent
        if reason == "settlement_timeout":
            entry["expires_at"] = (now + timedelta(days=7)).isoformat()

        self._blacklist[market_id] = entry

        log_event(logger, "MARKET_BLACKLISTED",
                  "Blacklisted: reason=%s source=%s | %s" % (
                      reason, source, question[:50]),
                  market_id=market_id,
                  details=entry)

        asyncio.create_task(self._save_blacklist())

    def is_blacklisted(self, market_id: str) -> bool:
        """Fast in-memory blacklist check. O(1)."""
        entry = self._blacklist.get(market_id)
        if entry is None:
            return False

        # Check expiry
        expires_at = entry.get("expires_at")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at)
                if exp_dt < datetime.now(timezone.utc):
                    return False
            except (ValueError, TypeError):
                pass

        return True

    # ------------------------------------------------------------------
    # Circuit breakers
    # ------------------------------------------------------------------

    def is_trading_allowed(self, available_capital: float) -> tuple[bool, str]:
        """Check all circuit breaker conditions.

        Returns (allowed, reason_if_blocked).
        """
        self._check_daily_reset()

        # 1. Explicit circuit open (dispute, etc.)
        if self._circuit_open:
            return False, self._circuit_reason

        # 2. WebSocket disconnected
        if not self._ws_connected:
            return False, "ws_disconnected"

        # 3. Daily loss limit
        if self._daily_net_profit < -self.config.daily_loss_limit:
            return False, "daily_loss_limit=%.2f" % self._daily_net_profit

        # 4. Consecutive failures
        if self._consecutive_failures >= self.config.consecutive_failure_limit:
            return False, "consecutive_failures=%d" % self._consecutive_failures

        # 5. Capital floor
        if available_capital < self.config.capital_floor:
            return False, "capital_floor_breached=%.2f" % available_capital

        return True, ""

    def set_ws_connected(self, connected: bool) -> None:
        """Called by MarketMonitor on WebSocket connect/disconnect."""
        was_connected = self._ws_connected
        self._ws_connected = connected

        if was_connected and not connected:
            log_event(logger, "CIRCUIT_BREAKER_TRIGGERED",
                      "WebSocket disconnected — trading paused",
                      details={"reason": "ws_disconnected"})
        elif not was_connected and connected:
            log_event(logger, "CIRCUIT_BREAKER_RESET",
                      "WebSocket reconnected — trading resumed",
                      details={"reason": "ws_reconnected"})

    def report_dispute(self, market_id: str, question: str = "") -> None:
        """Dispute detected on an open position. Trip breaker + blacklist."""
        self._circuit_open = True
        self._circuit_reason = "dispute_on_position"

        log_event(logger, "CIRCUIT_BREAKER_TRIGGERED",
                  "Dispute detected — trading paused | %s" % question[:50],
                  level="WARNING",
                  market_id=market_id,
                  details={"reason": "dispute_on_position"})

        asyncio.create_task(
            self.blacklist_market(market_id, "dispute_detected", question, "redeemer")
        )

    def report_transaction_failure(self) -> None:
        """Increment consecutive failure counter. Trip breaker if >= limit."""
        self._consecutive_failures += 1

        if self._consecutive_failures >= self.config.consecutive_failure_limit:
            self._circuit_open = True
            self._circuit_reason = "consecutive_failures=%d" % self._consecutive_failures

            log_event(logger, "CIRCUIT_BREAKER_TRIGGERED",
                      "Consecutive failures: %d — trading paused" % self._consecutive_failures,
                      level="WARNING",
                      details={"reason": "consecutive_failures",
                               "count": self._consecutive_failures})

    def report_transaction_success(self) -> None:
        """Reset consecutive failure counter."""
        self._consecutive_failures = 0

    def reset_circuit_breaker(self, reason: str = "manual") -> None:
        """Manually reset the circuit breaker."""
        self._circuit_open = False
        self._circuit_reason = ""
        self._consecutive_failures = 0

        log_event(logger, "CIRCUIT_BREAKER_RESET",
                  "Circuit breaker reset: %s" % reason,
                  details={"reason": reason})

    # ------------------------------------------------------------------
    # Daily tracking
    # ------------------------------------------------------------------

    def _check_daily_reset(self) -> None:
        """Reset daily counters if date has changed (midnight UTC)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if self._daily_reset_date == today:
            return

        # Log final stats for the old day (if there was one)
        if self._daily_reset_date:
            stats = self._build_stats_dict()
            log_event(logger, "DAILY_STATS_RESET",
                      "Daily reset: %s -> %s" % (self._daily_reset_date, today),
                      details=stats)

        # Zero all counters
        self._daily_reset_date = today
        self._daily_opps_seen = 0
        self._daily_opps_filtered = 0
        self._daily_opps_traded = 0
        self._daily_gross_profit = 0.0
        self._daily_gas_costs = 0.0
        self._daily_net_profit = 0.0
        self._daily_fill_sizes = []
        self._daily_capital_samples = []

    def record_opportunity_seen(self) -> None:
        """Increment daily seen counter."""
        self._check_daily_reset()
        self._daily_opps_seen += 1
        if self.dry_run_reporter:
            self.dry_run_reporter.record_opportunity_seen()

    def record_opportunity_filtered(self) -> None:
        """Increment daily filtered counter."""
        self._check_daily_reset()
        self._daily_opps_filtered += 1

    def record_opportunity_traded(self, fill_size: float) -> None:
        """Record a successful trade."""
        self._check_daily_reset()
        self._daily_opps_traded += 1
        self._daily_fill_sizes.append(fill_size)
        if self.dry_run_reporter:
            self.dry_run_reporter.record_opportunity_traded()

    def record_trade_result(self, net_profit: float, gas_cost: float,
                             deployed: float = 0.0) -> None:
        """Record realized P&L from a settled position.

        Also snapshots capital utilization for accuracy on trade events.
        """
        self._check_daily_reset()
        self._daily_net_profit += net_profit
        self._daily_gas_costs += gas_cost
        self._daily_gross_profit += net_profit + gas_cost
        if self.dry_run_reporter:
            self.dry_run_reporter.record_trade_result(net_profit, gas_cost)

        # Snapshot capital at trade settlement for utilization accuracy
        if deployed > 0:
            self._daily_capital_samples.append(deployed)

        # Check daily loss circuit breaker
        if self._daily_net_profit < -self.config.daily_loss_limit:
            if not self._circuit_open:
                self._circuit_open = True
                self._circuit_reason = "daily_loss_limit=%.2f" % self._daily_net_profit
                log_event(logger, "CIRCUIT_BREAKER_TRIGGERED",
                          "Daily loss limit breached: $%.2f — trading paused" % (
                              self._daily_net_profit),
                          level="WARNING",
                          details={"reason": "daily_loss_limit",
                                   "daily_net_profit": self._daily_net_profit})

    def record_settlement_timeout(self, market_id: str, question: str = "") -> None:
        """Market evicted from watchlist after 48h without settling."""
        asyncio.create_task(
            self.blacklist_market(market_id, "settlement_timeout", question,
                                  "watchlist_eviction")
        )

    def record_capital_snapshot(self, deployed: float) -> None:
        """Record capital utilization sample."""
        self._daily_capital_samples.append(deployed)

    def get_daily_stats(self) -> dict:
        """Return current daily stats."""
        self._check_daily_reset()
        return self._build_stats_dict()

    def _build_stats_dict(self) -> dict:
        """Build the stats dict without triggering reset check."""
        avg_fill = 0.0
        if self._daily_fill_sizes:
            avg_fill = sum(self._daily_fill_sizes) / len(self._daily_fill_sizes)

        avg_utilization = 0.0
        if self._daily_capital_samples and self.config.max_total_deployed > 0:
            avg_deployed = sum(self._daily_capital_samples) / len(self._daily_capital_samples)
            avg_utilization = avg_deployed / self.config.max_total_deployed

        return {
            "date": self._daily_reset_date,
            "opportunities_seen": self._daily_opps_seen,
            "opportunities_filtered": self._daily_opps_filtered,
            "opportunities_traded": self._daily_opps_traded,
            "gross_profit": round(self._daily_gross_profit, 2),
            "gas_costs": round(self._daily_gas_costs, 2),
            "net_profit": round(self._daily_net_profit, 2),
            "avg_fill_size": round(avg_fill, 2),
            "target_fill_size": 35.0,
            "capital_utilization_rate": round(avg_utilization, 3),
            "consecutive_failures": self._consecutive_failures,
            "circuit_breaker_open": self._circuit_open,
            "circuit_breaker_reason": self._circuit_reason,
            "ws_connected": self._ws_connected,
            "blacklist_size": len(self._blacklist),
        }

    # ------------------------------------------------------------------
    # Background stats task
    # ------------------------------------------------------------------

    async def run_periodic_stats(self, order_mgr_summary_fn) -> None:
        """Log daily stats periodically and sample capital utilization."""
        logger.info("Risk stats logger started (interval=%ds)",
                     self.config.stats_log_interval_seconds)

        while True:
            await asyncio.sleep(self.config.stats_log_interval_seconds)

            try:
                # Sample capital utilization
                summary = order_mgr_summary_fn()
                self.record_capital_snapshot(summary.get("total_deployed", 0.0))

                # Log current stats
                stats = self.get_daily_stats()
                log_event(logger, "DAILY_STATS",
                          "opps=%d/%d/%d net=$%.2f util=%.1f%% breaker=%s" % (
                              stats["opportunities_seen"],
                              stats["opportunities_filtered"],
                              stats["opportunities_traded"],
                              stats["net_profit"],
                              stats["capital_utilization_rate"] * 100,
                              "OPEN(%s)" % stats["circuit_breaker_reason"]
                              if stats["circuit_breaker_open"] else "ok"),
                          details=stats)

                # Settlement tracker stats (if wired)
                if self.settlement_tracker:
                    sett_stats = self.settlement_tracker.get_settlement_stats()
                    cat_vel = {
                        k: v["capital_velocity"]
                        for k, v in sett_stats["by_category"].items()
                    }
                    log_event(
                        logger, "SETTLEMENT_STATS",
                        "trades=%d flagged=%d velocity=%s pending=%d" % (
                            sett_stats["total_trades_logged"],
                            sett_stats["flagged_trades"],
                            cat_vel,
                            sett_stats["pending_resolutions"]),
                        details=sett_stats)
            except Exception:
                logger.exception("Error in periodic stats")
