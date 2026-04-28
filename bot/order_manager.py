"""Order execution module calibrated to the profitability model.

Key constraints from analysis:
  - Base case: $35 avg fill size, $0.65 net per trade after gas
  - Conservative case failed due to high gas costs on small positions
  - ~20 opps/day trending down — competition is real, speed matters

Every order goes through a pre-execution profitability check:
  Gross = ($1.00 - fill_price) * shares
  Net = Gross - gas_cost - platform_fees
  Skip if net < min_net_profit ($0.30) or position < min_position_size ($20)
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from bot.config import BotConfig
from bot.logger import log_event

logger = logging.getLogger("arb_bot")


@dataclass
class Position:
    """Tracks a single open position."""

    market_id: str
    token_id: str
    condition_id: str
    question: str
    category: str
    order_id: str
    price: float
    size: float
    cost: float
    gross_profit: float
    gas_cost: float
    net_profit: float
    status: str  # "pending", "filled", "cancelled", "redeemed", "disputed", "cut_loss", "resolved_loss"
    placed_at: float
    filled_at: float = 0.0
    source: str = ""
    score: float = 0.0
    cut_loss_order_id: str = ""
    cut_loss_triggered_at: float = 0.0
    neg_risk: bool = False  # True for NegRiskAdapter markets — routes redemption accordingly
    outcome_index: int = -1  # Which outcome we bought (0=YES, 1=NO, etc); -1 = unknown/needs backfill


class OrderManager:
    """Manages order placement, tracking, and cancellation.

    Every order is checked against the profitability model before execution.
    Partial fills are accepted — if $30 is available at $0.98 out of $50 target,
    the $30 is taken.
    """

    POSITIONS_FILE = "data/positions.json"

    def __init__(self, config: BotConfig):
        self.config = config
        self.positions: dict[str, Position] = {}  # keyed by order_id
        self._clob_client = None
        self.risk_manager = None  # Set by main.py
        self._market_order_timestamps: dict[str, float] = {}  # market_id -> last order time
        self._wallet_balance: float | None = None  # set on startup from CLOB API
        self._load_positions()

    def _get_client(self):
        """Lazy-initialize the CLOB client with pre-derived API credentials."""
        if self._clob_client is not None:
            return self._clob_client

        if self.config.dry_run:
            return None

        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds

        if self.config.clob_api_key:
            api_creds = ApiCreds(
                api_key=self.config.clob_api_key,
                api_secret=self.config.clob_api_secret,
                api_passphrase=self.config.clob_api_passphrase,
            )
        else:
            tmp = ClobClient(
                self.config.clob_base_url,
                key=self.config.private_key,
                chain_id=self.config.chain_id,
                signature_type=2,
            )
            api_creds = tmp.create_or_derive_api_key()
            logger.warning("CLOB creds derived at runtime — set CLOB_API_KEY/SECRET/PASSPHRASE in .env")

        funder = self.config.polymarket_proxy_address or None
        self._clob_client = ClobClient(
            self.config.clob_base_url,
            key=self.config.private_key,
            chain_id=self.config.chain_id,
            creds=api_creds,
            signature_type=2,
            funder=funder,
        )
        logger.info("CLOB client initialized (funder=%s)", funder or "default")
        return self._clob_client

    def sync_wallet_balance(self):
        """Query CLOB API for actual available balance. Call on startup."""
        try:
            client = self._get_client()
            if client is None:
                return
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
            resp = client.get_balance_allowance(params)
            raw = float(resp.get("balance", 0))
            self._wallet_balance = raw / 1e6
            logger.info("Wallet balance synced: $%.2f available", self._wallet_balance)
        except Exception as e:
            logger.warning("Failed to sync wallet balance: %s", e)

    def sync_open_positions(self):
        """Fetch recent trades from CLOB API and add any untracked positions.

        This ensures positions placed outside the bot (or lost due to bugs)
        are picked up for settlement tracking and redemption.
        """
        try:
            client = self._get_client()
            if client is None:
                return

            from py_clob_client_v2.clob_types import TradeParams
            trades = client.get_trades(TradeParams())

            # Group trades by market — aggregate BUY fills
            # Each trade has: id, market, asset_id, side, price, size, ...
            tracked_markets = {p.market_id for p in self.positions.values()}
            tracked_tokens = {p.token_id for p in self.positions.values()}
            tracked_conditions = {p.condition_id for p in self.positions.values()
                                  if p.condition_id}
            tracked_order_ids = set(self.positions.keys())

            # Group by asset_id (token) to aggregate fills
            untracked = {}
            for trade in trades:
                order_id = trade.get("id", "")
                market_id = trade.get("market", "")
                asset_id = trade.get("asset_id", "")
                side = trade.get("side", "").upper()

                # Only track BUY trades (our strategy buys YES tokens)
                if side != "BUY":
                    continue

                # Skip if we already track this market (by market_id, token_id,
                # or condition_id — CLOB uses condition_id as market_id)
                if (market_id in tracked_markets
                        or asset_id in tracked_tokens
                        or market_id in tracked_conditions):
                    continue

                # Aggregate by market
                if market_id not in untracked:
                    untracked[market_id] = {
                        "market_id": market_id,
                        "token_id": asset_id,
                        "order_id": order_id,
                        "total_size": 0.0,
                        "total_cost": 0.0,
                        "prices": [],
                        "timestamp": trade.get("created_at", ""),
                    }
                size = float(trade.get("size", 0))
                price = float(trade.get("price", 0))
                untracked[market_id]["total_size"] += size
                untracked[market_id]["total_cost"] += size * price
                untracked[market_id]["prices"].append(price)

            # Create Position entries for untracked trades
            added = 0
            for market_id, info in untracked.items():
                avg_price = info["total_cost"] / info["total_size"] if info["total_size"] > 0 else 0
                size = info["total_size"]
                cost = info["total_cost"]
                gross = size - cost  # $1 per share at redemption minus cost
                gas = self.config.estimated_gas_cost_usd
                net = gross - gas

                pos = Position(
                    market_id=market_id,
                    token_id=info["token_id"],
                    condition_id=market_id if market_id.startswith("0x") else "",
                    question=f"[synced from CLOB] market {market_id}",
                    category="",
                    order_id=info["order_id"],
                    price=round(avg_price, 4),
                    size=round(size, 2),
                    cost=round(cost, 2),
                    gross_profit=round(gross, 4),
                    gas_cost=gas,
                    net_profit=round(net, 4),
                    status="filled",
                    placed_at=time.time(),
                    filled_at=time.time(),
                    source="clob_sync",
                )
                self.positions[info["order_id"]] = pos
                added += 1
                logger.info(
                    "Synced untracked position: market=%s size=%.2f cost=$%.2f avg_price=%.4f",
                    market_id, size, cost, avg_price,
                )

            if added:
                self._save_positions()
                logger.info("Synced %d untracked positions from CLOB trade history", added)
            else:
                logger.info("Position sync: all CLOB trades already tracked")

        except Exception as e:
            logger.warning("Failed to sync open positions: %s", e)

    def sync_redeemed_positions(self):
        """Check token balances for filled positions. Mark redeemed if balance is 0.

        Catches positions redeemed outside the bot (e.g. via Polymarket website).
        """
        try:
            client = self._get_client()
            if client is None:
                return

            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType

            filled = [p for p in self.positions.values() if p.status == "filled"]
            if not filled:
                return

            marked = 0
            for pos in filled:
                try:
                    params = BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=pos.token_id,
                        signature_type=2,
                    )
                    resp = client.get_balance_allowance(params)
                    balance = float(resp.get("balance", 0))
                    if balance == 0:
                        pos.status = "redeemed"
                        marked += 1
                        logger.info(
                            "Position redeemed externally: %s (market=%s)",
                            pos.question[:60], pos.market_id,
                        )
                except Exception as e:
                    logger.debug("Balance check failed for %s: %s", pos.order_id, e)

            if marked:
                self._save_positions()
                logger.info("Marked %d positions as redeemed (external redemption)", marked)
            else:
                logger.info("Redemption sync: all filled positions still have token balance")

        except Exception as e:
            logger.warning("Failed to sync redeemed positions: %s", e)

    def sync_pending_positions(self):
        """Verify stale 'pending' orders against CLOB on startup.

        Pending orders that are no longer open on CLOB get marked as
        cancelled (expired/dead) or filled (matched while bot was down).
        Without this, stale pending positions inflate total_deployed forever.

        Also recovers 'cancelled' positions that actually filled — the cancel
        logic may have raced with a fill, leaving shares in the wallet but
        the position marked cancelled.
        """
        try:
            client = self._get_client()
            if client is None:
                return

            pending = [p for p in self.positions.values() if p.status == "pending"]
            if not pending:
                logger.info("Pending sync: no pending positions to verify")

            updated = 0
            for pos in pending:
                try:
                    order = client.get_order(pos.order_id)
                    status = (order.get("status", "").lower()
                              if order else "")

                    if status == "matched":
                        # Order filled while bot was down
                        pos.status = "filled"
                        pos.filled_at = time.time()
                        updated += 1
                        logger.info(
                            "Pending→filled (matched on CLOB): %s (market=%s)",
                            pos.question[:60], pos.market_id,
                        )
                    elif status in ("cancelled", "canceled", "expired", ""):
                        # Order dead — free up the capital
                        pos.status = "cancelled"
                        updated += 1
                        logger.info(
                            "Pending→cancelled (status=%s on CLOB): %s (market=%s)",
                            status or "not_found", pos.question[:60], pos.market_id,
                        )
                    # "live" or "open" → still pending, leave as-is
                except Exception as e:
                    logger.debug("Failed to check pending order %s: %s",
                                 pos.order_id[:16], e)

            # Recover cancelled positions that actually filled (race between
            # TTL cancel and CLOB matching).
            cancelled = [p for p in self.positions.values() if p.status == "cancelled"]
            for pos in cancelled:
                try:
                    order = client.get_order(pos.order_id)
                    status = (order.get("status", "").lower()
                              if order else "")
                    if status == "matched":
                        pos.status = "filled"
                        pos.filled_at = time.time()
                        updated += 1
                        logger.info(
                            "Cancelled→filled (matched on CLOB): %s (market=%s)",
                            pos.question[:60], pos.market_id,
                        )
                except Exception as e:
                    logger.debug("Failed to check cancelled order %s: %s",
                                 pos.order_id[:16], e)

            if updated:
                self._save_positions()
                logger.info("Position sync: updated %d positions",
                            updated)

        except Exception as e:
            logger.warning("Failed to sync pending positions: %s", e)

    def _load_positions(self):
        """Load persisted positions from disk, pruning old completed ones."""
        import json, os
        if not os.path.exists(self.POSITIONS_FILE):
            return
        try:
            with open(self.POSITIONS_FILE, "r") as f:
                data = json.load(f)
            cutoff = time.time() - 7 * 86400  # 7 days
            pruned = 0
            for order_id, fields in data.items():
                # Backfill condition_id for CLOB-synced positions
                if not fields.get("condition_id") and fields.get("market_id", "").startswith("0x"):
                    fields["condition_id"] = fields["market_id"]
                pos = Position(**fields)
                # Prune completed positions older than 7 days
                if (pos.status not in ("pending", "filled")
                        and pos.filled_at and pos.filled_at < cutoff):
                    pruned += 1
                    continue
                self.positions[order_id] = pos
            active = sum(1 for p in self.positions.values() if p.status in ("pending", "filled"))
            logger.info("Loaded %d positions from disk (%d active, %d pruned)",
                        len(self.positions), active, pruned)
            if pruned:
                self._save_positions()
        except Exception as e:
            logger.warning("Failed to load positions: %s", e)

    def _save_positions(self):
        """Persist positions to disk."""
        import json, os
        os.makedirs(os.path.dirname(self.POSITIONS_FILE) or ".", exist_ok=True)
        try:
            from dataclasses import asdict
            data = {oid: asdict(pos) for oid, pos in self.positions.items()}
            with open(self.POSITIONS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save positions: %s", e)

    # ------------------------------------------------------------------
    # Position accounting
    # ------------------------------------------------------------------

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.status in ("pending", "filled"))

    @property
    def total_deployed(self) -> float:
        return sum(p.cost for p in self.positions.values() if p.status in ("pending", "filled"))

    @property
    def available_capital(self) -> float:
        # Policy limit: how much more we're allowed to deploy
        cap = self.config.max_total_deployed - self.total_deployed
        # Also cap by actual wallet USDC (prevents over-deploying when
        # policy says OK but wallet is low due to un-redeemed tokens)
        if self._wallet_balance is not None:
            cap = min(cap, self._wallet_balance)
        return max(cap, 0)

    def _has_position_in_market(self, market_id: str, condition_id: str = "",
                                token_id: str = "") -> bool:
        # Block re-buying if ANY position exists for this market,
        # regardless of status (pending, filled, cancelled, redeemed).
        # Check market_id, condition_id, AND token_id to catch duplicates
        # across different ID schemes (Gamma numeric vs CLOB 0x condition_id).
        for p in self.positions.values():
            if p.market_id == market_id:
                return True
            if condition_id and (p.condition_id == condition_id
                                 or p.market_id == condition_id):
                return True
            if token_id and p.token_id == token_id:
                return True
        return False

    # ------------------------------------------------------------------
    # Profitability calculation
    # ------------------------------------------------------------------

    def calculate_profit(self, fill_price: float, shares: float,
                         fee_rate: float | None = None) -> dict:
        """Calculate exact net profit for a potential order.

        Returns dict with all components for logging.
        """
        cost = fill_price * shares
        gross = (1.0 - fill_price) * shares
        gas = self.config.estimated_gas_cost_usd
        effective_fee_rate = fee_rate if fee_rate is not None else self.config.platform_fee_rate
        fees = cost * effective_fee_rate
        net = gross - gas - fees

        return {
            "fill_price": fill_price,
            "shares": shares,
            "cost": cost,
            "gross_profit": gross,
            "gas_cost_usd": gas,
            "platform_fees": fees,
            "net_profit": net,
            "profitable": net >= self.config.min_net_profit,
        }

    # ------------------------------------------------------------------
    # Opportunity handling
    # ------------------------------------------------------------------

    async def handle_opportunity(self, opportunity: dict):
        """Handle a qualified opportunity from the market monitor.

        Runs hard-limit checks, profitability gate, and places the order.
        Accepts partial fills — takes whatever liquidity is available up to max.
        """
        market_id = opportunity["market_id"]
        token_id = opportunity["winning_token_id"]
        condition_id = opportunity["condition_id"]
        question = opportunity["question"]
        category = opportunity["category"]
        liquidity_usdc = opportunity.get("liquidity_usdc", 0.0)
        score = opportunity.get("score", 0.0)
        source = opportunity.get("source", "unknown")
        neg_risk = bool(opportunity.get("neg_risk", False))
        outcome_index = int(opportunity.get("outcome_index", -1))

        fill_price = opportunity.get("price_threshold", self.config.price_threshold)

        # --- Risk manager checks (if wired) ---

        if self.risk_manager:
            if self.risk_manager.is_blacklisted(market_id):
                self._log_skip(market_id, token_id, question, category,
                               "blacklisted")
                return

            allowed, cb_reason = self.risk_manager.is_trading_allowed(
                self.available_capital,
                wallet_balance=self._wallet_balance)
            if not allowed:
                self._log_skip(market_id, token_id, question, category,
                               "circuit_breaker=%s" % cb_reason)
                return

        # --- Hard limit checks (cheapest first) ---

        # 1. Max open positions
        if self.open_position_count >= self.config.max_open_positions:
            self._log_skip(market_id, token_id, question, category,
                           "max_positions_reached=%d" % self.config.max_open_positions)
            return

        # 1b. Per-category position cap
        cat_limit = self.config.max_positions_per_category.get(category)
        if cat_limit is not None:
            cat_count = sum(1 for p in self.positions.values()
                           if p.status in ("pending", "filled") and p.category == category)
            if cat_count >= cat_limit:
                self._log_skip(market_id, token_id, question, category,
                               "category_limit=%d/%d" % (cat_count, cat_limit))
                return

        # 2a. Per-market cooldown (prevents duplicate orders from delayed responses)
        now = time.time()
        last_by_market = self._market_order_timestamps.get(market_id, 0)
        last_by_token = self._market_order_timestamps.get(token_id, 0)
        if now - last_by_market < 60 or now - last_by_token < 60:
            self._log_skip(market_id, token_id, question, category,
                           "market_cooldown_60s")
            return

        # 2b. Duplicate market check (cross-references condition_id and token_id
        # to catch duplicates across Gamma numeric IDs and CLOB 0x condition IDs)
        if self._has_position_in_market(market_id, condition_id, token_id):
            self._log_skip(market_id, token_id, question, category,
                           "duplicate_market")
            return

        # 3. Capital floor — never deploy if wallet would drop below floor
        if self.available_capital <= 0:
            self._log_skip(market_id, token_id, question, category,
                           "no_available_capital")
            return

        # 4. Max 40% of total capital deployed (max_total_deployed is the 40%)
        # This is already enforced by available_capital check above since
        # max_total_deployed is set to 400 out of ~1000 capital

        # --- Size calculation with partial fill support ---

        # Max shares by capital limit per market (category-specific)
        max_position_usd = self.config.max_position_per_market.get(
            category, self.config.max_position_per_market.get("default", 50))
        # Low-temp weather markets are riskier (low can shift late in day),
        # so cap at default position size instead of the Weather-specific limit
        q_lower = question.lower() if question else ""
        if category == "Science/Weather" and ("lowest" in q_lower or "low temp" in q_lower):
            max_position_usd = min(max_position_usd,
                                   self.config.max_position_per_market.get("default", 50))
        max_shares_by_capital = max_position_usd / fill_price

        # Take whatever is available, up to our max
        # liquidity_usdc is the USDC value of asks at/below threshold
        available_shares = liquidity_usdc / fill_price if fill_price > 0 else 0
        shares = min(max_shares_by_capital, available_shares)

        # Also cap by remaining available capital
        max_shares_by_available = self.available_capital / fill_price
        shares = min(shares, max_shares_by_available)

        cost = shares * fill_price

        # 5. Minimum position size — gas makes small positions unprofitable
        if cost < self.config.min_position_size:
            self._log_skip(market_id, token_id, question, category,
                           "position_too_small=%.2f<%.2f" % (cost, self.config.min_position_size))
            return

        # --- Profitability gate ---

        # Use crypto short-term fee rate if the opportunity specified a lower threshold
        fee_rate = (self.config.crypto_short_term_fee_rate
                    if fill_price <= self.config.crypto_short_term_price_threshold
                    and fill_price < self.config.price_threshold
                    else None)
        profit = self.calculate_profit(fill_price, shares, fee_rate=fee_rate)

        if not profit["profitable"]:
            log_event(logger, "ORDER_REJECTED",
                      "Unprofitable: net=$%.2f (min=$%.2f) | "
                      "gross=$%.2f gas=$%.2f fees=$%.2f | %s" % (
                          profit["net_profit"], self.config.min_net_profit,
                          profit["gross_profit"], profit["gas_cost_usd"],
                          profit["platform_fees"], question[:50]),
                      market_id=market_id, token_id=token_id,
                      category=category, price=fill_price, size=shares,
                      details={
                          "reason": "unprofitable",
                          "net_profit": profit["net_profit"],
                          "gross_profit": profit["gross_profit"],
                          "gas_cost": profit["gas_cost_usd"],
                          "min_required": self.config.min_net_profit,
                      })
            return

        # --- Execute order ---

        # Set cooldown BEFORE placing order to prevent concurrent duplicates
        self._market_order_timestamps[market_id] = time.time()
        self._market_order_timestamps[token_id] = time.time()

        log_event(logger, "ORDER_ATTEMPT",
                  "BUY %.1f shares @ $%.2f = $%.2f | "
                  "net=$%.2f gross=$%.2f gas=$%.2f | score=%.1f | %s" % (
                      shares, fill_price, cost,
                      profit["net_profit"], profit["gross_profit"],
                      profit["gas_cost_usd"], score, question[:50]),
                  market_id=market_id, token_id=token_id,
                  category=category, price=fill_price, size=shares,
                  details={
                      "source": source,
                      "liquidity_usdc": liquidity_usdc,
                      "score": score,
                      **profit,
                  })

        await self._place_order_with_retry(
            market_id=market_id,
            token_id=token_id,
            condition_id=condition_id,
            question=question,
            category=category,
            price=fill_price,
            size=shares,
            profit=profit,
            source=source,
            score=score,
            neg_risk=neg_risk,
            outcome_index=outcome_index,
        )

    # ------------------------------------------------------------------
    # Order placement with retry
    # ------------------------------------------------------------------

    async def _place_order_with_retry(self, market_id: str, token_id: str,
                                       condition_id: str, question: str,
                                       category: str, price: float, size: float,
                                       profit: dict, source: str, score: float,
                                       neg_risk: bool = False,
                                       outcome_index: int = -1):
        """Place order with configurable retry on failure."""
        for attempt in range(1 + self.config.max_retries_on_failure):
            success = await self._place_order(
                market_id=market_id,
                token_id=token_id,
                condition_id=condition_id,
                question=question,
                category=category,
                price=price,
                size=size,
                profit=profit,
                source=source,
                score=score,
                attempt=attempt,
                neg_risk=neg_risk,
                outcome_index=outcome_index,
            )
            if success:
                if self.risk_manager:
                    self.risk_manager.report_transaction_success()
                    self.risk_manager.record_opportunity_traded(size * price)
                return

            if attempt < self.config.max_retries_on_failure:
                log_event(logger, "ORDER_RETRY",
                          "Retrying in %ds (attempt %d/%d)" % (
                              self.config.retry_delay_seconds,
                              attempt + 1, self.config.max_retries_on_failure),
                          market_id=market_id, token_id=token_id)
                await asyncio.sleep(self.config.retry_delay_seconds)
            else:
                log_event(logger, "ORDER_FAILED",
                          "All attempts exhausted for %s" % question[:60],
                          level="WARNING",
                          market_id=market_id, token_id=token_id,
                          category=category,
                          details={"attempts": attempt + 1, "reason": "retries_exhausted"})
                if self.risk_manager:
                    self.risk_manager.report_transaction_failure()

    async def _place_order(self, market_id: str, token_id: str, condition_id: str,
                           question: str, category: str, price: float, size: float,
                           profit: dict, source: str, score: float,
                           attempt: int = 0, neg_risk: bool = False,
                           outcome_index: int = -1) -> bool:
        """Place a single limit buy order. Returns True on success."""
        cost = size * price
        now = time.time()

        if self.config.dry_run:
            order_id = "dry_%s_%d_%d" % (market_id[:16], int(now), len(self.positions))

            pos = Position(
                market_id=market_id, token_id=token_id, condition_id=condition_id,
                question=question, category=category, order_id=order_id,
                price=price, size=size, cost=cost,
                gross_profit=profit["gross_profit"],
                gas_cost=profit["gas_cost_usd"],
                net_profit=profit["net_profit"],
                status="filled", placed_at=now, filled_at=now,
                source=source, score=score, neg_risk=neg_risk,
                outcome_index=outcome_index,
            )
            self.positions[order_id] = pos
            self._save_positions()

            log_event(logger, "ORDER_PLACED",
                      "[DRY RUN] BUY %.1f @ $%.2f = $%.2f | "
                      "net=$%.2f | %s" % (
                          size, price, cost,
                          profit["net_profit"], question[:50]),
                      market_id=market_id, token_id=token_id, price=price,
                      size=size, order_id=order_id, category=category,
                      details={
                          "dry_run": True, "source": source, "score": score,
                          **profit,
                      })
            return True

        # --- Live order via py-clob-client ---

        client = self._get_client()
        if client is None:
            logger.error("Cannot place order: CLOB client not initialized")
            return False

        try:
            from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY

            resp = client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=BUY,
                ),
                PartialCreateOrderOptions(
                    tick_size=self.config.tick_size,
                ),
            )

            order_id = resp.get("orderID", "unknown_%d" % int(now))
            status = resp.get("status", "unknown")
            success = status in ("matched", "live", "delayed")

            if not success:
                error_msg = resp.get("errorMsg", "")
                log_event(logger, "ORDER_REJECTED_BY_CLOB",
                          "CLOB rejected: status=%s error=%s | %s" % (
                              status, error_msg, question[:50]),
                          level="WARNING",
                          market_id=market_id, token_id=token_id,
                          category=category,
                          details={"response": resp, "attempt": attempt})
                # Clear cooldown on balance-related failures so the market
                # can be re-discovered on next poll when funds are available
                err_lower = error_msg.lower()
                if any(kw in err_lower for kw in (
                    "insufficient", "balance", "not enough", "allowance",
                )):
                    self._market_order_timestamps.pop(market_id, None)
                    self._market_order_timestamps.pop(token_id, None)
                    logger.info("Cleared cooldown for %s (balance issue, will retry on next poll)",
                                market_id)
                return False

            self._market_order_timestamps[market_id] = time.time()

            filled = status == "matched"
            pos = Position(
                market_id=market_id, token_id=token_id, condition_id=condition_id,
                question=question, category=category, order_id=order_id,
                price=price, size=size, cost=cost,
                gross_profit=profit["gross_profit"],
                gas_cost=profit["gas_cost_usd"],
                net_profit=profit["net_profit"],
                status="filled" if filled else "pending",
                placed_at=now,
                filled_at=now if filled else 0.0,
                source=source, score=score, neg_risk=neg_risk,
                outcome_index=outcome_index,
            )
            self.positions[order_id] = pos
            self._save_positions()

            log_event(logger, "ORDER_PLACED",
                      "BUY %.1f @ $%.2f = $%.2f | status=%s | "
                      "net=$%.2f | %s" % (
                          size, price, cost, status,
                          profit["net_profit"], question[:50]),
                      market_id=market_id, token_id=token_id, price=price,
                      size=size, order_id=order_id, category=category,
                      details={
                          "status": status, "source": source, "score": score,
                          "attempt": attempt, **profit,
                      })
            return True

        except Exception as e:
            log_event(logger, "ORDER_ERROR",
                      "Order failed: %s | %s" % (str(e)[:80], question[:50]),
                      level="ERROR",
                      market_id=market_id, token_id=token_id,
                      category=category,
                      details={"error": str(e), "attempt": attempt})
            # Clear cooldown on balance-related exceptions
            err_lower = str(e).lower()
            if any(kw in err_lower for kw in (
                "insufficient", "balance", "not enough", "allowance",
            )):
                self._market_order_timestamps.pop(market_id, None)
                self._market_order_timestamps.pop(token_id, None)
                logger.info("Cleared cooldown for %s (balance exception, will retry on next poll)",
                            market_id)
            return False

    # ------------------------------------------------------------------
    # Order monitoring and cancellation
    # ------------------------------------------------------------------

    async def run_periodic_sync(self):
        """Periodically sync wallet balance and detect external redemptions.

        Runs independently of the redeemer so that total_deployed and
        available_capital stay accurate even between redeemer cycles.
        """
        logger.info("Periodic sync started (interval=%ds)",
                     self.config.redeem_check_interval_seconds)
        while True:
            await asyncio.sleep(self.config.redeem_check_interval_seconds)
            try:
                self.sync_redeemed_positions()
                self.sync_wallet_balance()
                logger.info("Periodic sync: deployed=$%.2f available=$%.2f open=%d",
                            self.total_deployed, self.available_capital,
                            self.open_position_count)
            except Exception as e:
                logger.warning("Periodic sync failed: %s", e)

    async def monitor_open_orders(self):
        """Periodically check and cancel expired unfilled orders."""
        logger.info("Order monitor started (ttl=%ds, retry_delay=%ds)",
                     self.config.order_ttl_seconds, self.config.retry_delay_seconds)

        while True:
            await asyncio.sleep(10)

            now = time.time()
            expired = []

            for order_id, pos in self.positions.items():
                if pos.status != "pending":
                    continue
                if now - pos.placed_at > self.config.order_ttl_seconds:
                    expired.append(order_id)

            for order_id in expired:
                await self._cancel_order(order_id)

    async def _cancel_order(self, order_id: str):
        """Cancel an open order."""
        pos = self.positions.get(order_id)
        if not pos:
            return

        if self.config.dry_run:
            pos.status = "cancelled"
            self._save_positions()
            log_event(logger, "ORDER_CANCELLED",
                      "[DRY RUN] Cancelled expired order %s | %s" % (
                          order_id, pos.question[:50]),
                      market_id=pos.market_id, order_id=order_id,
                      details={"age_seconds": time.time() - pos.placed_at})
            return

        client = self._get_client()
        if client is None:
            return

        try:
            # Check if order already filled before cancelling — the CLOB may
            # have matched it between our last check and now.
            try:
                order = client.get_order(order_id)
                status = (order.get("status", "").lower() if order else "")
                if status == "matched":
                    pos.status = "filled"
                    pos.filled_at = time.time()
                    self._save_positions()
                    log_event(logger, "ORDER_FILLED",
                              "Order filled (caught at cancel): %s | %s" % (
                                  order_id, pos.question[:50]),
                              market_id=pos.market_id, order_id=order_id)
                    return
            except Exception:
                pass  # If check fails, proceed with cancel attempt

            from py_clob_client_v2.clob_types import OrderPayload
            client.cancel_order(OrderPayload(orderID=order_id))
            pos.status = "cancelled"
            self._save_positions()
            log_event(logger, "ORDER_CANCELLED",
                      "Cancelled expired order %s | %s" % (
                          order_id, pos.question[:50]),
                      market_id=pos.market_id, order_id=order_id,
                      details={"age_seconds": time.time() - pos.placed_at})
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)

    # ------------------------------------------------------------------
    # Cut-loss sell orders
    # ------------------------------------------------------------------

    def place_sell_order(self, order_id: str, sell_price: float) -> str | None:
        """Place a GTC limit sell order to exit a filled position.

        Returns the sell order ID on success, None on failure.
        """
        pos = self.positions.get(order_id)
        if not pos or pos.status != "filled":
            return None

        if self.config.dry_run:
            sell_id = "sell_dry_%s_%d" % (order_id[:16], int(time.time()))
            pos.cut_loss_order_id = sell_id
            pos.cut_loss_triggered_at = time.time()
            pos.status = "cut_loss"
            self._save_positions()
            log_event(logger, "CUTLOSS_SELL",
                      "[DRY RUN] SELL %.1f @ $%.2f | loss=$%.2f | %s" % (
                          pos.size, sell_price, pos.cost - pos.size * sell_price,
                          pos.question[:50]),
                      market_id=pos.market_id, order_id=order_id,
                      details={"sell_price": sell_price, "dry_run": True})
            return sell_id

        client = self._get_client()
        if client is None:
            logger.error("Cannot place sell: CLOB client not initialized")
            return None

        try:
            from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import SELL

            # Query actual token balance to avoid "not enough balance"
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            import math
            try:
                ba = client.get_balance_allowance(
                    BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=pos.token_id,
                        signature_type=2,
                    ))
                raw = float(ba.get("balance", 0))
                # Balance is in raw units (1e6); floor to 2 decimals
                actual_balance = math.floor(raw / 1e6 * 100) / 100
                logger.info("Token balance for sell: raw=%s actual=%.2f recorded=%.2f | %s",
                            ba.get("balance"), actual_balance, pos.size, pos.question[:40])
            except Exception as e:
                logger.warning("Balance query failed, using recorded size: %s", e)
                actual_balance = math.floor(pos.size * 100) / 100
            sell_size = min(actual_balance, pos.size)
            if sell_size < 0.01:
                logger.warning("No token balance to sell for %s", pos.question[:50])
                return None
            resp = client.create_and_post_order(
                OrderArgs(
                    token_id=pos.token_id,
                    price=sell_price,
                    size=sell_size,
                    side=SELL,
                ),
                PartialCreateOrderOptions(
                    tick_size=self.config.tick_size,
                ),
            )

            sell_id = resp.get("orderID", "")
            status = resp.get("status", "unknown")
            success = status in ("matched", "live", "delayed")

            if not success:
                log_event(logger, "CUTLOSS_SELL_REJECTED",
                          "Sell rejected: status=%s | %s" % (status, pos.question[:50]),
                          level="WARNING", market_id=pos.market_id, order_id=order_id,
                          details={"response": resp})
                return None

            pos.cut_loss_order_id = sell_id
            pos.cut_loss_triggered_at = time.time()

            if status == "matched":
                # Immediately filled
                actual_proceeds = pos.size * sell_price
                actual_loss = pos.cost - actual_proceeds
                pos.status = "cut_loss"
                pos.net_profit = -actual_loss
                if self.risk_manager:
                    self.risk_manager.record_trade_result(
                        net_profit=-actual_loss, gas_cost=0.0)

            self._save_positions()
            log_event(logger, "CUTLOSS_SELL",
                      "SELL %.1f @ $%.2f | status=%s | %s" % (
                          pos.size, sell_price, status, pos.question[:50]),
                      market_id=pos.market_id, order_id=order_id,
                      details={"sell_id": sell_id, "sell_price": sell_price,
                               "status": status})
            return sell_id

        except Exception as e:
            log_event(logger, "CUTLOSS_SELL_ERROR",
                      "Sell failed: %s | %s" % (str(e)[:80], pos.question[:50]),
                      level="ERROR", market_id=pos.market_id, order_id=order_id)
            return None

    def cancel_sell_order(self, order_id: str) -> bool:
        """Cancel an outstanding cut-loss sell order."""
        pos = self.positions.get(order_id)
        if not pos or not pos.cut_loss_order_id:
            return False

        if self.config.dry_run:
            pos.cut_loss_order_id = ""
            pos.cut_loss_triggered_at = 0.0
            self._save_positions()
            return True

        client = self._get_client()
        if client is None:
            return False

        try:
            from py_clob_client_v2.clob_types import OrderPayload
            client.cancel_order(OrderPayload(orderID=pos.cut_loss_order_id))
            pos.cut_loss_order_id = ""
            pos.cut_loss_triggered_at = 0.0
            self._save_positions()
            return True
        except Exception:
            logger.exception("Failed to cancel sell order %s", pos.cut_loss_order_id)
            return False

    def mark_cut_loss(self, order_id: str, sell_price: float):
        """Mark a position as cut-loss after sell order fills."""
        pos = self.positions.get(order_id)
        if not pos:
            return
        actual_proceeds = pos.size * sell_price
        actual_loss = pos.cost - actual_proceeds
        pos.status = "cut_loss"
        pos.net_profit = -actual_loss
        self._save_positions()
        log_event(logger, "CUTLOSS_COMPLETED",
                  "Cut loss: sold %.1f @ $%.2f | loss=$%.2f | %s" % (
                      pos.size, sell_price, actual_loss, pos.question[:50]),
                  level="WARNING",
                  market_id=pos.market_id, order_id=order_id,
                  details={"entry_price": pos.price, "sell_price": sell_price,
                           "loss": round(actual_loss, 2)})

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log_skip(self, market_id: str, token_id: str, question: str,
                  category: str, reason: str):
        """Log a skipped opportunity."""
        log_event(logger, "ORDER_SKIPPED",
                  "Skipped: %s | %s" % (reason, question[:50]),
                  market_id=market_id, token_id=token_id,
                  category=category,
                  details={"reason": reason})

    # ------------------------------------------------------------------
    # Position management for redeemer
    # ------------------------------------------------------------------

    def get_redeemable_positions(self) -> list[Position]:
        """Get positions that are filled and ready for redemption."""
        return [p for p in self.positions.values() if p.status == "filled"]

    def mark_redeemed(self, order_id: str):
        """Mark a position as redeemed."""
        if order_id in self.positions:
            self.positions[order_id].status = "redeemed"
            self._save_positions()

    def mark_resolved_loss(self, order_id: str):
        """Mark a position as a confirmed loss (the outcome we bought lost).

        The market resolved against us — our shares are worth $0 and there is
        nothing to redeem on-chain. Record the full cost as a loss and persist.
        """
        pos = self.positions.get(order_id)
        if not pos:
            return
        pos.status = "resolved_loss"
        pos.net_profit = -pos.cost
        self._save_positions()
        log_event(logger, "RESOLVED_LOSS",
                  "Position lost: %s | cost=$%.2f" % (pos.question[:60], pos.cost),
                  level="WARNING",
                  market_id=pos.market_id, order_id=order_id,
                  details={"cost": pos.cost, "outcome_index": pos.outcome_index})
        if self.risk_manager:
            try:
                self.risk_manager.record_trade_result(
                    net_profit=-pos.cost, gas_cost=0.0,
                    deployed=self.total_deployed)
            except Exception:
                logger.exception("Failed to record loss with risk_manager")

    # ------------------------------------------------------------------
    # Summary and reporting
    # ------------------------------------------------------------------

    def get_summary(self) -> dict:
        """Get current position summary with profitability stats."""
        total_net = sum(p.net_profit for p in self.positions.values()
                        if p.status in ("filled", "redeemed"))
        total_gross = sum(p.gross_profit for p in self.positions.values()
                          if p.status in ("filled", "redeemed"))
        total_gas = sum(p.gas_cost for p in self.positions.values()
                        if p.status in ("filled", "redeemed"))

        return {
            "open_positions": self.open_position_count,
            "total_deployed": round(self.total_deployed, 2),
            "available_capital": round(self.available_capital, 2),
            "total_net_profit": round(total_net, 2),
            "total_gross_profit": round(total_gross, 2),
            "total_gas_cost": round(total_gas, 2),
            "positions": {
                oid: {
                    "market_id": p.market_id,
                    "question": p.question[:60],
                    "category": p.category,
                    "price": p.price,
                    "size": round(p.size, 2),
                    "cost": round(p.cost, 2),
                    "net_profit": round(p.net_profit, 2),
                    "status": p.status,
                    "source": p.source,
                    "score": p.score,
                }
                for oid, p in self.positions.items()
            },
        }
