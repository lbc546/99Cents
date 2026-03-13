"""Settlement tracking: on-chain resolution detection, per-trade logging, running stats.

Augments the Redeemer with:
  1. Direct RPC polling of payoutNumerators() on the CT Framework contract
     to detect resolution before Gamma API reports it.
  2. Per-trade settlement log (hold time, actual vs expected profit, gas).
  3. Running stats: avg settlement time by category, capital velocity.
  4. Anomaly flags: overdue settlements, unexpected price movement, Esports variance.

Architecture (tracker-signals-redeemer pattern):
  - Tracker polls payoutNumerators(conditionId, 0) on-chain every 15s.
  - Non-zero payout = definitive resolution confirmation.
  - Redeemer checks is_resolution_confirmed() for fast-path (skip 2.5hr UMA wait).
  - Trade completion logged after redemption with full settlement metrics.

Data flow:
  Filled Position
    ├→ RPC monitor (15s)  → payoutNumerators() → resolution_confirmed[]
    ├→ CLOB monitor (60s) → Gamma API resolved  → resolution_confirmed[]
    ├→ Anomaly monitor    → overdue/price/variance alerts
    └→ Redeemer checks is_resolution_confirmed() for fast-path redemption

Key thresholds from analysis:
  - Crypto: 3.02hr median, flag at >8h
  - Esports: 1.97hr median, 9.46hr mean (high variance), flag at >24h
  - 90% settle within 24h
"""

import asyncio
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

import aiohttp

from bot.config import BotConfig
from bot.logger import log_event
from bot.order_manager import OrderManager, Position

logger = logging.getLogger("arb_bot")

# Minimal ABI for Conditional Tokens Framework — payoutNumerators
CT_FRAMEWORK_ABI = [
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "index", "type": "uint256"},
        ],
        "name": "payoutNumerators",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


@dataclass
class TradeLog:
    """Completed trade record for settlement analysis."""

    order_id: str
    market_id: str
    category: str
    entry_price: float
    shares: float
    cost: float
    expected_net_profit: float
    actual_net_profit: float
    actual_gas_cost: float
    filled_at: float
    resolved_at: float
    redeemed_at: float
    hold_time_seconds: float
    resolution_source: str  # "rpc_poll" or "gamma_api"
    flagged: bool = False
    flag_reasons: list[str] = field(default_factory=list)


@dataclass
class CategoryStats:
    """Running settlement statistics for a category."""

    settlement_times: list[float] = field(default_factory=list)
    total_redeemed_value: float = 0.0
    total_deployed_seconds: float = 0.0  # cost * hold_time for each trade

    @property
    def avg_settlement_hours(self) -> float:
        if not self.settlement_times:
            return 0.0
        return (sum(self.settlement_times) / len(self.settlement_times)) / 3600

    @property
    def median_settlement_hours(self) -> float:
        if not self.settlement_times:
            return 0.0
        s = sorted(self.settlement_times)
        n = len(s)
        mid = n // 2
        if n % 2 == 0:
            return ((s[mid - 1] + s[mid]) / 2) / 3600
        return s[mid] / 3600

    @property
    def settlement_variance(self) -> float:
        if len(self.settlement_times) < 2:
            return 0.0
        mean = sum(self.settlement_times) / len(self.settlement_times)
        return sum((t - mean) ** 2 for t in self.settlement_times) / len(
            self.settlement_times
        )


