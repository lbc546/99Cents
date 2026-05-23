"""On-chain share redemption via Polymarket's Gnosis Safe proxy.

Polymarket wallets (signature_type=2) are 1/1 Gnosis Safe proxies. Our EOA
is the Safe's only owner but the shares live in the Safe, not the EOA. So we
can't call redeemPositions() directly — instead we wrap the inner call in
Safe.execTransaction(...), which the EOA submits as msg.sender.

Routing:
  - NegRisk markets  -> NegRiskAdapter.redeemPositions(conditionId, [yes, no])
  - Regular CTF      -> ConditionalTokens.redeemPositions(collat, parent, cond, indexSets)

The pre-validated signature trick (v=1, r=owner, s=0) means we don't need
EIP-712 signing: the Safe checks msg.sender == owner and accepts it.
"""

import asyncio
import logging
import time

import aiohttp

from bot.config import BotConfig
from bot.logger import log_event
from bot.order_manager import OrderManager

logger = logging.getLogger("arb_bot")


# Gnosis Safe v1.3.0 — only the bits we need
GNOSIS_SAFE_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ],
        "name": "execTransaction",
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getOwners",
        "outputs": [{"name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# ConditionalTokens (CTF) — redeem + balance + approval views
CTF_ABI = [
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
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# NegRiskAdapter — YES/NO amounts, adapter derives positionIds internally
NEG_RISK_ADAPTER_ABI = [
    {
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amounts", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# Minimal ERC-20 — used for USDC.e balance/allowance/approve on the Safe.
ERC20_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

# CollateralOnramp.wrap — converts USDC.e in the Safe to pUSD 1:1.
COLLATERAL_ONRAMP_ABI = [
    {
        "inputs": [
            {"name": "_asset", "type": "address"},
            {"name": "_to", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "name": "wrap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def _build_prevalidated_signature(owner_address: str) -> bytes:
    """Gnosis Safe pre-validated signature used when msg.sender is the owner.

    Format: r (32 bytes = owner address left-padded) + s (32 bytes zero) + v (1 byte = 1).
    Safe's checkNSignatures accepts v=1 when executor == owner, skipping the hash
    match. This means we don't need to EIP-712-sign anything when the EOA that
    owns the Safe is also submitting the tx.
    """
    addr_hex = owner_address.lower().replace("0x", "").rjust(40, "0")
    r = b"\x00" * 12 + bytes.fromhex(addr_hex)  # 12 zero bytes + 20 addr bytes = 32
    s = b"\x00" * 32
    v = b"\x01"
    return r + s + v


class Redeemer:
    """Redeems winning shares on-chain via the Polymarket Safe proxy."""

    def __init__(self, config: BotConfig, order_manager: OrderManager):
        self.config = config
        self.order_manager = order_manager
        self.risk_manager = None  # Set by main.py
        self.settlement_tracker = None  # Set by main.py
        self._w3 = None
        self._ctf = None
        self._neg_risk_adapter = None
        self._usdce = None
        self._collateral_onramp = None
        self._safe = None
        self._account = None
        self._approval_checked = False
        self._wrap_approval_checked = False
        self._redeem_fail_counts: dict[str, int] = {}  # order_id -> fail count
        self._redeem_next_retry: dict[str, float] = {}  # order_id -> next retry ts

    # ------------------------------------------------------------------
    # Web3 initialization
    # ------------------------------------------------------------------

    def _init_web3(self):
        """Lazy-initialize web3 + contracts. Idempotent."""
        if self._w3 is not None:
            return

        from web3 import Web3

        self._w3 = Web3(Web3.HTTPProvider(self.config.polygon_rpc_url))
        if not self._w3.is_connected():
            logger.error("Cannot connect to Polygon RPC: %s", self.config.polygon_rpc_url)
            self._w3 = None
            return

        self._ctf = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.ct_framework_address),
            abi=CTF_ABI,
        )
        self._neg_risk_adapter = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.neg_risk_adapter_address),
            abi=NEG_RISK_ADAPTER_ABI,
        )

        # V2 wrap path: USDC.e -> pUSD via CollateralOnramp.
        self._usdce = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.usdc_address),
            abi=ERC20_ABI,
        )
        self._collateral_onramp = self._w3.eth.contract(
            address=Web3.to_checksum_address(self.config.collateral_onramp_address),
            abi=COLLATERAL_ONRAMP_ABI,
        )
        self._wrap_approval_checked = False

        if self.config.polymarket_proxy_address:
            self._safe = self._w3.eth.contract(
                address=Web3.to_checksum_address(self.config.polymarket_proxy_address),
                abi=GNOSIS_SAFE_ABI,
            )

        if self.config.private_key:
            self._account = self._w3.eth.account.from_key(self.config.private_key)
            logger.info("Redeemer web3 ready — EOA=%s safe=%s",
                        self._account.address,
                        self.config.polymarket_proxy_address or "<none>")

    def _check_approvals_once(self):
        """Verify the Safe has approved NegRiskAdapter as a CTF operator.

        Polymarket sets this up during onboarding for neg-risk trading, so it
        should already be true. We log a clear error if not — redemption will
        fail until the user approves via the website.
        """
        if self._approval_checked:
            return
        self._approval_checked = True

        if not self._safe or not self._ctf:
            return

        try:
            from web3 import Web3
            approved = self._ctf.functions.isApprovedForAll(
                Web3.to_checksum_address(self.config.polymarket_proxy_address),
                Web3.to_checksum_address(self.config.neg_risk_adapter_address),
            ).call()
            if approved:
                logger.info("Approval OK: Safe -> NegRiskAdapter on CTF")
            else:
                logger.error(
                    "MISSING APPROVAL: Safe has not approved NegRiskAdapter on CTF. "
                    "Neg-risk redemptions will FAIL. Approve via polymarket.com "
                    "(trade any neg-risk market once) or disable redemption_enabled."
                )
        except Exception as e:
            logger.warning("Approval check failed (non-fatal): %s", e)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def check_and_redeem_settled(self):
        """Periodically check filled positions and attempt redemption."""
        logger.info("Redeemer started (interval=%ds, on_chain=%s)",
                    self.config.redeem_check_interval_seconds,
                    self.config.redemption_enabled)

        # Wait a bit before first check to let positions accumulate
        await asyncio.sleep(30)

        while True:
            try:
                redeemable = self.order_manager.get_redeemable_positions()

                # Sync manual redemptions every cycle (cheap CLOB calls only
                # for filled positions). This ensures positions redeemed via
                # the website get marked correctly even if it's the last one.
                if redeemable:
                    self.order_manager.sync_redeemed_positions()
                    self.order_manager.sync_wallet_balance()
                    # Re-check after sync — some may have been redeemed externally
                    redeemable = self.order_manager.get_redeemable_positions()

                if not redeemable:
                    await asyncio.sleep(self.config.redeem_check_interval_seconds)
                    continue

                logger.info("Redeemer: %d filled positions to check", len(redeemable))

                for pos in redeemable:
                    # Skip if in backoff from previous failed attempts
                    next_retry = self._redeem_next_retry.get(pos.order_id, 0)
                    if time.time() < next_retry:
                        logger.info("Redeemer skip (backoff): %s", pos.question[:40])
                        continue

                    # FAST PATH: if settlement tracker confirmed resolution
                    # on-chain via payoutNumerators(), skip 2.5hr UMA wait.
                    if (self.settlement_tracker
                            and self.settlement_tracker.is_resolution_confirmed(
                                pos.order_id)):
                        logger.info("Redeemer FAST PATH: resolution confirmed for %s",
                                    pos.question[:40])
                        await self._redeem(pos)
                        continue

                    # SLOW PATH: wait at least 2.5 hours for UMA challenge
                    hours_since_fill = (time.time() - pos.filled_at) / 3600
                    if hours_since_fill < 2.5:
                        if not await self._is_market_resolved(pos):
                            logger.info("Redeemer skip (not resolved, %.1fh old): %s",
                                        hours_since_fill, pos.question[:40])
                            continue

                    # Check for disputes (only after UMA window)
                    if self.risk_manager:
                        if await self._check_position_dispute(pos):
                            logger.warning("Redeemer: DISPUTE detected for %s",
                                           pos.question[:40])
                            self.risk_manager.report_dispute(
                                pos.market_id, pos.question)
                            self.risk_manager.record_trade_result(
                                net_profit=-pos.cost, gas_cost=0.0)
                            pos.status = "disputed"
                            self.order_manager._save_positions()
                            continue

                    # Verify on-chain resolution before spending gas. For
                    # neg-risk markets, payoutNumerators on the base CTF
                    # returns 0 — fall back to Gamma's "closed" signal plus
                    # the 2.5hr UMA window.
                    resolved = await self._is_resolved_onchain(pos)
                    if not resolved:
                        if hours_since_fill >= 2.5 and await self._is_market_resolved(pos):
                            logger.info("Redeemer: payout=0 but Gamma says resolved, attempting: %s",
                                        pos.question[:40])
                        elif (hours_since_fill >= 24
                              and pos.category == "Science/Weather"):
                            # Force attempt for WEATHER ONLY: Gamma sometimes
                            # never updates closed=true for resolved neg-risk
                            # weather markets. After 24h the day's temp is
                            # locked in, so just try — adapter reverts safely
                            # if not yet resolved.
                            #
                            # WARNING: do NOT extend this to other categories.
                            # Slow-resolution markets (Politics, Sports finals,
                            # etc.) also revert pre-resolution; the 3-revert
                            # rule would misinterpret as loss and prematurely
                            # mark resolved_loss on still-pending positions.
                            logger.info("Redeemer: force-attempt (%.1fh old, weather Gamma stale): %s",
                                        hours_since_fill, pos.question[:40])
                        else:
                            logger.info("Redeemer skip (not resolved on-chain, cond=%s): %s",
                                        pos.condition_id[:18] if pos.condition_id else "EMPTY",
                                        pos.question[:40])
                            continue

                    await self._redeem(pos)
            except Exception:
                logger.exception("Redemption check error")

            await asyncio.sleep(self.config.redeem_check_interval_seconds)

    # ------------------------------------------------------------------
    # Redemption core
    # ------------------------------------------------------------------

    async def _redeem(self, pos):
        """Redeem winning shares for a single position."""
        order_id = pos.order_id
        condition_id = pos.condition_id
        market_id = pos.market_id
        question = pos.question

        log_event(logger, "REDEEM_STARTED",
                  f"Redeeming shares for: {question[:60]}",
                  market_id=market_id, details={"condition_id": condition_id})

        if self.config.dry_run:
            log_event(logger, "REDEEM_COMPLETE",
                      f"[DRY RUN] Redeemed: {question[:60]}",
                      market_id=market_id,
                      details={"condition_id": condition_id})
            self.order_manager.mark_redeemed(order_id)
            p = self.order_manager.positions.get(order_id)
            if p:
                if self.risk_manager:
                    self.risk_manager.record_trade_result(
                        net_profit=p.net_profit, gas_cost=p.gas_cost,
                        deployed=self.order_manager.total_deployed)
                if self.settlement_tracker:
                    self.settlement_tracker.record_trade_completion(
                        order_id, p.gas_cost, time.time())
            return

        if not self.config.redemption_enabled:
            # Manual mode — sync_redeemed_positions() in the main loop
            # will still detect website redemptions.
            logger.info("Redeemer: on-chain disabled, redeem manually: %s",
                        question[:60])
            return

        if not self.config.polymarket_proxy_address:
            logger.error("Redeemer: POLYMARKET_PROXY_ADDRESS not set, cannot redeem via Safe")
            return

        self._init_web3()
        if not self._w3 or not self._safe or not self._account:
            logger.error("Redeemer: web3/safe/account not initialized, cannot redeem")
            return

        # Backfill neg_risk flag lazily for CLOB-synced or old positions.
        await self._ensure_neg_risk_detected(pos)

        # Backfill outcome_index — we need it both to know if we won and
        # (for neg-risk) to build the correct amounts array.
        outcome_index = await self._detect_outcome_index(pos)
        if outcome_index < 0:
            logger.warning("Redeemer: cannot determine outcome_index for %s — skipping",
                           pos.question[:50])
            self._record_failure(pos.order_id)
            return

        self._check_approvals_once()

        # Did the outcome we bought actually win? For non-neg-risk markets,
        # check payoutNumerators directly — it's authoritative on the base CTF.
        # For neg-risk markets, the base CTF often has payout=0 even for
        # winners (the NegRiskAdapter resolves internally via UMA, not via
        # CTF.reportPayouts). So for neg-risk we skip the payout check and
        # just attempt redemption — the adapter will revert if we're wrong,
        # and we'll backoff gracefully.
        if not pos.neg_risk:
            our_payout = await self._get_outcome_payout(pos, outcome_index)
            if our_payout == 0:
                log_event(logger, "REDEEM_SKIP_LOSER",
                          "Outcome %d lost (payout=0): %s" % (
                              outcome_index, pos.question[:60]),
                          market_id=pos.market_id,
                          details={"condition_id": pos.condition_id,
                                   "outcome_index": outcome_index})
                self.order_manager.mark_resolved_loss(pos.order_id)
                self._redeem_fail_counts.pop(pos.order_id, None)
                self._redeem_next_retry.pop(pos.order_id, None)
                return

        if pos.neg_risk:
            await self._redeem_neg_risk(pos, outcome_index)
        else:
            await self._redeem_ctf(pos)

    async def _redeem_neg_risk(self, pos, outcome_index: int):
        """Build & submit a NegRiskAdapter redemption via the Safe.

        amounts is a 2-element array [yes_amount, no_amount]; we put our
        balance at the index of the outcome we actually bought.
        """
        from web3 import Web3

        try:
            # 1. Query the raw token balance held by the Safe on CTF for our token.
            safe_addr = Web3.to_checksum_address(self.config.polymarket_proxy_address)
            token_id_int = int(pos.token_id)
            balance = await asyncio.to_thread(
                self._ctf.functions.balanceOf(safe_addr, token_id_int).call
            )
            if balance == 0:
                logger.info("Redeemer: token balance is 0 for %s — already redeemed externally",
                            pos.question[:50])
                self.order_manager.mark_redeemed(pos.order_id)
                self._redeem_fail_counts.pop(pos.order_id, None)
                self._redeem_next_retry.pop(pos.order_id, None)
                return

            # 2. Build amounts array — balance at our outcome's index.
            amounts = [0, 0]
            if 0 <= outcome_index < len(amounts):
                amounts[outcome_index] = int(balance)
            else:
                logger.error("Redeemer: outcome_index %d out of range for %s",
                             outcome_index, pos.question[:50])
                self._record_failure(pos.order_id)
                return

            cond_bytes32 = self._to_bytes32(pos.condition_id)

            # 3a. PRE-FLIGHT: simulate the redeemPositions call as a read-only
            # eth_call. If the adapter would revert, skip submitting the real
            # tx — saves gas on positions waiting for UMA resolution. Backoff
            # via _record_failure so we eventually catch confirmed losers
            # (when opposite-side simulation succeeds).
            try:
                await asyncio.to_thread(
                    self._neg_risk_adapter.functions.redeemPositions(
                        cond_bytes32, amounts
                    ).call,
                    {"from": safe_addr},
                )
            except Exception:
                logger.info(
                    "Redeemer: adapter would revert (market not yet resolvable), skipping tx: %s",
                    pos.question[:50])
                self._record_failure(pos.order_id)
                return

            inner_data = self._neg_risk_adapter.encode_abi(
                abi_element_identifier="redeemPositions",
                args=[cond_bytes32, amounts],
            )

            # 3b. Wrap in Safe.execTransaction and submit.
            target = Web3.to_checksum_address(self.config.neg_risk_adapter_address)
            await self._submit_safe_exec(pos, target, inner_data,
                                         context="neg_risk",
                                         details={
                                             "balance": int(balance),
                                             "outcome_index": outcome_index,
                                         })
        except Exception:
            logger.exception("NegRisk redemption failed for market %s", pos.market_id)
            self._record_failure(pos.order_id)

    async def _redeem_ctf(self, pos):
        """Build & submit a plain ConditionalTokens redemption via the Safe."""
        from web3 import Web3

        try:
            # Check the Safe still holds tokens before paying gas. If we
            # already sold/redeemed externally, balance is 0 — mark as
            # redeemed and skip rather than reverting on-chain.
            if self._safe and pos.token_id:
                try:
                    safe_addr = Web3.to_checksum_address(
                        self.config.polymarket_proxy_address)
                    balance = await asyncio.to_thread(
                        self._ctf.functions.balanceOf(
                            safe_addr, int(pos.token_id)).call
                    )
                    if balance == 0:
                        logger.info(
                            "Redeemer: token balance is 0 for %s — already redeemed/sold externally",
                            pos.question[:50])
                        self.order_manager.mark_redeemed(pos.order_id)
                        self._redeem_fail_counts.pop(pos.order_id, None)
                        self._redeem_next_retry.pop(pos.order_id, None)
                        return
                except Exception as e:
                    logger.debug("CTF balance check failed for %s: %s",
                                 pos.order_id[:16], e)

            cond_bytes32 = self._to_bytes32(pos.condition_id)
            parent_collection_id = b"\x00" * 32

            inner_data = self._ctf.encode_abi(
                abi_element_identifier="redeemPositions",
                args=[
                    Web3.to_checksum_address(self.config.usdc_address),
                    parent_collection_id,
                    cond_bytes32,
                    [1, 2],  # YES + NO partition for binary markets
                ],
            )

            target = Web3.to_checksum_address(self.config.ct_framework_address)
            await self._submit_safe_exec(pos, target, inner_data,
                                         context="ctf",
                                         details={})
        except Exception:
            logger.exception("CTF redemption failed for market %s", pos.market_id)
            self._record_failure(pos.order_id)

    async def _wrap_to_pusd(self, pos, context: str):
        """Wrap any USDC.e in the Safe to pUSD via CollateralOnramp.

        Called after a successful redemption (CTF pays out USDC.e but the
        V2 exchange only accepts pUSD). Best-effort — failure here doesn't
        affect the redemption outcome.
        """
        if not self.config.auto_wrap_after_redeem:
            return
        if not self._safe or not self._usdce or not self._collateral_onramp:
            return
        from web3 import Web3

        try:
            safe_addr = Web3.to_checksum_address(self.config.polymarket_proxy_address)
            onramp_addr = Web3.to_checksum_address(self.config.collateral_onramp_address)

            # Query USDC.e balance held by the Safe.
            balance = await asyncio.to_thread(
                self._usdce.functions.balanceOf(safe_addr).call
            )
            if balance == 0:
                return

            # Ensure CollateralOnramp is approved to pull USDC.e from the Safe.
            # Lazy one-shot check; if missing, submit max-approval first.
            if not self._wrap_approval_checked:
                self._wrap_approval_checked = True
                allowance = await asyncio.to_thread(
                    self._usdce.functions.allowance(safe_addr, onramp_addr).call
                )
                if allowance < balance:
                    max_uint = (1 << 256) - 1
                    approve_data = self._usdce.encode_abi(
                        abi_element_identifier="approve",
                        args=[onramp_addr, max_uint],
                    )
                    usdce_addr = Web3.to_checksum_address(self.config.usdc_address)
                    await self._submit_safe_exec(
                        pos, usdce_addr, approve_data,
                        context="wrap_approve",
                        details={"spender": onramp_addr},
                    )

            # Build wrap calldata: wrap(USDC.e, safe, balance) — output goes to safe.
            wrap_data = self._collateral_onramp.encode_abi(
                abi_element_identifier="wrap",
                args=[
                    Web3.to_checksum_address(self.config.usdc_address),
                    safe_addr,
                    int(balance),
                ],
            )
            await self._submit_safe_exec(
                pos, onramp_addr, wrap_data,
                context="wrap",
                details={
                    "amount_usdce_raw": int(balance),
                    "amount_usdc": int(balance) / 1e6,
                    "after": context,
                },
            )
        except Exception:
            logger.exception("Auto-wrap USDC.e -> pUSD failed for market %s",
                             pos.market_id)

    async def _submit_safe_exec(self, pos, target_address: str,
                                inner_calldata: str,
                                context: str, details: dict):
        """Wrap inner calldata in Safe.execTransaction and send."""
        from web3 import Web3

        try:
            signatures = _build_prevalidated_signature(self._account.address)
            zero_addr = "0x0000000000000000000000000000000000000000"

            # Polygon gas pricing: eth.gas_price often returns 200-300+ gwei
            # (inflated), but normal Polygon mainnet is 30-50 gwei. Cap at 60
            # gwei to avoid burning MATIC paying inflated rates. If the
            # network truly needs more, tx will sit pending — fine for
            # non-urgent redemptions.
            node_price = self._w3.eth.gas_price
            gas_price = min(int(node_price * 1.1), 60 * 10**9)

            tx = self._safe.functions.execTransaction(
                target_address,
                0,  # value
                bytes.fromhex(inner_calldata.replace("0x", "")),
                0,  # operation = Call
                0,  # safeTxGas
                0,  # baseGas
                0,  # gasPrice (Safe-internal refund accounting, not tx gas)
                zero_addr,  # gasToken
                zero_addr,  # refundReceiver
                signatures,
            ).build_transaction({
                "from": self._account.address,
                "gas": 400000,  # neg-risk redemption uses ~250k; headroom only
                "nonce": self._w3.eth.get_transaction_count(self._account.address),
                "gasPrice": gas_price,
            })

            signed_tx = self._w3.eth.account.sign_transaction(
                tx, self.config.private_key)
            tx_hash = self._w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            logger.info("Redeemer [%s] tx sent: %s | %s",
                        context, tx_hash.hex(), pos.question[:50])

            receipt = await asyncio.to_thread(
                self._w3.eth.wait_for_transaction_receipt, tx_hash, 180
            )

            if receipt["status"] == 1:
                gas_used_matic = (
                    receipt["gasUsed"]
                    * receipt.get("effectiveGasPrice", 0) / 1e18
                )
                actual_gas_usd = gas_used_matic * 0.50  # rough MATIC/USD
                log_event(logger, "REDEEM_COMPLETE",
                          f"Redeemed [{context}]: {pos.question[:60]}",
                          market_id=pos.market_id,
                          tx_hash=tx_hash.hex(),
                          details={
                              "gas_used": receipt["gasUsed"],
                              "gas_usd": actual_gas_usd,
                              **details,
                          })
                # Mark redeemed only for redemption contexts (not the
                # auto-wrap submissions which reuse this method).
                if context in ("ctf", "neg_risk"):
                    self.order_manager.mark_redeemed(pos.order_id)
                    self._redeem_fail_counts.pop(pos.order_id, None)
                    self._redeem_next_retry.pop(pos.order_id, None)
                    p = self.order_manager.positions.get(pos.order_id)
                    if p:
                        if self.risk_manager:
                            self.risk_manager.record_trade_result(
                                net_profit=p.net_profit, gas_cost=actual_gas_usd,
                                deployed=self.order_manager.total_deployed)
                        if self.settlement_tracker:
                            self.settlement_tracker.record_trade_completion(
                                pos.order_id, actual_gas_usd, time.time())
                    # CTF pays out USDC.e; auto-wrap to pUSD so funds are
                    # immediately tradeable on the V2 exchange.
                    await self._wrap_to_pusd(pos, context=context)
            else:
                log_event(logger, "REDEEM_FAILED",
                          f"Safe.execTransaction reverted [{context}]: {pos.question[:60]}",
                          level="ERROR",
                          market_id=pos.market_id,
                          tx_hash=tx_hash.hex())
                # Wrap/approve failures shouldn't penalize the redemption.
                if context in ("ctf", "neg_risk"):
                    self._record_failure(pos.order_id)
        except Exception:
            logger.exception("Safe exec failed for market %s (%s)", pos.market_id, context)
            if context in ("ctf", "neg_risk"):
                self._record_failure(pos.order_id)

    def _record_failure(self, order_id: str):
        """Exponential backoff: 5min, 10min, 20min, 40min, capped at 60min.

        After 3 reverts on a neg-risk position, verify the loss before
        marking it as resolved_loss: simulate redemption on the *opposite*
        side. If that simulation succeeds, the market is resolved and we
        are confirmed the losing side. If it ALSO reverts, the market
        isn't actually resolved yet (UMA still pending) — keep waiting,
        don't mark a false loss.
        """
        fails = self._redeem_fail_counts.get(order_id, 0) + 1
        self._redeem_fail_counts[order_id] = fails
        backoff = min(3600, 300 * (2 ** (fails - 1)))
        self._redeem_next_retry[order_id] = time.time() + backoff
        logger.info("Redemption backoff: %s retry in %dm (attempt %d)",
                    order_id[:16], backoff // 60, fails)

        if fails < 3:
            return
        pos = self.order_manager.positions.get(order_id)
        if not pos or not pos.neg_risk:
            return

        # Verify resolution before marking loss: try redeeming the OTHER side.
        # If both sides revert, market hasn't actually resolved — keep retrying.
        try:
            from web3 import Web3
            self._init_web3()
            if not self._neg_risk_adapter or not self._safe:
                return
            cond_bytes = self._to_bytes32(pos.condition_id)
            our_idx = pos.outcome_index if 0 <= pos.outcome_index <= 1 else 0
            other_idx = 1 - our_idx
            test_amounts = [0, 0]
            test_amounts[other_idx] = 1  # 1 wei to test resolvability
            safe_addr = Web3.to_checksum_address(
                self.config.polymarket_proxy_address)
            other_resolves = False
            try:
                self._neg_risk_adapter.functions.redeemPositions(
                    cond_bytes, test_amounts
                ).call({"from": safe_addr})
                other_resolves = True
            except Exception:
                other_resolves = False
        except Exception:
            logger.exception("Failed pre-loss resolution check for %s", order_id[:16])
            return  # don't mark loss if check itself errored

        if not other_resolves:
            logger.info(
                "Resolution not yet finalized for %s — not marking loss, will keep retrying",
                pos.question[:50])
            # Stretch the next retry — UMA settlement can take a while
            self._redeem_next_retry[order_id] = time.time() + 3600
            return

        logger.warning(
            "Marking neg-risk position as resolved_loss (opposite side redeemable): %s",
            pos.question[:50])
        self.order_manager.mark_resolved_loss(order_id)
        self._redeem_fail_counts.pop(order_id, None)
        self._redeem_next_retry.pop(order_id, None)

    @staticmethod
    def _to_bytes32(hex_id: str) -> bytes:
        """Convert a 0x-prefixed hex string to a left-aligned 32-byte value."""
        raw = hex_id.replace("0x", "")
        return bytes.fromhex(raw).ljust(32, b"\x00")

    # ------------------------------------------------------------------
    # Resolution + metadata checks
    # ------------------------------------------------------------------

    async def _ensure_neg_risk_detected(self, pos):
        """Fetch Gamma once to populate pos.neg_risk if we don't know it yet.

        New positions get neg_risk set at order time. Old persisted positions
        and CLOB-synced positions don't — this fills in the gap on first
        redemption attempt so we route to the right contract.
        """
        if pos.neg_risk:
            return  # already known

        market_id = pos.market_id
        # Gamma needs `condition_ids=<cid>&closed=true` to return
        # resolved markets (the default filter excludes closed ones).
        # By the time we're here, the market has already passed the
        # resolution check, so closed=true is the right bucket.
        if market_id.startswith("0x") or pos.condition_id:
            cond = pos.condition_id or market_id
            url = "%s/markets?condition_ids=%s&closed=true" % (
                self.config.gamma_base_url, cond)
        else:
            url = "%s/markets/%s" % (self.config.gamma_base_url, market_id)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
                    market = data[0] if isinstance(data, list) and data else data
                    if not isinstance(market, dict):
                        return
                    if bool(market.get("negRisk", False)):
                        pos.neg_risk = True
                        self.order_manager._save_positions()
                        logger.info("Detected neg_risk=True for %s (backfilled)",
                                    pos.question[:50])
                    else:
                        logger.info("Detected neg_risk=False for %s", pos.question[:50])
        except Exception as e:
            logger.debug("neg_risk detection failed for %s: %s", pos.market_id, e)

    async def _is_resolved_onchain(self, pos) -> bool:
        """Check payoutDenominator on CTF — non-zero for ANY resolved outcome.

        Previously checked payoutNumerators(cond, 0) > 0 which only detected
        YES-winners and missed NO-winners entirely (~5/8 of recent positions).
        payoutDenominator is set in reportPayouts() regardless of which side
        won, so it's a clean "is this condition resolved" probe.
        """
        if not pos.condition_id:
            logger.info("Redeemer: no condition_id for %s", pos.question[:40])
            return False

        self._init_web3()
        if not self._w3 or not self._ctf:
            return False

        try:
            cond_bytes32 = self._to_bytes32(pos.condition_id)
            denom = await asyncio.to_thread(
                self._ctf.functions.payoutDenominator(cond_bytes32).call
            )
            logger.info("payoutDenominator(%s) = %s",
                        pos.condition_id[:18], denom)
            return denom > 0
        except Exception as e:
            logger.warning("On-chain resolution check failed for %s: %s",
                           pos.condition_id[:18], e)
            return False

    async def _detect_outcome_index(self, pos) -> int:
        """Find which outcome index our token_id corresponds to via Gamma.

        New positions get outcome_index at order time. Old positions and
        CLOB-synced positions need backfill — query Gamma's clobTokenIds
        array and match against pos.token_id. Persist on success.
        Returns -1 if detection fails.
        """
        if pos.outcome_index >= 0:
            return pos.outcome_index

        cond = pos.condition_id or pos.market_id
        if not cond:
            return -1

        if cond.startswith("0x"):
            url = "%s/markets?condition_ids=%s&closed=true" % (
                self.config.gamma_base_url, cond)
        else:
            url = "%s/markets/%s" % (self.config.gamma_base_url, cond)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        return -1
                    data = await resp.json()
                    market = data[0] if isinstance(data, list) and data else data
                    if not isinstance(market, dict):
                        return -1

                    raw = market.get("clobTokenIds", "[]")
                    import json
                    token_ids = json.loads(raw) if isinstance(raw, str) else raw
                    if not isinstance(token_ids, list):
                        return -1

                    target = str(pos.token_id)
                    for i, tid in enumerate(token_ids):
                        if str(tid) == target:
                            pos.outcome_index = i
                            self.order_manager._save_positions()
                            logger.info(
                                "Detected outcome_index=%d for %s (backfilled)",
                                i, pos.question[:50])
                            return i
                    logger.warning(
                        "outcome_index detection: token %s not in clobTokenIds %s for %s",
                        target[:16], token_ids, pos.question[:50])
                    return -1
        except Exception as e:
            logger.debug("outcome_index detection failed for %s: %s",
                         pos.market_id, e)
            return -1

    async def _get_outcome_payout(self, pos, outcome_index: int) -> int:
        """Read payoutNumerators for our specific outcome. Returns 0 on error."""
        self._init_web3()
        if not self._w3 or not self._ctf or not pos.condition_id:
            return 0
        try:
            cond_bytes32 = self._to_bytes32(pos.condition_id)
            return await asyncio.to_thread(
                self._ctf.functions.payoutNumerators(
                    cond_bytes32, outcome_index).call
            )
        except Exception as e:
            logger.warning("payoutNumerators(%s, %d) failed: %s",
                           pos.condition_id[:18], outcome_index, e)
            return 0

    async def _is_market_resolved(self, pos) -> bool:
        """Check if the market has actually resolved via Gamma API.

        Tries two approaches:
          1. By market_id (Gamma numeric slug) — works for non-neg-risk
          2. By condition_ids with closed=true — works for neg-risk sub-markets
             where the individual market endpoint may not reflect resolution

        On API errors, returns True to avoid blocking redemptions (the
        on-chain call will fail gracefully with backoff).
        """
        market_id = pos.market_id
        # Approach 1: query by numeric market_id (Gamma slug). Skip for
        # CLOB-synced positions where market_id is a hex condition_id —
        # those use Approach 2 below.
        if not market_id.startswith("0x"):
            url = "%s/markets/%s" % (self.config.gamma_base_url, market_id)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status == 200:
                            market = await resp.json()
                            if market.get("closed", False) or market.get("resolved", False):
                                return True
            except Exception:
                pass

        # Approach 2: query by condition_ids (catches neg-risk sub-markets)
        cond = pos.condition_id
        if cond:
            url2 = "%s/markets?condition_ids=%s&closed=true" % (
                self.config.gamma_base_url, cond)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url2, timeout=aiohttp.ClientTimeout(total=15)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list) and len(data) > 0:
                                logger.info("Gamma resolved (by condition_id): %s",
                                            pos.question[:50])
                                return True
            except Exception:
                pass

        logger.debug("Market %s not yet resolved: %s", market_id, pos.question[:60])
        return False

    async def _check_position_dispute(self, pos) -> bool:
        """Fetch market detail from Gamma and check for UMA dispute."""
        market_id = pos.market_id
        if market_id.startswith("0x") or pos.condition_id:
            cond = pos.condition_id or market_id
            url = "%s/markets?condition_ids=%s&closed=true" % (
                self.config.gamma_base_url, cond)
        else:
            url = "%s/markets/%s" % (self.config.gamma_base_url, market_id)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    market = data[0] if isinstance(data, list) and data else data
                    if not isinstance(market, dict):
                        return False

                    if market.get("challenged", False):
                        return True

                    dispute_status = (market.get("disputeStatus") or "").lower()
                    if dispute_status in ("disputed", "flagged"):
                        return True

                    uma_status = (market.get("umaResolutionStatus") or "").lower()
                    if uma_status in ("disputed", "challenged"):
                        return True
        except Exception:
            logger.debug("Dispute check failed for %s (will retry next cycle)",
                         pos.market_id)
        return False
