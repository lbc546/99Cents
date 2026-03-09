#!/usr/bin/env python3
"""
Polymarket 99-Cent Arbitrage Data Collection

Collects resolved market data from Gamma API and price histories from CLOB API.
Excludes 5min/15min crypto markets (they have fees that kill the margin).
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

# --- Configuration ---
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Lookback: 30 days keeps crawl fast; Polymarket has ~5000 markets/day
LOOKBACK_DAYS = 30

# Max markets to fetch price histories for (most impactful)
MAX_PRICE_HISTORY_MARKETS = 500

# Rate limit: CLOB allows 100 req/min for public endpoints
CLOB_DELAY = 0.6  # ~100/min


def is_crypto_short_term(question):
    """Detect 5min/15min crypto markets by question text.

    Matches patterns like: 'Bitcoin Up or Down - February 28, 1:45AM-1:50AM ET'
    """
    q = question if isinstance(question, str) else ""
    if re.search(r"(?i)(?:bitcoin|ethereum|solana|xrp|dogecoin|matic|btc|eth|sol|doge)\s+up\s+or\s+down", q):
        return True
    if re.search(r"(?i)(?:bitcoin|ethereum|solana|xrp|btc|eth|sol|doge).*\d+:\d+\s*(?:AM|PM)", q):
        return True
    return False


def safe_request(url, params=None, max_retries=3):
    """Make an HTTP GET request with retry logic."""
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                wait = 2 ** attempt * 5
                print(f"  Rate limited, waiting {wait}s...", flush=True)
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"  Failed: {url} - {e}", flush=True)
                return None
    return None


def parse_json_field(value, default=None):
    """Safely parse a JSON string field."""
    if default is None:
        default = []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return default


def collect_resolved_markets():
    """Fetch resolved markets from Gamma API, filtering inline."""
    print("=" * 60, flush=True)
    print("Step 1: Collecting resolved markets from Gamma API", flush=True)
    print("=" * 60, flush=True)

    cutoff = datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    processed = []
    excluded_crypto = 0
    excluded_unresolved = 0
    excluded_old = 0
    total_raw = 0
    offset = 0
    limit = 100
    page = 0
    hit_cutoff = False

    while not hit_cutoff:
        page += 1
        data = safe_request(f"{GAMMA_BASE}/markets", params={
            "closed": "true",
            "limit": limit,
            "offset": offset,
            "order": "closedTime",
            "ascending": "false",
        })

        if data is None or len(data) == 0:
            break

        total_raw += len(data)

        for m in data:
            # Check date cutoff first (most efficient filter)
            closed_time = m.get("closedTime") or m.get("endDate") or ""
            if closed_time and closed_time < cutoff_str:
                excluded_old += 1
                hit_cutoff = True
                continue

            # Exclude crypto short-term
            question = m.get("question", "")
            if is_crypto_short_term(question):
                excluded_crypto += 1
                continue

            # Check resolution
            outcome_prices = parse_json_field(m.get("outcomePrices", "[]"))
            outcomes = parse_json_field(m.get("outcomes", "[]"))
            token_ids = parse_json_field(m.get("clobTokenIds", "[]"))

            winning_idx = None
            for i, price in enumerate(outcome_prices):
                try:
                    if float(price) == 1.0:
                        winning_idx = i
                        break
                except (ValueError, TypeError):
                    continue

            if winning_idx is None:
                excluded_unresolved += 1
                continue

            winning_outcome = outcomes[winning_idx] if winning_idx < len(outcomes) else "Unknown"
            winning_token = token_ids[winning_idx] if winning_idx < len(token_ids) else ""

            processed.append({
                "id": m.get("id", ""),
                "question": question,
                "slug": m.get("slug", ""),
                "conditionId": m.get("conditionId", ""),
                "outcomes": json.dumps(outcomes),
                "outcomePrices": json.dumps(outcome_prices),
                "winning_outcome": winning_outcome,
                "winning_idx": winning_idx,
                "winning_token_id": winning_token,
                "endDate": m.get("endDate", ""),
                "closedTime": closed_time,
                "volume": m.get("volume", 0),
                "volumeNum": m.get("volumeNum", 0),
                "liquidity": m.get("liquidity", 0),
                "liquidityNum": m.get("liquidityNum", 0),
                "lastTradePrice": m.get("lastTradePrice", 0),
                "bestBid": m.get("bestBid", 0),
                "bestAsk": m.get("bestAsk", 0),
                "description": (m.get("description", "") or "")[:200],
                "groupItemTitle": m.get("groupItemTitle", ""),
                "enableOrderBook": m.get("enableOrderBook", True),
            })

        offset += limit

        if page % 20 == 0:
            oldest = data[-1].get("closedTime", "?")[:10] if data else "?"
            print(f"  Page {page}: {total_raw} raw, {len(processed)} kept, "
                  f"{excluded_crypto} crypto excluded (oldest: {oldest})", flush=True)

        # Gamma API has no documented rate limit but let's be polite
        if page % 50 == 0:
            time.sleep(1)

        if len(data) < limit:
            break

    print(f"\n  Total raw markets fetched: {total_raw}", flush=True)
    print(f"  Excluded (unresolved): {excluded_unresolved}", flush=True)
    print(f"  Excluded (before cutoff): {excluded_old}", flush=True)
    print(f"  Excluded (crypto short-term): {excluded_crypto}", flush=True)
    print(f"  Remaining resolved markets: {len(processed)}", flush=True)

    df = pd.DataFrame(processed)
    out_path = os.path.join(DATA_DIR, "markets_resolved.csv")
    df.to_csv(out_path, index=False)
    print(f"  Saved to {out_path}", flush=True)
    return df


def collect_price_histories(markets_df):
    """Fetch price histories for winning tokens on qualifying markets."""
    print("\n" + "=" * 60, flush=True)
    print("Step 2: Collecting price histories from CLOB API", flush=True)
    print("=" * 60, flush=True)

    qualifying = markets_df[
        (markets_df["winning_token_id"].notna()) &
        (markets_df["winning_token_id"] != "")
    ].copy()

    # Filter by lastTradePrice >= 0.90
    qualifying["ltp"] = pd.to_numeric(qualifying["lastTradePrice"], errors="coerce")
    qualifying = qualifying[
        (qualifying["ltp"].isna()) | (qualifying["ltp"] >= 0.90)
    ]

    print(f"  Qualifying markets (LTP >= 0.90): {len(qualifying)}", flush=True)

    # Cap and prioritize by volume
    if len(qualifying) > MAX_PRICE_HISTORY_MARKETS:
        qualifying["vol_num"] = pd.to_numeric(qualifying["volumeNum"], errors="coerce").fillna(0)
        qualifying = qualifying.sort_values("vol_num", ascending=False).head(MAX_PRICE_HISTORY_MARKETS)
        print(f"  Capped to top {MAX_PRICE_HISTORY_MARKETS} by volume", flush=True)

    all_prices = []
    fetched = 0
    errors = 0

    for i, (_, market) in enumerate(qualifying.iterrows()):
        token_id = market["winning_token_id"]
        market_id = market["id"]

        data = safe_request(
            f"{CLOB_BASE}/prices-history",
            params={"market": token_id, "interval": "all", "fidelity": "60"}
        )

        if data and "history" in data:
            for point in data["history"]:
                all_prices.append({
                    "market_id": market_id,
                    "token_id": token_id,
                    "timestamp": point.get("t", 0),
                    "price": point.get("p", 0),
                })
            fetched += 1
        else:
            errors += 1

        if (i + 1) % 50 == 0:
            print(f"  Progress: {i + 1}/{len(qualifying)} markets "
                  f"({fetched} OK, {errors} err, "
                  f"{len(all_prices)} points)", flush=True)

        time.sleep(CLOB_DELAY)

    print(f"\n  Final: {fetched} markets, {errors} errors, "
          f"{len(all_prices)} total price points", flush=True)

    df = pd.DataFrame(all_prices)
    out_path = os.path.join(DATA_DIR, "price_histories.csv")
    df.to_csv(out_path, index=False)
    print(f"  Saved to {out_path}", flush=True)
    return df


def collect_orderbook_snapshots(markets_df):
    """Collect order book snapshots for recently-resolved markets."""
    print("\n" + "=" * 60, flush=True)
    print("Step 3: Collecting order book snapshots", flush=True)
    print("=" * 60, flush=True)

    recent_cutoff = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = markets_df[
        (markets_df["closedTime"] >= recent_cutoff) &
        (markets_df["winning_token_id"].notna()) &
        (markets_df["winning_token_id"] != "")
    ]

    # Cap to 200 for speed
    if len(recent) > 200:
        recent = recent.head(200)

    print(f"  Recent markets (last 3 days): {len(recent)}", flush=True)

    snapshots = []
    now = datetime.utcnow().isoformat()

    for idx, (_, market) in enumerate(recent.iterrows()):
        token_id = market["winning_token_id"]
        market_id = market["id"]

        data = safe_request(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id}
        )

        if data and isinstance(data, dict):
            bids = data.get("bids", [])
            asks = data.get("asks", [])

            if not bids and not asks:
                continue

            try:
                bid_at_99 = sum(float(b.get("size", 0)) for b in bids
                               if float(b.get("price", 0)) >= 0.99)
                bid_at_98 = sum(float(b.get("size", 0)) for b in bids
                               if 0.98 <= float(b.get("price", 0)) < 0.99)
                total_bid = sum(float(b.get("size", 0)) for b in bids)
                ask_at_99 = sum(float(a.get("size", 0)) for a in asks
                               if float(a.get("price", 0)) <= 0.99)
                total_ask = sum(float(a.get("size", 0)) for a in asks)
                best_bid = max((float(b.get("price", 0)) for b in bids), default=0)
                best_ask = min((float(a.get("price", 0)) for a in asks), default=0) if asks else 0
            except (ValueError, TypeError):
                continue

            snapshots.append({
                "snapshot_time": now,
                "market_id": market_id,
                "token_id": token_id,
                "question": market["question"][:100],
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": best_ask - best_bid if best_ask > 0 and best_bid > 0 else None,
                "bid_at_99_plus": bid_at_99,
                "bid_at_98_99": bid_at_98,
                "total_bid_depth": total_bid,
                "ask_at_99_minus": ask_at_99,
                "total_ask_depth": total_ask,
                "num_bid_levels": len(bids),
                "num_ask_levels": len(asks),
            })

        time.sleep(CLOB_DELAY)

        if (idx + 1) % 50 == 0:
            print(f"  Order book progress: {idx + 1}/{len(recent)} "
                  f"({len(snapshots)} with data)", flush=True)

    print(f"  Collected {len(snapshots)} order book snapshots", flush=True)

    df = pd.DataFrame(snapshots)
    out_path = os.path.join(DATA_DIR, "orderbook_snapshots.csv")

    if os.path.exists(out_path) and not df.empty:
        existing = pd.read_csv(out_path)
        df = pd.concat([existing, df], ignore_index=True)

    if not df.empty:
        df.to_csv(out_path, index=False)
        print(f"  Saved to {out_path}", flush=True)
    else:
        print("  No order book data collected", flush=True)

    return df


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    start = time.time()
    print(f"Starting data collection at {datetime.utcnow().isoformat()}Z", flush=True)
    print(f"Lookback period: {LOOKBACK_DAYS} days", flush=True)
    print(f"Max price history markets: {MAX_PRICE_HISTORY_MARKETS}", flush=True)
    print(flush=True)

    markets_df = collect_resolved_markets()
    if markets_df.empty:
        print("\nNo resolved markets found. Exiting.", flush=True)
        sys.exit(1)

    prices_df = collect_price_histories(markets_df)
    orderbook_df = collect_orderbook_snapshots(markets_df)

    elapsed = time.time() - start
    print(f"\n{'=' * 60}", flush=True)
    print(f"Data collection complete in {elapsed / 60:.1f} minutes", flush=True)
    print(f"  Markets: {len(markets_df)}", flush=True)
    print(f"  Price points: {len(prices_df)}", flush=True)
    print(f"  Order book snapshots: {len(orderbook_df) if not orderbook_df.empty else 0}", flush=True)
    print(f"{'=' * 60}", flush=True)


if __name__ == "__main__":
    main()
