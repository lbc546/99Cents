"""Market scanning module for the Polymarket settlement arbitrage bot.

Three concurrent tasks:
  1. Gamma API poller — paginated, incremental polling for newly-resolved markets
  2. WebSocket listener — real-time market_resolved and price events
  3. Watchlist scanner — periodic re-evaluation of pending markets

Data-analysis-driven filters:
  - Crypto (3hr median settlement) and Esports (2hr) categories only
  - 33% of markets had prices <$0.50 near close → surprise pricing filter
  - 30-minute grace period after end_date (UMA 2hr challenge window)
  - Subjective language detection for UMA dispute risk
  - Liquidity check: $20 USDC minimum at threshold price

Rate limiting:
  - CLOB API: 85 req/min (limit is 100) via semaphore + min interval
  - Gamma API: 120 req/min (defensive) via semaphore + min interval
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp
import websockets

from bot.config import BotConfig
from bot.filters import (
    check_end_date_timing,
    check_liquidity_at_threshold,
    get_winning_token,
    has_subjective_language,
    infer_category,
    is_crypto_short_term,
    is_live_event_market,
    is_weather_temp_known,
    parse_json_field,
    score_opportunity,
    was_below_threshold_pre_close,
)
from bot.logger import log_event

logger = logging.getLogger("arb_bot")


def _parse_ts(iso_str: str) -> float:
    """Convert ISO datetime string to Unix timestamp. Returns 0.0 on error."""
    if not iso_str:
        return 0.0
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


@dataclass
class WatchlistEntry:
    """Tracks a market whose end_date has passed but is not yet settled."""

    market_id: str
    question: str
    category: str
    end_date: str
    end_date_ts: float
    token_ids: list[str] = field(default_factory=list)
    winning_token: str = ""
    added_at: float = 0.0
    last_evaluated_at: float = 0.0
    resolved: bool = False


class MarketMonitor:
    """Scans Polymarket for settlement arbitrage opportunities."""

    def __init__(self, config: BotConfig, on_opportunity):
        """
        Args:
            config: Bot configuration.
            on_opportunity: Async callback(opportunity_dict) when qualified.
        """
        self.config = config
        self.on_opportunity = on_opportunity

        # State
        self._watchlist: dict[str, WatchlistEntry] = {}
        self._seen_market_ids: set[str] = set()
        self._subscribed_tokens: set[str] = set()
        self._last_poll_time: str = ""
        self._ws_ref = None  # live WebSocket handle for dynamic subscribes
        self._token_to_market: dict[str, str] = {}  # token_id → market_id
        self.risk_manager = None  # Set by main.py
        self.dry_run_reporter = None  # Set by main.py

        # Shared HTTP session
        self._session: aiohttp.ClientSession | None = None

        # Rate limiters — semaphore + min interval
        self._clob_sem = asyncio.Semaphore(1)
        self._clob_min_interval = 60.0 / config.clob_rate_limit_per_min
        self._last_clob_call: float = 0.0

        self._gamma_sem = asyncio.Semaphore(1)
        self._gamma_min_interval = 60.0 / config.gamma_rate_limit_per_min
        self._last_gamma_call: float = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Clean up HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Rate-limited HTTP wrappers
    # ------------------------------------------------------------------

    async def _rate_limited_clob_get(self, url: str, params: dict | None = None) -> dict | None:
        """Rate-limited GET to CLOB API with retry on 429."""
        async with self._clob_sem:
            elapsed = time.monotonic() - self._last_clob_call
            if elapsed < self._clob_min_interval:
                await asyncio.sleep(self._clob_min_interval - elapsed)

            session = await self._get_session()
            for attempt in range(3):
                try:
                    async with session.get(url, params=params,
                                           timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        self._last_clob_call = time.monotonic()
                        if resp.status == 429:
                            wait = (2 ** attempt) * 5
                            logger.warning("CLOB 429 rate limited, waiting %ds", wait)
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            return None
                        return await resp.json()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.exception("CLOB request failed: %s", url)
            return None

    async def _rate_limited_gamma_get(self, url: str, params: dict | None = None) -> dict | list | None:
        """Rate-limited GET to Gamma API with retry on 429."""
        async with self._gamma_sem:
            elapsed = time.monotonic() - self._last_gamma_call
            if elapsed < self._gamma_min_interval:
                await asyncio.sleep(self._gamma_min_interval - elapsed)

            session = await self._get_session()
            for attempt in range(3):
                try:
                    async with session.get(url, params=params,
                                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        self._last_gamma_call = time.monotonic()
                        if resp.status == 429:
                            wait = (2 ** attempt) * 5
                            logger.warning("Gamma 429 rate limited, waiting %ds", wait)
                            await asyncio.sleep(wait)
                            continue
                        if resp.status != 200:
                            return None
                        return await resp.json()
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.exception("Gamma request failed: %s", url)
            return None

    # ------------------------------------------------------------------
    # Task 1: Gamma API Poller
    # ------------------------------------------------------------------

    async def run_gamma_poller(self):
        """Poll Gamma API for markets in the settlement-lag window."""
        logger.info("Gamma poller started (interval=%ds, max_pages=%d)",
                     self.config.polling_interval_seconds, self.config.gamma_poll_max_pages)

        while True:
            try:
                await self._poll_gamma_active()
                await self._poll_gamma_upcoming()
                await self._poll_gamma_closed()
            except Exception:
                logger.exception("Gamma poll error")
            await asyncio.sleep(self.config.polling_interval_seconds)

    async def _poll_gamma_active(self):
        """Fetch active (not-yet-closed) markets with past endDate.

        This is the PRIMARY source of opportunities: events have concluded
        but the oracle hasn't resolved yet, so the CLOB book is still active
        with shares trading at $0.95-0.99.
        """
        url = f"{self.config.gamma_base_url}/markets"
        offset = 0
        pages_fetched = 0
        new_count = 0

        # Window: endDate between 48h ago and now (past-endDate active markets)
        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(hours=self.config.watchlist_max_age_hours)).isoformat()
        window_end = now.isoformat()

        while pages_fetched < self.config.gamma_poll_max_pages:
            params = {
                "active": "true",
                "closed": "false",
                "limit": self.config.gamma_poll_limit,
                "offset": offset,
                "end_date_min": window_start,
                "end_date_max": window_end,
            }

            markets = await self._rate_limited_gamma_get(url, params)
            if not markets:
                break

            for market in markets:
                market_id = market.get("id", "")
                if market_id in self._seen_market_ids:
                    continue
                self._seen_market_ids.add(market_id)

                question = market.get("question", "")
                category = infer_category(question)
                if category in self.config.blocked_categories:
                    continue

                new_count += 1
                self._add_to_watchlist(market)
                await self._evaluate_market(market, source="gamma_active")

            if len(markets) < self.config.gamma_poll_limit:
                break

            offset += self.config.gamma_poll_limit
            pages_fetched += 1

        if new_count:
            logger.info("Gamma active poll: %d new pre-resolution markets (pages=%d)",
                         new_count, pages_fetched + 1)

    async def _poll_gamma_upcoming(self):
        """Fetch markets closing in the next 12 hours.

        Catches weather/crypto markets where the outcome may already be known
        (temperature recorded, price settled) but the market end date hasn't
        passed yet. Weather markets often have end-of-day endDate even though
        the high temp is known by mid-afternoon. These often have a YES token
        at $0.95-0.99 with liquidity still available.
        """
        url = f"{self.config.gamma_base_url}/markets"
        offset = 0
        pages_fetched = 0
        new_count = 0

        now = datetime.now(timezone.utc)
        window_start = now.isoformat()
        window_end = (now + timedelta(hours=12)).isoformat()

        while pages_fetched < self.config.gamma_poll_max_pages:
            params = {
                "active": "true",
                "closed": "false",
                "limit": self.config.gamma_poll_limit,
                "offset": offset,
                "end_date_min": window_start,
                "end_date_max": window_end,
            }

            markets = await self._rate_limited_gamma_get(url, params)
            if not markets:
                break

            for market in markets:
                market_id = market.get("id", "")
                # Don't add to _seen_market_ids — upcoming markets should be
                # re-evaluated each cycle as prices change near resolution.

                question = market.get("question", "")
                category = infer_category(question)
                if category in self.config.blocked_categories:
                    continue

                new_count += 1
                self._add_to_watchlist(market)
                await self._evaluate_market(market, source="gamma_upcoming")

            if len(markets) < self.config.gamma_poll_limit:
                break

            offset += self.config.gamma_poll_limit
            pages_fetched += 1

        if new_count:
            logger.info("Gamma upcoming poll: %d markets closing within 12h (pages=%d)",
                         new_count, pages_fetched + 1)

    async def _poll_gamma_closed(self):
        """Fetch recently closed markets (already resolved by oracle).

        Secondary source: catches markets that resolved while we weren't
        looking, in case the book hasn't been torn down yet.
        """
        url = f"{self.config.gamma_base_url}/markets"
        offset = 0
        pages_fetched = 0
        new_count = 0

        while pages_fetched < self.config.gamma_poll_max_pages:
            params = {
                "closed": "true",
                "limit": self.config.gamma_poll_limit,
                "offset": offset,
                "order": "closedTime",
                "ascending": "false",
            }

            markets = await self._rate_limited_gamma_get(url, params)
            if not markets:
                break

            hit_cutoff = False
            for market in markets:
                closed_time = market.get("closedTime") or market.get("endDate") or ""

                if self._last_poll_time and closed_time and closed_time < self._last_poll_time:
                    hit_cutoff = True
                    break

                market_id = market.get("id", "")
                # Don't add to _seen_market_ids — resolved markets should be
                # re-evaluated each cycle as liquidity can appear after first check.
                # The _last_poll_time cutoff prevents re-processing old markets.

                question = market.get("question", "")
                category = infer_category(question)
                if category in self.config.blocked_categories:
                    continue

                new_count += 1
                await self._evaluate_market(market, source="gamma_closed")

            if hit_cutoff or len(markets) < self.config.gamma_poll_limit:
                break

            offset += self.config.gamma_poll_limit
            pages_fetched += 1

        self._last_poll_time = datetime.now(timezone.utc).isoformat()

        if new_count:
            logger.info("Gamma closed poll: %d new resolved markets (pages=%d)",
                         new_count, pages_fetched + 1)

        if new_count:
            logger.info("Gamma poll: %d new target-category markets (pages=%d, watchlist=%d)",
                         new_count, pages_fetched + 1, len(self._watchlist))

    # ------------------------------------------------------------------
    # Task 2: WebSocket Listener
    # ------------------------------------------------------------------

    async def run_websocket(self):
        """Maintain persistent WebSocket connection for real-time events."""
        backoff = self.config.ws_reconnect_base_seconds

        while True:
            try:
                await self._ws_connect()
                backoff = self.config.ws_reconnect_base_seconds
            except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
                logger.warning("WebSocket disconnected: %s (reconnect in %ds)", e, backoff)
            except Exception:
                logger.exception("WebSocket error (reconnect in %ds)", backoff)

            self._ws_ref = None
            self._subscribed_tokens.clear()
            if self.risk_manager:
                self.risk_manager.set_ws_connected(False)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _ws_connect(self):
        """Connect to market WebSocket and process events.

        Polymarket WS requires:
        - Application-level "PING" text every 10s (keeps server alive)
        - Server responds with "PONG" text (used for dead-connection detection)
        - Protocol-level pings disabled (server doesn't support them)
        - Non-empty assets_ids on initial subscription
        - custom_feature_enabled=true for market_resolved events
        """
        logger.info("Connecting to WebSocket: %s", self.config.ws_market_url)

        async with websockets.connect(
            self.config.ws_market_url,
            ping_interval=None,   # Server doesn't support protocol-level pings
            ping_timeout=None,
            close_timeout=10,
            max_size=10 * 1024 * 1024,  # 10MB
        ) as ws:
            self._ws_ref = ws
            logger.info("WebSocket connected")
            if self.risk_manager:
                self.risk_manager.set_ws_connected(True)

            # Build initial subscription with watchlist tokens (if any)
            initial_tokens = set()
            for entry in self._watchlist.values():
                initial_tokens.update(entry.token_ids)

            # Polymarket needs at least one asset_id on initial subscribe.
            # Use a well-known high-volume token as a seed if watchlist is empty.
            # This gets us connected and receiving market_resolved events globally.
            if not initial_tokens:
                # Subscribe to a placeholder — the important part is
                # custom_feature_enabled which gives us market_resolved events.
                # We'll dynamically subscribe to real tokens as watchlist populates.
                seed_token = "0"  # minimal valid value; server accepts it
                initial_tokens = {seed_token}

            await ws.send(json.dumps({
                "type": "market",
                "assets_ids": list(initial_tokens),
                "custom_feature_enabled": True,
            }))
            self._subscribed_tokens.update(initial_tokens)
            logger.info("WebSocket initial subscribe: %d tokens, custom_feature=True",
                         len(initial_tokens))

            # Run ping sender and message receiver concurrently
            ping_task = asyncio.create_task(
                self._ws_ping_loop(ws), name="ws_ping")
            recv_task = asyncio.create_task(
                self._ws_recv_loop(ws), name="ws_recv")

            try:
                # If either task exits, we're done (connection lost or error)
                done, pending = await asyncio.wait(
                    [ping_task, recv_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # Propagate any exception from the completed task
                for task in done:
                    task.result()
            finally:
                for task in [ping_task, recv_task]:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

    async def _ws_ping_loop(self, ws):
        """Send app-level PING and detect dead connections via PONG tracking."""
        interval = self.config.ws_ping_interval_seconds
        miss_limit = 3  # Close connection after 3 missed PONGs (~30s)
        self._ws_last_pong = time.time()
        while True:
            await asyncio.sleep(interval)
            # Check if we've received any PONG recently
            elapsed = time.time() - self._ws_last_pong
            if elapsed > interval * miss_limit:
                logger.warning("No PONG received in %.0fs, closing connection", elapsed)
                await ws.close(1000, "pong timeout")
                return
            await ws.send("PING")

    async def _ws_recv_loop(self, ws):
        """Receive and dispatch WebSocket messages."""
        async for raw_msg in ws:
            # Track PONG for dead-connection detection
            if raw_msg == "PONG":
                self._ws_last_pong = time.time()
                continue
            try:
                data = json.loads(raw_msg)
            except (json.JSONDecodeError, TypeError):
                continue
            # Polymarket WS can send arrays of events
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        await self._handle_ws_event(item)
            elif isinstance(data, dict):
                await self._handle_ws_event(data)

    async def _handle_ws_event(self, data: dict):
        """Route WebSocket events."""
        event_type = data.get("event_type", "")

        if event_type == "market_resolved":
            await self._handle_resolution(data)
        elif event_type == "last_trade_price":
            await self._handle_trade_price(data)
        elif event_type == "best_bid_ask":
            await self._handle_best_bid_ask(data)

    async def _handle_resolution(self, data: dict):
        """Handle a market_resolved WebSocket event."""
        market_id = data.get("market", "")
        winning_asset = data.get("winning_asset_id", "")
        winning_outcome = data.get("winning_outcome", "")

        log_event(logger, "MARKET_RESOLVED",
                  f"Market {market_id} resolved: {winning_outcome}",
                  market_id=market_id, token_id=winning_asset)

        # Update watchlist entry if it exists
        if market_id in self._watchlist:
            self._watchlist[market_id].resolved = True
            self._watchlist[market_id].winning_token = winning_asset

        # Fetch full market details and evaluate
        try:
            market_data = await self._fetch_market_detail(market_id)
            if market_data:
                await self._evaluate_market(market_data, source="websocket_resolved")
        except Exception:
            logger.exception("Error processing resolution for %s", market_id)

    async def _handle_trade_price(self, data: dict):
        """Handle last_trade_price — trigger evaluation if price at threshold."""
        asset_id = data.get("asset_id", "")
        price = data.get("price")
        if price is None:
            return

        try:
            price_f = float(price)
        except (ValueError, TypeError):
            return

        # Only trigger evaluation if price is near the threshold (within ~5 cents)
        # Prices far below threshold (e.g. $0.05) are not actionable
        if price_f > self.config.price_threshold or price_f < self.config.price_threshold - 0.05:
            return

        # Look up which market this token belongs to
        market_id = self._token_to_market.get(asset_id)
        if not market_id:
            return

        entry = self._watchlist.get(market_id)
        if not entry or entry.resolved:
            return

        log_event(logger, "PRICE_AT_THRESHOLD",
                  f"Token {asset_id} at {price_f:.4f} (market {market_id})",
                  token_id=asset_id, price=price_f, market_id=market_id)

        # Re-fetch and evaluate
        market_data = await self._fetch_market_detail(market_id)
        if market_data:
            await self._evaluate_market(market_data, source="ws_price_trigger")

    async def _handle_best_bid_ask(self, data: dict):
        """Handle best_bid_ask events — check if ask dropped to threshold."""
        asset_id = data.get("asset_id", "")
        best_ask = data.get("best_ask")
        if best_ask is None:
            return

        try:
            ask_f = float(best_ask)
        except (ValueError, TypeError):
            return

        if ask_f <= self.config.price_threshold:
            market_id = self._token_to_market.get(asset_id)
            if market_id and market_id in self._watchlist:
                log_event(logger, "ASK_AT_THRESHOLD",
                          f"Best ask {ask_f:.4f} for {asset_id}",
                          level="DEBUG", token_id=asset_id, price=ask_f)

    # ------------------------------------------------------------------
    # Task 3: Watchlist Scanner
    # ------------------------------------------------------------------

    async def run_watchlist_scanner(self):
        """Periodically re-evaluate watchlist entries for resolution."""
        logger.info("Watchlist scanner started (interval=%ds, cooldown=%ds, max_checks=%d)",
                     self.config.watchlist_scan_interval_seconds,
                     self.config.watchlist_recheck_cooldown_seconds,
                     self.config.watchlist_max_checks_per_cycle)

        await asyncio.sleep(10)  # let Gamma poller populate watchlist first

        while True:
            try:
                await self._scan_watchlist()
            except Exception:
                logger.exception("Watchlist scan error")
            await asyncio.sleep(self.config.watchlist_scan_interval_seconds)

    async def _scan_watchlist(self):
        """Iterate watchlist, evict stale entries, re-evaluate pending ones."""
        now = time.time()
        evicted = 0
        checked = 0
        to_remove = []

        for market_id, entry in list(self._watchlist.items()):
            # Skip already resolved
            if entry.resolved:
                continue

            # Evict entries older than max age
            age_hours = (now - entry.added_at) / 3600
            if age_hours > self.config.watchlist_max_age_hours:
                to_remove.append(market_id)
                evicted += 1
                if self.risk_manager:
                    self.risk_manager.record_settlement_timeout(
                        market_id, entry.question)

                continue

            # Skip if checked recently (cooldown)
            if entry.last_evaluated_at > 0:
                since_check = now - entry.last_evaluated_at
                if since_check < self.config.watchlist_recheck_cooldown_seconds:
                    continue

            # Cap checks per cycle
            if checked >= self.config.watchlist_max_checks_per_cycle:
                break

            # Re-fetch market detail
            market_data = await self._fetch_market_detail(market_id)
            entry.last_evaluated_at = now
            checked += 1

            if not market_data:
                continue

            # Check if now resolved
            winning_idx, _, winning_token = get_winning_token(market_data)
            if winning_idx is not None:
                entry.resolved = True
                entry.winning_token = winning_token

            # Always evaluate — pre-resolution strategy catches high-confidence
            # markets before official resolution (when CLOB book is still active)
            await self._evaluate_market(market_data, source="watchlist_scan")

        for mid in to_remove:
            del self._watchlist[mid]

        if checked or evicted:
            logger.debug("Watchlist scan: checked=%d, evicted=%d, total=%d",
                         checked, evicted, len(self._watchlist))

    # ------------------------------------------------------------------
    # Watchlist Management
    # ------------------------------------------------------------------

    def _add_to_watchlist(self, market: dict):
        """Add a market to the watchlist (idempotent)."""
        market_id = market.get("id", "")
        if market_id in self._watchlist:
            return

        question = market.get("question", "")
        end_date = market.get("endDate", "") or market.get("closedTime", "")
        token_ids = parse_json_field(market.get("clobTokenIds", "[]"))

        entry = WatchlistEntry(
            market_id=market_id,
            question=question,
            category=infer_category(question),
            end_date=end_date,
            end_date_ts=_parse_ts(end_date),
            token_ids=token_ids,
            added_at=time.time(),
        )
        self._watchlist[market_id] = entry

        # Map token_ids → market_id for WS event lookup
        for tid in token_ids:
            self._token_to_market[tid] = market_id

        # Subscribe tokens on live WebSocket
        if self._ws_ref is not None:
            asyncio.create_task(self._subscribe_tokens(token_ids))

        log_event(logger, "WATCHLIST_ADDED",
                  f"Watchlist: {question[:70]} (cat={entry.category}, end={end_date[:16]})",
                  market_id=market_id, category=entry.category)

    async def _subscribe_tokens(self, token_ids: list[str]):
        """Subscribe token IDs on the live WebSocket."""
        if self._ws_ref is None:
            return
        new_tokens = [t for t in token_ids if t not in self._subscribed_tokens]
        if not new_tokens:
            return
        try:
            await self._ws_ref.send(json.dumps({
                "type": "market",
                "assets_ids": new_tokens,
                "operation": "subscribe",
            }))
            self._subscribed_tokens.update(new_tokens)
        except Exception:
            logger.debug("Failed to subscribe tokens (WS may be disconnected)")

    # ------------------------------------------------------------------
    # Market Evaluation — 10-step filter chain
    # ------------------------------------------------------------------

    async def _evaluate_market(self, market: dict, source: str):
        """Full filter chain + scoring + opportunity emission.

        Filter order: cheapest (free) checks first, HTTP calls last.
        """
        market_id = market.get("id", "")
        question = market.get("question", "")
        description = market.get("description", "") or ""
        end_date = market.get("endDate", "") or market.get("closedTime", "")

        # Step 0: Risk manager — opportunity counting + blacklist [FREE]
        if self.risk_manager:
            self.risk_manager.record_opportunity_seen()
            if self.risk_manager.is_blacklisted(market_id):
                self._log_filtered(market_id, question, "", source, "blacklisted")
                return

        # Step 1: Category check [FREE]
        category = infer_category(question)
        if category in self.config.blocked_categories:
            self._log_filtered(market_id, question, category, source,
                               f"category={category}")
            return

        # Step 2: Crypto filter [FREE]
        # Block long-term crypto (daily/weekly) — too volatile before close.
        # Only allow short-term (5min/15min) crypto through.
        _is_crypto_st = is_crypto_short_term(question)
        if category == "Crypto" and not _is_crypto_st:
            self._log_filtered(market_id, question, category, source,
                               "crypto_long_term")
            return
        effective_threshold = (self.config.crypto_short_term_price_threshold
                               if _is_crypto_st else self.config.price_threshold)

        # Step 3: Resolution / high-confidence check [FREE]
        best_price = 1.0  # Track leading price for Step 5 upcoming filter
        winning_idx, winning_outcome, winning_token = get_winning_token(market)
        if winning_idx is None or not winning_token:
            best_price = 0.0
            # Not officially resolved — check for high-confidence leading outcome.
            # This catches the pre-resolution window where the CLOB book is still
            # active but the outcome is effectively decided (price >= 0.95).
            outcome_prices = parse_json_field(market.get("outcomePrices", "[]"))
            token_ids = parse_json_field(market.get("clobTokenIds", "[]"))
            outcomes = parse_json_field(market.get("outcomes", "[]"))

            best_idx, best_price = None, 0.0
            for i, price_str in enumerate(outcome_prices):
                try:
                    p = float(price_str)
                    if p > best_price:
                        best_price = p
                        best_idx = i
                except (ValueError, TypeError):
                    continue

            # Require leader >= threshold AND all others <= (1 - threshold)
            # This ensures decisive outcome, not split confidence
            max_other = 1.0 - self.config.high_confidence_threshold
            others_low = all(
                float(outcome_prices[i]) <= max_other
                for i in range(len(outcome_prices))
                if i != best_idx
            ) if best_idx is not None and len(outcome_prices) > 1 else False

            if (best_idx is not None
                    and best_price >= self.config.high_confidence_threshold
                    and others_low
                    and best_idx < len(token_ids) and token_ids[best_idx]):
                winning_idx = best_idx
                winning_outcome = outcomes[best_idx] if best_idx < len(outcomes) else "Unknown"
                winning_token = token_ids[best_idx]
            else:
                reason = f"not_resolved_low={best_price:.2f}" if best_price > 0 else "not_resolved"
                self._log_filtered(market_id, question, category, source, reason)
                return

        # Step 4a: Subjective language [FREE]
        if has_subjective_language(question, description):
            self._log_filtered(market_id, question, category, source,
                               "subjective_language")
            return

        # Step 4b: Live-event behavior markets [FREE]
        if is_live_event_market(question):
            self._log_filtered(market_id, question, category, source,
                               "live_event_market")
            return

        # Step 5: End date timing — category-aware grace period [FREE]
        # gamma_closed: skip timing (oracle already resolved).
        # gamma_upcoming weather: endDate < 4hr (afternoon peak likely passed).
        # gamma_upcoming non-weather: endDate < 1hr AND confidence >= 0.99.
        #
        # Weather guard (all sources except gamma_closed): check if it's
        # past 5 PM local time in the market's city. Daily high temps aren't
        # finalized until late afternoon. Uses city-to-timezone mapping.
        if category == "Science/Weather" and source != "gamma_closed":
            if not is_weather_temp_known(question):
                self._log_filtered(market_id, question, category, source,
                                   "weather_before_3pm_local")
                return

        if source == "gamma_upcoming":
            try:
                end_dt = datetime.fromisoformat(
                    end_date.replace("Z", "+00:00")) if end_date else None
                if end_dt:
                    hours_to_end = (end_dt - datetime.now(
                        timezone.utc)).total_seconds() / 3600
                    if category != "Science/Weather":
                        # Non-weather: endDate within 1 hour
                        if hours_to_end > 1:
                            self._log_filtered(market_id, question, category, source,
                                               "upcoming_too_early=%.0fmin" % (hours_to_end * 60))
                            return
            except (ValueError, TypeError):
                pass
            # Non-weather also requires >= 0.99 confidence
            if category != "Science/Weather" and best_price < 0.99:
                self._log_filtered(market_id, question, category, source,
                                   "upcoming_low_confidence=%.2f" % best_price)
                return
        if source not in ("gamma_closed", "gamma_upcoming"):
            grace = self.config.end_date_grace_by_category.get(
                category, self.config.end_date_grace_minutes
            )
            timing_ok, timing_reason = check_end_date_timing(end_date, grace)
            if not timing_ok:
                self._log_filtered(market_id, question, category, source,
                                   timing_reason)
                return

        # Step 6: UMA dispute check [FREE — reads Gamma fields]
        if self._check_uma_dispute(market):
            self._log_filtered(market_id, question, category, source,
                               "uma_dispute")
            return

        # Step 7: Order book liquidity [HTTP — CLOB]
        orderbook = await self._fetch_orderbook(winning_token)
        asks = orderbook.get("asks", []) if orderbook else []
        liquidity_usdc, passes_liq = check_liquidity_at_threshold(
            asks, effective_threshold, self.config.min_liquidity_usdc
        )
        if not passes_liq:
            self._log_filtered(market_id, question, category, source,
                               f"low_liquidity_usdc={liquidity_usdc:.2f}")
            return

        # Step 8: Price history — surprise pricing check [HTTP — CLOB]
        # Weather markets are inherently uncertain before the day ends,
        # so use a much lower cutoff (0.10) to avoid filtering valid trades.
        history = await self._fetch_price_history(winning_token)
        surprise_cutoff = (0.10 if category == "Science/Weather"
                           else self.config.surprise_price_cutoff)
        if was_below_threshold_pre_close(
            history, end_date,
            self.config.price_history_window_hours,
            surprise_cutoff,
        ):
            self._log_filtered(market_id, question, category, source,
                               "surprise_price_history")
            return

        # Step 9: Score the opportunity
        end_ts = _parse_ts(end_date)
        minutes_since_end = (time.time() - end_ts) / 60.0 if end_ts > 0 else 0.0
        opp_score = score_opportunity(category, liquidity_usdc, minutes_since_end)

        # Step 10: Emit opportunity
        opportunity = {
            "market_id": market_id,
            "question": question,
            "category": category,
            "winning_outcome": winning_outcome,
            "winning_token_id": winning_token,
            "condition_id": market.get("conditionId", ""),
            "liquidity_usdc": liquidity_usdc,
            "score": opp_score,
            "minutes_since_end": minutes_since_end,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price_threshold": effective_threshold,
        }

        log_event(logger, "OPPORTUNITY_QUALIFIED",
                  f"QUALIFIED score={opp_score:.1f} liq=${liquidity_usdc:.0f} "
                  f"age={minutes_since_end:.0f}min | {question[:60]}",
                  market_id=market_id, token_id=winning_token,
                  category=category, price=effective_threshold,
                  size=liquidity_usdc,
                  details={"score": opp_score, "source": source})

        await self.on_opportunity(opportunity)

    def _log_filtered(self, market_id: str, question: str, category: str,
                      source: str, reason: str):
        """Log a filtered opportunity with specific rejection reason."""
        if self.risk_manager:
            self.risk_manager.record_opportunity_filtered()
        if self.dry_run_reporter:
            self.dry_run_reporter.record_filter_reason(reason)
        log_event(logger, "OPPORTUNITY_FILTERED",
                  f"Filtered: {question[:70]} [{reason}]",
                  market_id=market_id, category=category,
                  details={"reason": reason, "source": source})

    def _check_uma_dispute(self, market: dict) -> bool:
        """Return True if market has an active UMA dispute."""
        if market.get("challenged", False):
            return True

        dispute_status = market.get("disputeStatus", "")
        if dispute_status and dispute_status.lower() not in ("", "none", "resolved"):
            return True

        uma_status = market.get("umaResolutionStatus", "")
        if uma_status and uma_status.lower() not in ("", "resolved", "settled"):
            return True

        return False

    # ------------------------------------------------------------------
    # HTTP Fetchers
    # ------------------------------------------------------------------

    async def _fetch_market_detail(self, market_id: str) -> dict | None:
        """Fetch a single market's details from Gamma API."""
        url = f"{self.config.gamma_base_url}/markets/{market_id}"
        return await self._rate_limited_gamma_get(url)

    async def _fetch_orderbook(self, token_id: str) -> dict | None:
        """Fetch order book from CLOB API."""
        url = f"{self.config.clob_base_url}/book"
        return await self._rate_limited_clob_get(url, {"token_id": token_id})

    async def _fetch_price_history(self, token_id: str) -> list[dict]:
        """Fetch price history from CLOB API."""
        url = f"{self.config.clob_base_url}/prices-history"
        params = {
            "market": token_id,
            "interval": "all",
            "fidelity": "60",
        }
        data = await self._rate_limited_clob_get(url, params)
        if data and isinstance(data, dict):
            return data.get("history", [])
        return []
