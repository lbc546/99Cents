"""Dry-run simulation report generator.

Session-level accumulator that never resets at midnight (unlike risk_manager
daily stats). Generates a structured comparison of actual simulation results
vs baseline projections after a 48-hour dry run.

Baseline projections (from data analysis):
  - ~20 qualified opportunities/day
  - $4.74/day net profit (base case, $1K capital, $0.98 threshold)
  - 40% capital utilization target

Verdict logic:
  - ABOVE EXPECTATIONS: daily profit > 120% of $4.74 baseline
  - ON TRACK: daily profit >= 80% of baseline AND opps/day >= 15
  - BELOW EXPECTATIONS: otherwise
"""

import logging
import os
import time
from collections import defaultdict

from bot.config import BotConfig
from bot.logger import log_event

logger = logging.getLogger("arb_bot")

# Baseline projections from data analysis
BASELINE_OPPS_PER_DAY = 20.0
BASELINE_PROFIT_PER_DAY = 4.74


class DryRunReporter:
    """Session-level stats accumulator and report generator."""

    def __init__(self, config: BotConfig):
        self.config = config
        self._start_time = time.time()

        # Session-level counters (never reset at midnight)
        self._total_opps_seen: int = 0
        self._total_opps_filtered: int = 0
        self._total_opps_traded: int = 0
        self._total_gross_profit: float = 0.0
        self._total_gas_costs: float = 0.0
        self._total_net_profit: float = 0.0

        # Filter rejection breakdown
        self._filter_reasons: dict[str, int] = defaultdict(int)

        # Track max concurrent positions seen
        self._max_concurrent_positions: int = 0

        # References (set by main.py)
        self.settlement_tracker = None
        self.order_manager = None

    # ------------------------------------------------------------------
    # Event recording (called by risk_manager and market_monitor)
    # ------------------------------------------------------------------

    def record_opportunity_seen(self) -> None:
        """Increment session-level seen counter."""
        self._total_opps_seen += 1

    def record_filter_reason(self, reason: str) -> None:
        """Record a specific filter rejection reason."""
        self._total_opps_filtered += 1
        # Normalize reason to bucket (strip numeric suffixes for aggregation)
        bucket = self._normalize_reason(reason)
        self._filter_reasons[bucket] += 1

    def record_opportunity_traded(self) -> None:
        """Increment session-level traded counter."""
        self._total_opps_traded += 1
        # Track max concurrent positions
        if self.order_manager:
            concurrent = self.order_manager.open_position_count
            if concurrent > self._max_concurrent_positions:
                self._max_concurrent_positions = concurrent

    def record_trade_result(self, net_profit: float, gas_cost: float) -> None:
        """Accumulate session-level P&L."""
        self._total_net_profit += net_profit
        self._total_gas_costs += gas_cost
        self._total_gross_profit += net_profit + gas_cost

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    def generate_report(self) -> str:
        """Build the full dry-run simulation report."""
        elapsed = time.time() - self._start_time
        elapsed_hours = elapsed / 3600
        elapsed_days = elapsed / 86400

        # Avoid division by zero
        if elapsed_days < 0.001:
            elapsed_days = 0.001

        # Settlement tracker stats
        sett_stats = {}
        trades_settled = 0
        if self.settlement_tracker:
            sett_stats = self.settlement_tracker.get_settlement_stats()
            trades_settled = sett_stats.get("total_trades_logged", 0)

        # Rates
        opps_per_day = self._total_opps_seen / elapsed_days
        profit_per_day = self._total_net_profit / elapsed_days
        filter_rate = (
            (self._total_opps_filtered / self._total_opps_seen * 100)
            if self._total_opps_seen > 0 else 0.0
        )
        pass_rate = (
            (self._total_opps_traded / self._total_opps_seen * 100)
            if self._total_opps_seen > 0 else 0.0
        )

        lines = []
        lines.append("=" * 60)
        lines.append("DRY RUN SIMULATION REPORT")
        lines.append(
            "Duration: %.1f hours (%.1f days)" % (elapsed_hours, elapsed_days)
        )
        lines.append("Mode: Paper trading — no real capital deployed")
        lines.append("=" * 60)
        lines.append("")

        # --- Opportunity funnel ---
        lines.append("OPPORTUNITY FUNNEL")
        lines.append(
            "  Opportunities seen:     %d  (%.1f/day vs ~%.0f/day baseline)"
            % (self._total_opps_seen, opps_per_day, BASELINE_OPPS_PER_DAY)
        )
        lines.append(
            "  Opportunities filtered: %d  (%.1f%% filter rate)"
            % (self._total_opps_filtered, filter_rate)
        )
        lines.append(
            "  Opportunities traded:   %d  (%.1f%% pass rate)"
            % (self._total_opps_traded, pass_rate)
        )
        lines.append("  Trades settled:         %d" % trades_settled)
        lines.append("")

        # --- Filter rejection breakdown ---
        lines.append("FILTER REJECTION BREAKDOWN")
        if self._filter_reasons:
            sorted_reasons = sorted(
                self._filter_reasons.items(), key=lambda x: x[1], reverse=True
            )
            for reason, count in sorted_reasons:
                pct = count / self._total_opps_filtered * 100 if self._total_opps_filtered > 0 else 0
                lines.append("  %-30s %4d  (%.1f%%)" % (reason, count, pct))
        else:
            lines.append("  (no filter rejections recorded)")
        lines.append("")

        # --- Simulated P&L ---
        lines.append("SIMULATED P&L")
        lines.append("  Gross profit:  $%.2f" % self._total_gross_profit)
        lines.append("  Gas costs:    -$%.2f" % self._total_gas_costs)
        lines.append(
            "  Net profit:    $%.2f  ($%.2f/day vs $%.2f/day baseline)"
            % (self._total_net_profit, profit_per_day, BASELINE_PROFIT_PER_DAY)
        )
        lines.append("")

        # --- Category breakdown ---
        lines.append("CATEGORY BREAKDOWN")
        by_category = sett_stats.get("by_category", {})
        if by_category:
            for cat, cat_data in sorted(by_category.items()):
                avg_profit = 0.0
                if cat_data["trade_count"] > 0:
                    avg_profit = cat_data["total_redeemed_value"] / cat_data["trade_count"]
                    # Subtract avg cost per trade from avg payout
                    # Approximate: avg_profit ≈ total_redeemed / count - avg_cost
                lines.append(
                    "  %-10s %d trades, avg settlement %.1fh, velocity %.2f"
                    % (
                        cat + ":",
                        cat_data["trade_count"],
                        cat_data["avg_settlement_hours"],
                        cat_data["capital_velocity"],
                    )
                )
        else:
            lines.append("  (no completed trades yet)")
        lines.append("")

        # --- Capital metrics ---
        lines.append("CAPITAL METRICS")
        global_velocity = sett_stats.get("global_capital_velocity", 0.0)
        lines.append("  Max deployed: $%.0f" % self.config.max_total_deployed)
        lines.append(
            "  Max concurrent positions: %d / %d"
            % (self._max_concurrent_positions, self.config.max_open_positions)
        )
        lines.append("  Capital velocity: %.2f turns/day" % global_velocity)
        lines.append("")

        # --- Verdict ---
        verdict, verdict_detail = self._compute_verdict(
            opps_per_day, profit_per_day
        )
        lines.append("VERDICT: %s" % verdict)
        lines.append(
            "  Opportunity rate:  %.1f/day  (%.0f%% of baseline)"
            % (opps_per_day, opps_per_day / BASELINE_OPPS_PER_DAY * 100)
        )
        lines.append(
            "  Daily profit:      $%.2f/day (%.0f%% of $%.2f baseline)"
            % (
                profit_per_day,
                profit_per_day / BASELINE_PROFIT_PER_DAY * 100
                if BASELINE_PROFIT_PER_DAY > 0 else 0,
                BASELINE_PROFIT_PER_DAY,
            )
        )
        if verdict_detail:
            lines.append("  Note: %s" % verdict_detail)
        lines.append("=" * 60)

        return "\n".join(lines)

    def _compute_verdict(
        self, opps_per_day: float, profit_per_day: float
    ) -> tuple[str, str]:
        """Determine verdict based on actual vs baseline performance."""
        profit_pct = profit_per_day / BASELINE_PROFIT_PER_DAY * 100

        if profit_per_day > BASELINE_PROFIT_PER_DAY * 1.2:
            return "ABOVE EXPECTATIONS", "Profit %.0f%% above baseline" % (
                profit_pct - 100
            )

        if profit_per_day >= BASELINE_PROFIT_PER_DAY * 0.8 and opps_per_day >= 15:
            return "ON TRACK", ""

        details = []
        if profit_per_day < BASELINE_PROFIT_PER_DAY * 0.8:
            details.append(
                "profit at %.0f%% of baseline (need >= 80%%)" % profit_pct
            )
        if opps_per_day < 15:
            details.append(
                "only %.1f opps/day (need >= 15)" % opps_per_day
            )
        return "BELOW EXPECTATIONS", "; ".join(details)

    def save_report(self, path: str = "report/dry_run_report.txt") -> None:
        """Generate report, print to console, and save to file."""
        report = self.generate_report()

        # Print to console
        print("\n" + report + "\n")

        # Save to file
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(report + "\n")

        log_event(
            logger,
            "DRY_RUN_REPORT_SAVED",
            "Dry-run report saved to %s" % path,
            details={"path": path, "duration_hours": (time.time() - self._start_time) / 3600},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_reason(reason: str) -> str:
        """Normalize filter reason for aggregation.

        Strips numeric values from reasons like 'low_liquidity_usdc=12.50'
        to bucket as 'low_liquidity_usdc'.
        """
        if "=" in reason:
            parts = reason.split("=", 1)
            # Keep the key, drop the value if it looks numeric
            try:
                float(parts[1])
                return parts[0]
            except (ValueError, IndexError):
                pass
        return reason