class SettlementTracker:
    """Monitors open positions for on-chain resolution and tracks settlement stats."""

    def __init__(self, config: BotConfig, order_manager: OrderManager):
        self.config = config
        self.order_manager = order_manager
        self.risk_manager = None  # Set by main.py

        # On-chain detection state
        self._w3 = None
        self._ct_contract = None
        self._resolution_confirmed: dict[str, float] = {}  # order_id -> resolved_at

        # Trade logs and per-category running stats
        self._trade_logs: list[TradeLog] = []
        self._category_stats: dict[str, CategoryStats] = defaultdict(CategoryStats)

        # Esports variance alert (one-shot per session)
        self._esports_variance_alert_sent: bool = False

    # ------------------------------------------------------------------
    # Web3 initialization (lazy, reuses existing RPC URL)
    # ------------------------------------------------------------------

    def _init_web3(self):
        """Lazy-initialize web3 connection to CT Framework contract."""
        if self._w3 is not None:
            return

        from web3 import Web3

        self._w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc_url))
        if not self._w3.is_connected():
            logger.error(
                "Settlement tracker: cannot connect to Polygon RPC: %s",
                self.config.polygon_rpc_url,
            )
            return

        self._ct_contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.ct_framework_address),
            abi=CT_FRAMEWORK_ABI,
        )
        logger.info(
            "Settlement tracker: Web3 initialized (CT Framework at %s)",
            self.config.ct_framework_address,
        )

    # ------------------------------------------------------------------
    # Task 1: On-chain RPC polling for resolution detection
    # ------------------------------------------------------------------

    async def run_rpc_resolution_monitor(self):
        """Poll payoutNumerators() on-chain for filled positions.

        Runs every rpc_poll_interval_seconds (default 15s).
        Batches calls to respect RPC rate limits (max 20 calls/min at defaults).
        """
        if self.config.dry_run:
            logger.info("RPC resolution monitor skipped (dry run — no on-chain state)")
            # Block forever so this task doesn't trigger FIRST_COMPLETED shutdown
            await asyncio.Event().wait()

        logger.info(
            "RPC resolution monitor started (interval=%ds, batch=%d)",
            self.config.rpc_poll_interval_seconds,
            self.config.rpc_poll_batch_size,
        )
        await asyncio.sleep(10)  # let positions accumulate

        while True:
            try:
                await self._poll_rpc_resolutions()
            except Exception:
                logger.exception("RPC resolution poll error")
            await asyncio.sleep(self.config.rpc_poll_interval_seconds)

    async def _poll_rpc_resolutions(self):
        """Check payoutNumerators for all filled positions not yet confirmed."""
        filled = [
            p
            for p in self.order_manager.positions.values()
            if p.status == "filled" and p.order_id not in self._resolution_confirmed
        ]

        if not filled:
            return

        self._init_web3()
        if not self._ct_contract:
            return

        batch_size = self.config.rpc_poll_batch_size
        for i in range(0, len(filled), batch_size):
            batch = filled[i : i + batch_size]
            tasks = [
                asyncio.to_thread(self._check_payout_numerator, pos)
                for pos in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for pos, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.debug(
                        "RPC check failed for %s: %s", pos.order_id, result
                    )
                    continue
                if result:  # resolved
                    now = time.time()
                    self._resolution_confirmed[pos.order_id] = now
                    hold_hours = (now - pos.filled_at) / 3600
                    log_event(
                        logger,
                        "RESOLUTION_DETECTED_ONCHAIN",
                        "On-chain resolution detected (hold=%.1fh) | %s"
                        % (hold_hours, pos.question[:50]),
                        market_id=pos.market_id,
                        details={
                            "order_id": pos.order_id,
                            "condition_id": pos.condition_id,
                            "hold_hours": round(hold_hours, 2),
                        },
                    )

            # 1s delay between batches to avoid RPC flood
            if i + batch_size < len(filled):
                await asyncio.sleep(1)

    def _check_payout_numerator(self, pos: Position) -> bool:
        """Check if payoutNumerators returns non-zero for a condition.

        Called via asyncio.to_thread (blocking web3 call).
        Returns True if condition is resolved on-chain.
        """
        try:
            cond_hex = pos.condition_id.replace("0x", "")
            cond_bytes = bytes.fromhex(cond_hex)
            cond_bytes32 = cond_bytes.ljust(32, b"\x00")

            payout = self._ct_contract.functions.payoutNumerators(
                cond_bytes32, 0
            ).call()
            return payout > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Task 2: CLOB/Gamma API polling for settlement status (fallback)
    # ------------------------------------------------------------------

    async def run_clob_settlement_monitor(self):
        """Poll Gamma API for settlement status of open positions.

        Complements RPC — catches positions RPC missed due to errors.
        Only checks positions NOT already confirmed by RPC.
        """
        logger.info(
            "CLOB settlement monitor started (interval=%ds)",
            self.config.redeem_check_interval_seconds,
        )
        await asyncio.sleep(30)

        while True:
            try:
                await self._poll_clob_settlements()
            except Exception:
                logger.exception("CLOB settlement poll error")
            await asyncio.sleep(self.config.redeem_check_interval_seconds)

    async def _poll_clob_settlements(self):
        """Check Gamma API for resolved status on filled positions."""
        filled = [
            p
            for p in self.order_manager.positions.values()
            if p.status == "filled" and p.order_id not in self._resolution_confirmed
        ]

        if not filled:
            return

        async with aiohttp.ClientSession() as session:
            for pos in filled:
                url = "%s/markets/%s" % (
                    self.config.gamma_base_url,
                    pos.market_id,
                )
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status != 200:
                            continue
                        market = await resp.json()
                        if market.get("resolved", False):
                            now = time.time()
                            self._resolution_confirmed[pos.order_id] = now
                            log_event(
                                logger,
                                "RESOLUTION_DETECTED_API",
                                "API resolution detected (hold=%.1fh) | %s"
                                % (
                                    (now - pos.filled_at) / 3600,
                                    pos.question[:50],
                                ),
                                market_id=pos.market_id,
                                details={
                                    "order_id": pos.order_id,
                                    "source": "gamma_api",
                                },
                            )
                except Exception:
                    logger.debug(
                        "CLOB settlement check failed for %s", pos.market_id
                    )

    # ------------------------------------------------------------------
    # Query: is position confirmed resolved?
    # ------------------------------------------------------------------

    def is_resolution_confirmed(self, order_id: str) -> bool:
        """Called by Redeemer to check if RPC/API has confirmed resolution."""
        return order_id in self._resolution_confirmed

    def get_resolution_time(self, order_id: str) -> float:
        """Get timestamp when resolution was confirmed, or 0."""
        return self._resolution_confirmed.get(order_id, 0.0)

    # ------------------------------------------------------------------
    # Task 3: Anomaly detection (overdue, price movement, variance)
    # ------------------------------------------------------------------

    async def run_anomaly_monitor(self):
        """Periodic anomaly checks on open positions."""
        logger.info("Anomaly monitor started (interval=60s)")
        await asyncio.sleep(60)

        while True:
            try:
                self._check_overdue_positions()
                await self._check_price_movement()
                self._check_esports_variance()
            except Exception:
                logger.exception("Anomaly monitor error")
            await asyncio.sleep(60)

    def _check_overdue_positions(self):
        """Flag positions exceeding category-specific settlement thresholds.

        Thresholds: Crypto >8h, Esports >24h, default >24h.
        """
        now = time.time()
        for pos in self.order_manager.positions.values():
            if pos.status != "filled":
                continue

            hold_hours = (now - pos.filled_at) / 3600
            threshold = self.config.settlement_timeout_hours.get(
                pos.category,
                self.config.settlement_timeout_hours.get("default", 24),
            )

            if hold_hours > threshold:
                log_event(
                    logger,
                    "SETTLEMENT_OVERDUE",
                    "Overdue: %.1fh > %dh threshold | %s"
                    % (hold_hours, threshold, pos.question[:50]),
                    level="WARNING",
                    market_id=pos.market_id,
                    details={
                        "order_id": pos.order_id,
                        "category": pos.category,
                        "hold_hours": round(hold_hours, 1),
                        "threshold_hours": threshold,
                    },
                )
                if self.risk_manager:
                    self.risk_manager.record_settlement_timeout(
                        pos.market_id, pos.question
                    )

    async def _check_price_movement(self):
        """Flag if current market price moved unexpectedly after entry.

        Checks CLOB orderbook best bid vs entry price.
        Alert threshold: configurable, default $0.03 drop.
        """
        filled = [
            p
            for p in self.order_manager.positions.values()
            if p.status == "filled" and p.order_id not in self._resolution_confirmed
        ]

        if not filled:
            return

        async with aiohttp.ClientSession() as session:
            for pos in filled:
                url = "%s/book" % self.config.clob_base_url
                try:
                    async with session.get(
                        url,
                        params={"token_id": pos.token_id},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        book = await resp.json()
                        bids = book.get("bids", [])
                        if bids:
                            best_bid = max(float(b.get("price", 0)) for b in bids)
                            if best_bid < 0.02:
                                continue  # Book torn down post-resolution, not a real drop
                            drop = pos.price - best_bid
                            if drop > self.config.price_movement_alert_threshold:
                                log_event(
                                    logger,
                                    "PRICE_MOVEMENT_ALERT",
                                    "Price dropped $%.2f: $%.2f -> $%.2f | %s"
                                    % (
                                        drop,
                                        pos.price,
                                        best_bid,
                                        pos.question[:50],
                                    ),
                                    level="WARNING",
                                    market_id=pos.market_id,
                                    details={
                                        "order_id": pos.order_id,
                                        "entry_price": pos.price,
                                        "current_bid": best_bid,
                                        "drop": round(drop, 4),
                                    },
                                )
                except Exception:
                    pass  # Non-critical, skip silently

    def _check_esports_variance(self):
        """Alert if Esports settlement variance is consistently high.

        Triggers once after 5+ completed Esports trades if coefficient
        of variation exceeds threshold (default 1.5).
        """
        stats = self._category_stats.get("Esports")
        if not stats or len(stats.settlement_times) < 5:
            return

        if self._esports_variance_alert_sent:
            return

        mean = sum(stats.settlement_times) / len(stats.settlement_times)
        if mean == 0:
            return

        std = math.sqrt(stats.settlement_variance)
        cv = std / mean

        if cv > self.config.esports_variance_cv_threshold:
            self._esports_variance_alert_sent = True
            log_event(
                logger,
                "ESPORTS_VARIANCE_ALERT",
                "Esports settlement variance high: CV=%.2f "
                "(mean=%.1fh, std=%.1fh, n=%d)"
                % (cv, mean / 3600, std / 3600, len(stats.settlement_times)),
                level="WARNING",
                details={
                    "coefficient_of_variation": round(cv, 3),
                    "mean_hours": round(mean / 3600, 2),
                    "std_hours": round(std / 3600, 2),
                    "sample_count": len(stats.settlement_times),
                    "median_hours": round(stats.median_settlement_hours, 2),
                },
            )

    # ------------------------------------------------------------------
    # Cut-loss monitor
    # ------------------------------------------------------------------

    async def run_cut_loss_monitor(self):
        """Monitor filled positions and sell if price drops below threshold.

        Requires both best bid AND best ask below threshold to confirm
        genuine market consensus (not just a thin/manipulated book).
        """
        if not self.config.cut_loss_enabled:
            logger.info("Cut-loss monitor disabled")
            await asyncio.Event().wait()
            return

        logger.info("Cut-loss monitor started (threshold=$%.2f, emergency=$%.2f, interval=%ds)",
                     self.config.cut_loss_threshold,
                     self.config.cut_loss_emergency_threshold,
                     self.config.cut_loss_check_interval)
        await asyncio.sleep(60)  # let positions accumulate

        # Track consecutive below-threshold checks per order_id
        cut_loss_counts: dict[str, int] = {}

        while True:
            try:
                await self._monitor_sell_orders()
                await self._check_cut_loss(cut_loss_counts)
            except Exception:
                logger.exception("Cut-loss monitor error")
            await asyncio.sleep(self.config.cut_loss_check_interval)

    async def _check_cut_loss(self, counts: dict[str, int]):
        """Check all filled positions for cut-loss conditions."""
        filled = [
            p for p in self.order_manager.positions.values()
            if p.status == "filled"
            and p.order_id not in self._resolution_confirmed
            and not p.cut_loss_order_id  # no outstanding sell
        ]

        if not filled:
            logger.debug("Cut-loss: no eligible positions")
            return

        logger.info("Cut-loss check: %d filled positions to evaluate", len(filled))

        now = time.time()
        min_hold_sec = self.config.cut_loss_min_hold_minutes * 60

        async with aiohttp.ClientSession() as session:
            for pos in filled:
                # Skip if held less than min hold time
                if pos.filled_at > 0 and (now - pos.filled_at) < min_hold_sec:
                    hold_sec = now - pos.filled_at
                    logger.info("Cut-loss skip (hold=%.0fs < %ds): %s",
                                hold_sec, min_hold_sec, pos.question[:40])
                    continue

                # Fetch orderbook
                url = "%s/book" % self.config.clob_base_url
                try:
                    async with session.get(
                        url,
                        params={"token_id": pos.token_id},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            logger.info("Cut-loss skip (book HTTP %d): %s",
                                        resp.status, pos.question[:40])
                            continue
                        book = await resp.json()
                except Exception as e:
                    logger.info("Cut-loss skip (book error: %s): %s",
                                str(e)[:40], pos.question[:40])
                    continue

                bids = book.get("bids", [])
                asks = book.get("asks", [])

                if not bids:
                    logger.info("Cut-loss skip (no bids): %s", pos.question[:40])
                    continue

                # CLOB returns bids ascending — best bid is the highest
                best_bid = max(float(b.get("price", 0)) for b in bids)
                # CLOB returns asks descending — best ask is the lowest
                best_ask = min(float(a.get("price", 1.0)) for a in asks) if asks else 1.0

                # Skip torn-down books (post-resolution)
                if best_bid < 0.02:
                    logger.info("Cut-loss skip (bid=$%.2f, book torn down): %s",
                                best_bid, pos.question[:40])
                    continue

                # Check bid depth
                total_bid_depth = sum(
                    float(b.get("size", 0)) * float(b.get("price", 0))
                    for b in bids
                )
                if total_bid_depth < self.config.cut_loss_min_bid_depth:
                    logger.info("Cut-loss skip (depth=$%.1f < $%.1f): %s",
                                total_bid_depth, self.config.cut_loss_min_bid_depth,
                                pos.question[:40])
                    continue

                hold_hours = (now - pos.filled_at) / 3600 if pos.filled_at > 0 else 0
                logger.info("Cut-loss eval: bid=$%.2f ask=$%.2f depth=$%.0f hold=%.1fh | %s",
                            best_bid, best_ask, total_bid_depth, hold_hours,
                            pos.question[:50])

                # Overdue escalation: held past timeout AND both bid+ask below overdue threshold
                timeout = self.config.settlement_timeout_hours.get(
                    pos.category,
                    self.config.settlement_timeout_hours.get("default", 24),
                )
                if (hold_hours > timeout
                        and best_bid < self.config.cut_loss_overdue_threshold
                        and best_ask < self.config.cut_loss_overdue_threshold):
                    log_event(logger, "CUTLOSS_OVERDUE_TRIGGER",
                              "Overdue %.1fh + bid=$%.2f ask=$%.2f | %s" % (
                                  hold_hours, best_bid, best_ask, pos.question[:50]),
                              level="WARNING", market_id=pos.market_id)
                    self.order_manager.place_sell_order(pos.order_id, best_bid)
                    if self.risk_manager:
                        actual_loss = pos.cost - pos.size * best_bid
                        self.risk_manager.record_trade_result(
                            net_profit=-actual_loss, gas_cost=0.0)
                        self.risk_manager.record_cut_loss(
                            pos.market_id, actual_loss, pos.question)
                    continue

                # Emergency: both bid AND ask below emergency threshold → immediate sell at $0.40
                if (best_bid <= self.config.cut_loss_emergency_threshold
                        and best_ask <= self.config.cut_loss_emergency_threshold):
                    sell_price = 0.40
                    log_event(logger, "CUTLOSS_EMERGENCY",
                              "EMERGENCY bid=$%.2f ask=$%.2f | sell@$%.2f | %s" % (
                                  best_bid, best_ask, sell_price, pos.question[:50]),
                              level="WARNING", market_id=pos.market_id)
                    self.order_manager.place_sell_order(pos.order_id, sell_price)
                    if self.risk_manager:
                        actual_loss = pos.cost - pos.size * sell_price
                        self.risk_manager.record_trade_result(
                            net_profit=-actual_loss, gas_cost=0.0)
                        self.risk_manager.record_cut_loss(
                            pos.market_id, actual_loss, pos.question)
                    counts.pop(pos.order_id, None)
                    continue

                # Normal: both bid AND ask below threshold
                if (best_bid <= self.config.cut_loss_threshold
                        and best_ask <= self.config.cut_loss_threshold):
                    counts[pos.order_id] = counts.get(pos.order_id, 0) + 1
                    log_event(logger, "CUTLOSS_ALERT",
                              "bid=$%.2f ask=$%.2f (%d/%d confirms) | %s" % (
                                  best_bid, best_ask,
                                  counts[pos.order_id],
                                  self.config.cut_loss_confirmations,
                                  pos.question[:50]),
                              level="WARNING", market_id=pos.market_id)

                    if counts[pos.order_id] >= self.config.cut_loss_confirmations:
                        self.order_manager.place_sell_order(pos.order_id, 0.70)
                        if self.risk_manager:
                            actual_loss = pos.cost - pos.size * 0.70
                            self.risk_manager.record_trade_result(
                                net_profit=-actual_loss, gas_cost=0.0)
                            self.risk_manager.record_cut_loss(
                                pos.market_id, actual_loss, pos.question)
                        counts.pop(pos.order_id, None)
                else:
                    # Price recovered — reset counter
                    if pos.order_id in counts:
                        counts.pop(pos.order_id)

    async def _monitor_sell_orders(self):
        """Check outstanding cut-loss sell orders for fill/cancel status.

        If a sell order was cancelled (manually or by TTL expiry), clear
        cut_loss_order_id so the position becomes eligible for cut-loss again.
        If filled, mark the position as cut_loss.
        """
        pending = [
            p for p in self.order_manager.positions.values()
            if p.status == "filled" and p.cut_loss_order_id
        ]
        if not pending:
            return

        client = self.order_manager._get_client()
        if client is None and not self.config.dry_run:
            return

        for pos in pending:
            # Check TTL expiry — cancel and repost
            elapsed = time.time() - pos.cut_loss_triggered_at
            if elapsed > self.config.cut_loss_order_ttl:
                logger.info("Cut-loss sell TTL expired (%.0fs), cancelling: %s",
                            elapsed, pos.question[:40])
                self.order_manager.cancel_sell_order(pos.order_id)
                # Will be re-evaluated in next _check_cut_loss cycle
                continue

            if self.config.dry_run:
                continue

            # Check order status via CLOB API
            try:
                order = client.get_order(pos.cut_loss_order_id)
                status = order.get("status", "").lower() if order else ""

                if status == "matched":
                    sell_price = float(order.get("price", 0))
                    self.order_manager.mark_cut_loss(pos.order_id, sell_price)
                    logger.info("Cut-loss sell FILLED: %s", pos.question[:40])
                elif status in ("cancelled", "canceled"):
                    pos.cut_loss_order_id = ""
                    pos.cut_loss_triggered_at = 0.0
                    self.order_manager._save_positions()
                    logger.info("Cut-loss sell cancelled externally, will retry: %s",
                                pos.question[:40])
            except Exception:
                logger.debug("Failed to check sell order status for %s",
                             pos.cut_loss_order_id[:16])

    # ------------------------------------------------------------------
    # Trade completion logging (called by Redeemer after redemption)
    # ------------------------------------------------------------------

    def record_trade_completion(
        self, order_id: str, actual_gas_cost: float, redeemed_at: float
    ):
        """Record a completed trade with full settlement metrics.

        Called by Redeemer after successful on-chain redemption.
        Creates TradeLog, updates CategoryStats, logs TRADE_COMPLETED.
        """
        pos = self.order_manager.positions.get(order_id)
        if not pos:
            return

        resolved_at = self._resolution_confirmed.get(order_id, redeemed_at)
        hold_time = redeemed_at - pos.filled_at
        resolution_source = (
            "rpc_poll" if order_id in self._resolution_confirmed else "gamma_api"
        )

        # Actual net profit = payout - cost - actual_gas
        actual_payout = pos.size * 1.0  # winning shares pay $1 each
        actual_net = actual_payout - pos.cost - actual_gas_cost

        # Determine flags
        flags = []
        threshold_hours = self.config.settlement_timeout_hours.get(
            pos.category,
            self.config.settlement_timeout_hours.get("default", 24),
        )
        if hold_time / 3600 > threshold_hours:
            flags.append("overdue_settlement")

        profit_diff = abs(actual_net - pos.net_profit)
        if profit_diff > 0.10:  # > $0.10 deviation from expected
            flags.append("profit_deviation=%.2f" % profit_diff)

        trade_log = TradeLog(
            order_id=order_id,
            market_id=pos.market_id,
            category=pos.category,
            entry_price=pos.price,
            shares=pos.size,
            cost=pos.cost,
            expected_net_profit=pos.net_profit,
            actual_net_profit=actual_net,
            actual_gas_cost=actual_gas_cost,
            filled_at=pos.filled_at,
            resolved_at=resolved_at,
            redeemed_at=redeemed_at,
            hold_time_seconds=hold_time,
            resolution_source=resolution_source,
            flagged=bool(flags),
            flag_reasons=flags,
        )
        self._trade_logs.append(trade_log)

        # Update category stats
        cat_stats = self._category_stats[pos.category]
        cat_stats.settlement_times.append(hold_time)
        cat_stats.total_redeemed_value += actual_payout
        cat_stats.total_deployed_seconds += pos.cost * hold_time

        log_event(
            logger,
            "TRADE_COMPLETED",
            "SETTLED hold=%.1fh actual=$%.2f expected=$%.2f "
            "gas=$%.4f source=%s %s| %s"
            % (
                hold_time / 3600,
                actual_net,
                pos.net_profit,
                actual_gas_cost,
                resolution_source,
                "[FLAGGED: %s] " % ", ".join(flags) if flags else "",
                pos.question[:40],
            ),
            market_id=pos.market_id,
            details={
                "order_id": order_id,
                "category": pos.category,
                "hold_time_hours": round(hold_time / 3600, 2),
                "actual_net_profit": round(actual_net, 2),
                "expected_net_profit": round(pos.net_profit, 2),
                "actual_gas_cost": round(actual_gas_cost, 4),
                "resolution_source": resolution_source,
                "flags": flags,
            },
        )

    # ------------------------------------------------------------------
    # Statistics queries
    # ------------------------------------------------------------------

    def get_settlement_stats(self) -> dict:
        """Return running settlement statistics.

        Includes per-category metrics and global capital velocity.
        Capital velocity = total_redeemed / avg_deployed (weighted by hold time).
        """
        stats = {}
        for category, cat_stats in self._category_stats.items():
            avg_deployed = 0.0
            if cat_stats.settlement_times:
                total_hold = sum(cat_stats.settlement_times)
                if total_hold > 0:
                    avg_deployed = cat_stats.total_deployed_seconds / total_hold

            capital_velocity = 0.0
            if avg_deployed > 0:
                capital_velocity = cat_stats.total_redeemed_value / avg_deployed

            stats[category] = {
                "trade_count": len(cat_stats.settlement_times),
                "avg_settlement_hours": round(cat_stats.avg_settlement_hours, 2),
                "median_settlement_hours": round(
                    cat_stats.median_settlement_hours, 2
                ),
                "total_redeemed_value": round(cat_stats.total_redeemed_value, 2),
                "capital_velocity": round(capital_velocity, 3),
            }

        # Global capital velocity
        total_redeemed = sum(
            cs.total_redeemed_value for cs in self._category_stats.values()
        )
        total_deployed_time = sum(
            cs.total_deployed_seconds for cs in self._category_stats.values()
        )
        total_hold = sum(
            sum(cs.settlement_times) for cs in self._category_stats.values()
        )
        global_avg_deployed = (
            total_deployed_time / total_hold if total_hold > 0 else 0
        )
        global_velocity = (
            total_redeemed / global_avg_deployed if global_avg_deployed > 0 else 0
        )

        return {
            "by_category": stats,
            "global_capital_velocity": round(global_velocity, 3),
            "total_trades_logged": len(self._trade_logs),
            "flagged_trades": sum(1 for t in self._trade_logs if t.flagged),
            "pending_resolutions": len(
                [
                    p
                    for p in self.order_manager.positions.values()
                    if p.status == "filled"
                ]
            ),
            "rpc_confirmed_pending_redeem": len(
                [
                    oid
                    for oid in self._resolution_confirmed
                    if self.order_manager.positions.get(oid)
                    and self.order_manager.positions[oid].status == "filled"
                ]
            ),
        }

    def get_recent_trade_logs(self, n: int = 20) -> list[dict]:
        """Return the N most recent trade logs as dicts."""
        recent = self._trade_logs[-n:]
        return [
            {
                "order_id": t.order_id,
                "market_id": t.market_id,
                "category": t.category,
                "hold_time_hours": round(t.hold_time_seconds / 3600, 2),
                "expected_profit": round(t.expected_net_profit, 2),
                "actual_profit": round(t.actual_net_profit, 2),
                "gas_cost": round(t.actual_gas_cost, 4),
                "resolution_source": t.resolution_source,
                "flagged": t.flagged,
                "flag_reasons": t.flag_reasons,
            }
            for t in recent
        ]
