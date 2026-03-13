"""On-chain share redemption via web3.py.

Calls redeemPositions() on the CTF contract to convert winning shares to USDC.e.
"""

import asyncio
import logging
import time

import aiohttp

from bot.config import BotConfig
from bot.logger import log_event
from bot.order_manager import OrderManager

logger = logging.getLogger("arb_bot")

# Minimal ABI for redeemPositions
CTF_REDEEM_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "index", "type": "uint256"},
        ],
        "name": "payoutNumerators",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class Redeemer:
    """Redeems winning shares on-chain for USDC.e."""

    def __init__(self, config: BotConfig, order_manager: OrderManager):
        self.config = config
        self.order_manager = order_manager
        self.risk_manager = None  # Set by main.py
        self.settlement_tracker = None  # Set by main.py
        self._w3 = None
        self._contract = None
        self._account = None
        self._redeem_fail_counts: dict[str, int] = {}  # order_id -> fail count
        self._redeem_next_retry: dict[str, float] = {}  # order_id -> next retry timestamp

    def _init_web3(self):
        """Lazy-initialize web3 connection."""
        if self._w3 is not None:
            return

        from web3 import Web3

        self._w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc_url))
        if not self._w3.is_connected():
            logger.error("Cannot connect to Polygon RPC: %s", self.config.polygon_rpc_url)
            return

        self._contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.ct_framework_address),
            abi=CTF_REDEEM_ABI,
        )

        if self.config.private_key:
            self._account = self._w3.eth.account.from_key(self.config.private_key)
            logger.info("Web3 initialized, wallet: %s", self._account.address)

    async def check_and_redeem_settled(self):
        """Periodically check filled positions and attempt redemption."""
        logger.info("Redeemer started (interval=%ds)", self.config.redeem_check_interval_seconds)

        # Wait a bit before first check to let positions accumulate
        await asyncio.sleep(30)

        while True:
            try:
                redeemable = self.order_manager.get_redeemable_positions()
                if redeemable:
                    logger.info("Redeemer: %d filled positions to check", len(redeemable))

                for pos in redeemable:
                    # Skip if in backoff from previous failed attempts
                    next_retry = self._redeem_next_retry.get(pos.order_id, 0)
                    if time.time() < next_retry:
                        logger.info("Redeemer skip (backoff): %s", pos.question[:40])
                        continue

                    # FAST PATH: if settlement tracker confirmed resolution
                    # on-chain via payoutNumerators(), skip 2.5hr UMA wait.
                    # Non-zero payout IS the definitive confirmation.
                    if (self.settlement_tracker
                            and self.settlement_tracker.is_resolution_confirmed(
                                pos.order_id)):
                        logger.info("Redeemer FAST PATH: resolution confirmed for %s", pos.question[:40])
                        await self._redeem(pos.order_id, pos.condition_id,
                                           pos.market_id, pos.question)
                        continue

                    # SLOW PATH: wait at least 2.5 hours for UMA challenge
                    hours_since_fill = (time.time() - pos.filled_at) / 3600
                    if hours_since_fill < 2.5:
                        # Before UMA window: only attempt if market is confirmed resolved
                        if not await self._is_market_resolved(pos):
                            logger.info("Redeemer skip (not resolved, %.1fh old): %s",
                                        hours_since_fill, pos.question[:40])
                            continue

                    # Check for disputes (only after UMA window — empty fields
                    # are normal during first 2hrs post-resolution)
                    if self.risk_manager:
                        if await self._check_position_dispute(pos):
                            logger.warning("Redeemer: DISPUTE detected for %s", pos.question[:40])
                            self.risk_manager.report_dispute(
                                pos.market_id, pos.question)
                            self.risk_manager.record_trade_result(
                                net_profit=-pos.cost, gas_cost=0.0)
                            pos.status = "disputed"
                            continue

                    # Verify on-chain resolution before spending gas
                    resolved = await self._is_resolved_onchain(pos)
                    if not resolved:
                        logger.info("Redeemer skip (not resolved on-chain, cond=%s): %s",
                                    pos.condition_id[:18] if pos.condition_id else "EMPTY",
                                    pos.question[:40])
                        continue

                    await self._redeem(pos.order_id, pos.condition_id,
                                       pos.market_id, pos.question)
                # Also check for positions redeemed externally (e.g. via website)
                self.order_manager.sync_redeemed_positions()
                # Refresh wallet balance to reflect redemptions
                self.order_manager.sync_wallet_balance()
            except Exception:
                logger.exception("Redemption check error")

            await asyncio.sleep(self.config.redeem_check_interval_seconds)

    async def _redeem(self, order_id: str, condition_id: str,
                      market_id: str, question: str):
        """Redeem winning shares for a single position."""
        log_event(logger, "REDEEM_STARTED",
                  f"Redeeming shares for: {question[:60]}",
                  market_id=market_id, details={"condition_id": condition_id})

        if self.config.dry_run:
            log_event(logger, "REDEEM_COMPLETE",
                      f"[DRY RUN] Redeemed: {question[:60]}",
                      market_id=market_id, details={"condition_id": condition_id})
            self.order_manager.mark_redeemed(order_id)
            pos = self.order_manager.positions.get(order_id)
            if pos:
                if self.risk_manager:
                    self.risk_manager.record_trade_result(
                        net_profit=pos.net_profit, gas_cost=pos.gas_cost,
                        deployed=self.order_manager.total_deployed)
                if self.settlement_tracker:
                    self.settlement_tracker.record_trade_completion(
                        order_id, pos.gas_cost, time.time())
            return

        self._init_web3()
        if not self._w3 or not self._account or not self._contract:
            logger.error("Web3 not initialized, cannot redeem")
            return

        try:
            # Convert condition_id to bytes32
            if condition_id.startswith("0x"):
                cond_bytes = bytes.fromhex(condition_id[2:])
            else:
                cond_bytes = bytes.fromhex(condition_id)
            cond_bytes32 = cond_bytes.ljust(32, b"\x00")

            parent_collection_id = b"\x00" * 32

            tx = self._contract.functions.redeemPositions(
                self._w3.to_checksum_address(self.config.usdc_address),
                parent_collection_id,
                cond_bytes32,
                [1, 2],  # redeem both outcome index sets
            ).build_transaction({
                "from": self._account.address,
                "gas": 200000,
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gasPrice": self._w3.eth.gas_price,
            })

            signed_tx = self._w3.eth.account.sign_transaction(tx, self.config.private_key)
            tx_hash = self._w3.eth.send_raw_transaction(signed_tx.raw_transaction)

            # Wait for confirmation
            receipt = await asyncio.to_thread(
                self._w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120
            )

            if receipt["status"] == 1:
                log_event(logger, "REDEEM_COMPLETE",
                          f"Redeemed: {question[:60]}",
                          market_id=market_id, tx_hash=tx_hash.hex(),
                          details={"gas_used": receipt["gasUsed"]})
                self.order_manager.mark_redeemed(order_id)
                self._redeem_fail_counts.pop(order_id, None)
                self._redeem_next_retry.pop(order_id, None)
                pos = self.order_manager.positions.get(order_id)
                # Convert on-chain gas to USD estimate
                gas_used_matic = (
                    receipt["gasUsed"]
                    * receipt.get("effectiveGasPrice", 0) / 1e18
                )
                actual_gas_usd = gas_used_matic * 0.50  # rough MATIC/USD
                if pos:
                    if self.risk_manager:
                        self.risk_manager.record_trade_result(
                            net_profit=pos.net_profit, gas_cost=actual_gas_usd,
                            deployed=self.order_manager.total_deployed)
                    if self.settlement_tracker:
                        self.settlement_tracker.record_trade_completion(
                            order_id, actual_gas_usd, time.time())
            else:
                log_event(logger, "REDEEM_FAILED",
                          f"Redemption tx failed: {question[:60]}",
                          level="ERROR", market_id=market_id, tx_hash=tx_hash.hex())
                # Exponential backoff: 5min, 10min, 20min, 40min, ... (max 60min)
                fails = self._redeem_fail_counts.get(order_id, 0) + 1
                self._redeem_fail_counts[order_id] = fails
                backoff = min(3600, 300 * (2 ** (fails - 1)))
                self._redeem_next_retry[order_id] = time.time() + backoff
                logger.info("Redemption backoff: %s retry in %dm (attempt %d)",
                            market_id, backoff // 60, fails)

        except Exception:
            logger.exception("Redemption failed for market %s", market_id)

    async def _is_resolved_onchain(self, pos) -> bool:
        """Check payoutNumerators on-chain before attempting redemption.

        Returns True only if the condition is confirmed resolved on-chain.
        This prevents wasting gas on txs that will revert with
        'result for condition not received yet'.
        """
        if not pos.condition_id:
            logger.info("Redeemer: no condition_id for %s", pos.question[:40])
            return False

        self._init_web3()
        if not self._w3 or not self._contract:
            return False  # Can't check, don't attempt

        try:
            cond_hex = pos.condition_id.replace("0x", "")
            cond_bytes = bytes.fromhex(cond_hex)
            cond_bytes32 = cond_bytes.ljust(32, b"\x00")

            # payoutNumerators is on the CTF contract
            payout = await asyncio.to_thread(
                self._contract.functions.payoutNumerators(cond_bytes32, 0).call
            )
            logger.info("payoutNumerators(%s, 0) = %s", pos.condition_id[:18], payout)
            if payout > 0:
                return True
            logger.info("Redeemer: payout=0 for %s (not resolved yet)", pos.condition_id[:18])
            return False
        except Exception as e:
            logger.warning("On-chain resolution check failed for %s: %s",
                           pos.condition_id[:18], e)
            return False

    async def _is_market_resolved(self, pos) -> bool:
        """Check if the market has actually resolved via Gamma API.

        Returns True if the market is closed/resolved, False if still active.
        On API errors, returns True to avoid permanently blocking redemptions
        (the on-chain call will fail gracefully with backoff).
        """
        # CLOB-synced positions use condition_id as market_id — can't query Gamma
        market_id = pos.market_id
        if market_id.startswith("0x"):
            return True  # Let on-chain call determine if redeemable

        url = "%s/markets/%s" % (self.config.gamma_base_url, market_id)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        return True  # Don't block on API errors
                    market = await resp.json()
                    closed = market.get("closed", False)
                    resolved = market.get("resolved", False)
                    if not closed and not resolved:
                        logger.debug("Market %s not yet resolved, skipping redemption: %s",
                                     market_id, pos.question[:60])
                        return False
                    return True
        except Exception:
            logger.debug("Resolution check failed for %s, allowing redemption attempt",
                         market_id)
            return True

    async def _check_position_dispute(self, pos) -> bool:
        """Fetch market detail from Gamma and check for UMA dispute.

        Only called on positions >2.5hrs old — empty dispute fields are
        normal during the first 2hrs post-resolution (UMA challenge window).
        """
        url = "%s/markets/%s" % (self.config.gamma_base_url, pos.market_id)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        return False
                    market = await resp.json()

                    if market.get("challenged", False):
                        return True

                    dispute_status = (market.get("disputeStatus") or "").lower()
                    if dispute_status in ("disputed", "flagged"):
                        return True

                    uma_status = (market.get("umaResolutionStatus") or "").lower()
                    # Only flag explicit dispute statuses — intermediate
                    # states like "proposed" are normal UMA resolution flow,
                    # NOT disputes.
                    if uma_status in ("disputed", "challenged"):
                        return True
        except Exception:
            logger.debug("Dispute check failed for %s (will retry next cycle)",
                         pos.market_id)
        return False
