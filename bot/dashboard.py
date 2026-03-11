"""Live terminal dashboard using the rich library.

Displays real-time bot status: capital, positions, PnL, settlement stats,
alerts, and WebSocket connectivity. Refreshes every N seconds.

Runs as an optional async task — controlled by config.dashboard_enabled.
When active, console log level is raised to WARNING to avoid interference
with the rich Live display.
"""

import asyncio
import logging
import time

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bot.config import BotConfig

logger = logging.getLogger("arb_bot")


class Dashboard:
    """Rich terminal dashboard for the arbitrage bot."""

    def __init__(self, config, order_manager, risk_manager,
                 settlement_tracker, dry_run_reporter=None):
        self.config = config
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.settlement_tracker = settlement_tracker
        self.dry_run_reporter = dry_run_reporter
        self._start_time = time.time()
        self._console = Console()

    async def run_dashboard(self):
        """Async task: refresh dashboard in a loop using rich Live."""
        import sys
        if not sys.stdout.isatty():
            logger.info("Dashboard disabled (no TTY)")
            await asyncio.Event().wait()
            return

        from rich.live import Live

        # Suppress console logging so it doesn't interfere with dashboard
        for handler in logging.getLogger("arb_bot").handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                handler.setLevel(logging.WARNING)

        logger.info("Dashboard started (refresh=%ds)", self.config.dashboard_refresh_seconds)

        with Live(
            self._build_layout(),
            console=self._console,
            refresh_per_second=0.5,
            screen=True,
        ) as live:
            while True:
                try:
                    live.update(self._build_layout())
                except Exception:
                    logger.exception("Dashboard render error")
                await asyncio.sleep(self.config.dashboard_refresh_seconds)

    # ------------------------------------------------------------------
    # Main layout builder
    # ------------------------------------------------------------------

    def _build_layout(self) -> Layout:
        """Compose the full dashboard layout."""
        layout = Layout()

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="top_row", size=8),
            Layout(name="positions", size=10),
            Layout(name="trades", size=9),
            Layout(name="bottom_row", size=8),
        )

        # Header
        layout["header"].update(self._build_header())

        # Top row: Capital | Stats | PnL | Velocity
        layout["top_row"].split_row(
            Layout(self._build_capital_panel(), name="capital"),
            Layout(self._build_stats_panel(), name="stats"),
            Layout(self._build_pnl_panel(), name="pnl"),
            Layout(self._build_velocity_panel(), name="velocity"),
        )

        # Middle: Open positions table
        layout["positions"].update(self._build_positions_table())

        # Trades: Last 5 settled
        layout["trades"].update(self._build_recent_trades_table())

        # Bottom row: Category performance | Status/Alerts
        layout["bottom_row"].split_row(
            Layout(self._build_category_panel(), name="category"),
            Layout(self._build_status_panel(), name="status"),
        )

        return layout

    # ------------------------------------------------------------------
    # Panel builders
    # ------------------------------------------------------------------

    def _build_header(self) -> Panel:
        """Title bar with mode and uptime."""
        uptime = time.time() - self._start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)

        mode = "[bold red]DRY RUN[/bold red]" if self.config.dry_run else "[bold green]LIVE[/bold green]"
        title = Text.from_markup(
            "[bold white]POLYMARKET SETTLEMENT ARB BOT[/bold white]  "
            "%s  uptime: %dh %02dm" % (mode, hours, minutes)
        )
        return Panel(title, style="bold blue")

    def _build_capital_panel(self) -> Panel:
        """Wallet balance and deployment status."""
        summary = self.order_manager.get_summary()
        deployed = summary["total_deployed"]
        available = summary["available_capital"]
        max_deploy = self.config.max_total_deployed
        pct = (deployed / max_deploy * 100) if max_deploy > 0 else 0

        # Color the percentage based on utilization
        if pct > 80:
            pct_color = "red"
        elif pct > 50:
            pct_color = "yellow"
        else:
            pct_color = "green"

        text = Text()
        text.append("Deployed: $%.0f / $%.0f " % (deployed, max_deploy))
        text.append("(%.0f%%)" % pct, style=pct_color)
        text.append("\nAvailable: $%.0f" % available)
        text.append("\nFloor: $%.0f" % self.config.capital_floor)
        text.append(
            "\nPositions: %d / %d"
            % (summary["open_positions"], self.config.max_open_positions)
        )

        return Panel(text, title="[bold]Capital[/bold]", border_style="cyan")

    def _build_stats_panel(self) -> Panel:
        """Today's opportunity funnel stats."""
        stats = self.risk_manager.get_daily_stats()

        seen = stats["opportunities_seen"]
        filtered = stats["opportunities_filtered"]
        traded = stats["opportunities_traded"]

        # Compute settled from settlement tracker
        sett = self.settlement_tracker.get_settlement_stats()
        settled = sett["total_trades_logged"]

        pass_rate = (traded / seen * 100) if seen > 0 else 0

        text = Text()
        text.append("Seen:     %d\n" % seen)
        text.append("Filtered: %d\n" % filtered)
        text.append("Traded:   %d\n" % traded)
        text.append("Settled:  %d\n" % settled)
        text.append("Pass rate: %.1f%%" % pass_rate)

        return Panel(text, title="[bold]Today's Stats[/bold]", border_style="cyan")

    def _build_pnl_panel(self) -> Panel:
        """Today's profit and loss."""
        stats = self.risk_manager.get_daily_stats()

        gross = stats["gross_profit"]
        gas = stats["gas_costs"]
        net = stats["net_profit"]

        # Color net profit
        if net > 0:
            net_style = "bold green"
        elif net < 0:
            net_style = "bold red"
        else:
            net_style = "white"

        text = Text()
        text.append("Gross:  $%.2f\n" % gross)
        text.append("Gas:   -$%.2f\n" % gas)
        text.append("Net:    ")
        text.append("$%.2f" % net, style=net_style)

        return Panel(text, title="[bold]Today's P&L[/bold]", border_style="cyan")

    def _build_velocity_panel(self) -> Panel:
        """Capital velocity and avg hold time."""
        sett = self.settlement_tracker.get_settlement_stats()
        velocity = sett.get("global_capital_velocity", 0.0)

        # Compute average hold across categories
        total_times = []
        for cat_data in sett.get("by_category", {}).values():
            if cat_data["trade_count"] > 0:
                total_times.append(cat_data["avg_settlement_hours"])

        avg_hold = sum(total_times) / len(total_times) if total_times else 0.0

        text = Text()
        text.append("%.2f turns/day\n" % velocity)
        text.append("Avg hold: %.1fh\n" % avg_hold)
        text.append("Pending: %d" % sett.get("pending_resolutions", 0))

        return Panel(text, title="[bold]Capital Velocity[/bold]", border_style="cyan")

    def _build_positions_table(self) -> Panel:
        """Open positions table."""
        table = Table(expand=True, show_edge=False, pad_edge=False)
        table.add_column("Market", style="white", ratio=4, no_wrap=True)
        table.add_column("Cat", style="cyan", ratio=1)
        table.add_column("Price", justify="right", ratio=1)
        table.add_column("Amount", justify="right", ratio=1)
        table.add_column("Held", justify="right", ratio=1)
        table.add_column("Exp Profit", justify="right", ratio=1)
        table.add_column("Status", ratio=1)

        now = time.time()
        open_positions = [
            p for p in self.order_manager.positions.values()
            if p.status in ("pending", "filled")
        ]

        # Sort by placed_at descending (newest first)
        open_positions.sort(key=lambda p: p.placed_at, reverse=True)

        for pos in open_positions[:self.config.max_open_positions]:
            hold_hours = (now - pos.filled_at) / 3600 if pos.filled_at > 0 else 0

            status_style = "green" if pos.status == "filled" else "yellow"
            profit_style = "green" if pos.net_profit > 0 else "red"

            table.add_row(
                pos.question[:35],
                pos.category[:7],
                "$%.2f" % pos.price,
                "$%.0f" % pos.cost,
                "%.1fh" % hold_hours,
                Text("$%.2f" % pos.net_profit, style=profit_style),
                Text(pos.status, style=status_style),
            )

        if not open_positions:
            table.add_row(
                "[dim]No open positions[/dim]", "", "", "", "", "", ""
            )

        count = len(open_positions)
        return Panel(
            table,
            title="[bold]Open Positions (%d/%d)[/bold]"
            % (count, self.config.max_open_positions),
            border_style="green",
        )

    def _build_recent_trades_table(self) -> Panel:
        """Last 5 completed trades."""
        table = Table(expand=True, show_edge=False, pad_edge=False)
        table.add_column("Market", style="white", ratio=4, no_wrap=True)
        table.add_column("Cat", style="cyan", ratio=1)
        table.add_column("Hold", justify="right", ratio=1)
        table.add_column("Expected", justify="right", ratio=1)
        table.add_column("Actual", justify="right", ratio=1)
        table.add_column("Gas", justify="right", ratio=1)
        table.add_column("Source", ratio=1)

        logs = self.settlement_tracker.get_recent_trade_logs(5)

        for log in reversed(logs):  # newest first
            actual_style = "green" if log["actual_profit"] > 0 else "red"
            flagged = " !" if log["flagged"] else ""

            table.add_row(
                (log.get("market_id", "")[:35]) + flagged,
                log["category"][:7],
                "%.1fh" % log["hold_time_hours"],
                "$%.2f" % log["expected_profit"],
                Text("$%.2f" % log["actual_profit"], style=actual_style),
                "$%.4f" % log["gas_cost"],
                log["resolution_source"][:8],
            )

        if not logs:
            table.add_row(
                "[dim]No settled trades yet[/dim]",
                "", "", "", "", "", "",
            )

        return Panel(
            table,
            title="[bold]Last 5 Settled Trades[/bold]",
            border_style="yellow",
        )

    def _build_category_panel(self) -> Panel:
        """Category performance breakdown."""
        sett = self.settlement_tracker.get_settlement_stats()
        by_cat = sett.get("by_category", {})

        text = Text()
        if by_cat:
            for cat, data in sorted(by_cat.items()):
                text.append("%s: " % cat, style="bold")
                text.append("%d trades\n" % data["trade_count"])
                text.append("  avg settle: %.1fh\n" % data["avg_settlement_hours"])
                text.append("  median:     %.1fh\n" % data["median_settlement_hours"])
                text.append(
                    "  velocity:   %.2f turns\n" % data["capital_velocity"]
                )
        else:
            text.append("[dim]No completed trades yet[/dim]")

        return Panel(
            text,
            title="[bold]Category Performance[/bold]",
            border_style="magenta",
        )

    def _build_status_panel(self) -> Panel:
        """WebSocket status, circuit breakers, alerts."""
        stats = self.risk_manager.get_daily_stats()

        text = Text()

        # WebSocket
        ws = stats["ws_connected"]
        if ws:
            text.append("WebSocket: ", style="white")
            text.append("Connected\n", style="bold green")
        else:
            text.append("WebSocket: ", style="white")
            text.append("DISCONNECTED\n", style="bold red")

        # Circuit breaker
        if stats["circuit_breaker_open"]:
            text.append("Breaker:   ", style="white")
            text.append(
                "OPEN (%s)\n" % stats["circuit_breaker_reason"],
                style="bold red",
            )
        else:
            text.append("Breaker:   ", style="white")
            text.append("OK\n", style="green")

        # Blacklist
        text.append(
            "Blacklist: %d entries\n" % stats["blacklist_size"]
        )

        # Consecutive failures
        failures = stats["consecutive_failures"]
        if failures > 0:
            text.append("Failures:  ", style="white")
            text.append(
                "%d consecutive\n" % failures, style="yellow"
            )

        # Alerts from settlement tracker
        sett = self.settlement_tracker.get_settlement_stats()
        flagged = sett.get("flagged_trades", 0)
        if flagged > 0:
            text.append("Flagged:   ", style="white")
            text.append("%d trades\n" % flagged, style="yellow")

        rpc_pending = sett.get("rpc_confirmed_pending_redeem", 0)
        if rpc_pending > 0:
            text.append("RPC ready: ", style="white")
            text.append(
                "%d awaiting redeem\n" % rpc_pending, style="cyan"
            )

        return Panel(
            text, title="[bold]Status[/bold]", border_style="magenta"
        )
