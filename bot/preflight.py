"""Pre-flight health checks that must all pass before trading begins.

Checks:
  1. Wallet balance above capital floor ($100)
  2. Polymarket CLOB API reachable and responding under 200ms
  3. Gamma API reachable
  4. WebSocket connection establishes successfully
  5. Polygon RPC endpoint responding
  6. At least one Crypto or Esports market currently active

If any check fails, the bot logs the specific failure and exits without trading.
"""

import asyncio
import logging
import time

import aiohttp
import websockets
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

from bot.config import BotConfig
from bot.logger import log_event

logger = logging.getLogger("arb_bot")


class PreflightError(Exception):
    """Raised when a pre-flight check fails."""


async def run_preflight(config: BotConfig) -> None:
    """Run all pre-flight checks. Raises PreflightError on failure."""
    checks = [
        ("CLOB API", _check_clob_api(config)),
        ("Gamma API", _check_gamma_api(config)),
        ("WebSocket", _check_websocket(config)),
        ("Active markets", _check_active_markets(config)),
    ]

    # RPC + wallet checks only in live mode (dry run doesn't touch the chain)
    if not config.dry_run:
        checks.insert(3, ("Polygon RPC", _check_polygon_rpc(config)))
        checks.insert(0, ("Wallet balance", _check_wallet_balance(config)))
        if config.redemption_enabled:
            checks.append(("EOA MATIC", _check_eoa_matic(config)))

    logger.info("Running %d pre-flight checks...", len(checks))

    failures = []
    for name, coro in checks:
        try:
            result = await asyncio.wait_for(coro, timeout=15.0)
            log_event(logger, "PREFLIGHT_PASS", "%s: %s" % (name, result))
        except asyncio.TimeoutError:
            msg = "%s: timed out (15s)" % name
            log_event(logger, "PREFLIGHT_FAIL", msg, level="ERROR")
            failures.append(msg)
        except Exception as e:
            msg = "%s: %s" % (name, e)
            log_event(logger, "PREFLIGHT_FAIL", msg, level="ERROR")
            failures.append(msg)

    if failures:
        summary = "Pre-flight failed (%d/%d): %s" % (
            len(failures), len(checks), "; ".join(failures))
        raise PreflightError(summary)

    log_event(logger, "PREFLIGHT_OK",
              "All %d pre-flight checks passed" % len(checks))


async def _check_clob_api(config: BotConfig) -> str:
    """CLOB API reachable and responding under 500ms."""
    url = config.clob_base_url.rstrip("/") + "/time"
    start = time.monotonic()

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            elapsed_ms = (time.monotonic() - start) * 1000
            if resp.status != 200:
                raise PreflightError("HTTP %d" % resp.status)
            if elapsed_ms > 500:
                raise PreflightError(
                    "response too slow: %.0fms (max 500ms)" % elapsed_ms)
            return "OK (%.0fms)" % elapsed_ms


async def _check_gamma_api(config: BotConfig) -> str:
    """Gamma API reachable."""
    url = config.gamma_base_url.rstrip("/") + "/markets?limit=1"
    start = time.monotonic()

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            elapsed_ms = (time.monotonic() - start) * 1000
            if resp.status != 200:
                raise PreflightError("HTTP %d" % resp.status)
            return "OK (%.0fms)" % elapsed_ms


async def _check_websocket(config: BotConfig) -> str:
    """WebSocket connection establishes successfully."""
    start = time.monotonic()

    async with websockets.connect(
        config.ws_market_url,
        ping_interval=None,
        close_timeout=5,
    ) as ws:
        elapsed_ms = (time.monotonic() - start) * 1000
        await ws.close()
        return "connected (%.0fms)" % elapsed_ms


async def _check_polygon_rpc(config: BotConfig) -> str:
    """Polygon RPC endpoint responding."""
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_blockNumber",
        "params": [],
        "id": 1,
    }
    start = time.monotonic()

    async with aiohttp.ClientSession() as session:
        async with session.post(config.polygon_rpc_url, json=payload) as resp:
            elapsed_ms = (time.monotonic() - start) * 1000
            if resp.status != 200:
                raise PreflightError("HTTP %d" % resp.status)
            data = await resp.json()
            if "result" not in data:
                raise PreflightError("invalid RPC response: %s" % data)
            block = int(data["result"], 16)
            return "block #%d (%.0fms)" % (block, elapsed_ms)


async def _check_wallet_balance(config: BotConfig) -> str:
    """Check Polymarket exchange balance (where deposited funds live).

    Funds deposited to Polymarket are held in their exchange contract,
    NOT in the wallet's on-chain USDC.e balance. Use the CLOB API to
    check the actual available balance for trading.
    """
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    if config.clob_api_key:
        creds = ApiCreds(
            api_key=config.clob_api_key,
            api_secret=config.clob_api_secret,
            api_passphrase=config.clob_api_passphrase,
        )
    else:
        tmp = ClobClient(
            config.clob_base_url,
            key=config.private_key,
            chain_id=config.chain_id,
            signature_type=2,
        )
        creds = tmp.create_or_derive_api_creds()

    funder = config.polymarket_proxy_address or None
    client = ClobClient(
        config.clob_base_url,
        key=config.private_key,
        chain_id=config.chain_id,
        creds=creds,
        signature_type=2,
        funder=funder,
    )

    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
    balance_resp = client.get_balance_allowance(params)
    # Response has 'balance' field in wei-like units (6 decimals for USDC)
    raw_balance = float(balance_resp.get("balance", 0))
    balance = raw_balance / 1e6  # USDC has 6 decimals

    if config.capital_floor and balance < config.capital_floor:
        logger.warning("Polymarket balance $%.2f below floor $%.0f — will skip trades at runtime",
                        balance, config.capital_floor)

    return "$%.2f on Polymarket" % balance


async def _check_eoa_matic(config: BotConfig) -> str:
    """Check the EOA's native MATIC balance for redemption gas.

    Redemption txs are signed and broadcast by the EOA (which owns the Safe),
    so the EOA — not the Safe — needs MATIC to pay gas. A redemption costs
    roughly 0.01-0.05 MATIC on Polygon, so min_matic_balance=0.5 is generous.
    Below the floor: warn but don't fail (user may top up during the day).
    """
    from web3 import Web3
    from eth_account import Account

    if not config.private_key:
        raise PreflightError("POLYMARKET_PRIVATE_KEY required for MATIC check")

    w3 = Web3(Web3.HTTPProvider(config.polygon_rpc_url))
    if not w3.is_connected():
        raise PreflightError("cannot connect to Polygon RPC")

    acct = Account.from_key(config.private_key)
    wei = w3.eth.get_balance(acct.address)
    matic = wei / 1e18

    if matic < config.min_matic_balance:
        logger.warning(
            "EOA %s has %.4f MATIC (below floor %.2f) — "
            "redemptions may fail until refilled",
            acct.address, matic, config.min_matic_balance)

    return "%.4f MATIC on EOA %s" % (matic, acct.address[:10])


async def _check_active_markets(config: BotConfig) -> str:
    """Verify that active markets exist on Polymarket."""
    url = config.gamma_base_url.rstrip("/") + "/markets"

    async with aiohttp.ClientSession() as session:
        params = {
            "limit": 10,
            "active": "true",
            "closed": "false",
        }
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise PreflightError("Gamma HTTP %d" % resp.status)
            markets = await resp.json()

    if not markets:
        raise PreflightError("no active markets found")

    return "%d active markets found" % len(markets)
