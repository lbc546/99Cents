#!/usr/bin/env python3
"""
Polymarket 99-Cent Arbitrage Analysis Engine

Reads collected data from data/ directory and answers 7 research questions
about the viability of a 99-cent settlement arbitrage strategy.
"""

import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RESULTS_PATH = os.path.join(DATA_DIR, "analysis_results.json")

# Configurable buy threshold — set via command line arg or default
# Usage: python analyze.py 0.98  (for $0.98 threshold)
BUY_THRESHOLD = 0.99  # will be overridden by CLI arg if provided


def load_data():
    """Load all collected CSV data."""
    markets_path = os.path.join(DATA_DIR, "markets_resolved.csv")
    prices_path = os.path.join(DATA_DIR, "price_histories.csv")
    orderbook_path = os.path.join(DATA_DIR, "orderbook_snapshots.csv")

    if not os.path.exists(markets_path):
        print("ERROR: markets_resolved.csv not found. Run collect_data.py first.")
        sys.exit(1)

    markets = pd.read_csv(markets_path)
    prices = pd.read_csv(prices_path) if os.path.exists(prices_path) else pd.DataFrame()
    orderbook = pd.read_csv(orderbook_path) if os.path.exists(orderbook_path) else pd.DataFrame()

    print(f"Loaded {len(markets)} markets, {len(prices)} price points, "
          f"{len(orderbook)} order book snapshots")
    return markets, prices, orderbook


def infer_category(question):
    """Infer market category from question text."""
    q = question.lower() if isinstance(question, str) else ""

    # Sports
    if any(w in q for w in ["tennis", "football", "soccer", "nba", "nfl", "nhl",
                             "mlb", "ufc", "mma", "boxing", "cricket", "f1",
                             "formula", "grand prix", "atp", "wta", "ncaa",
                             "premier league", "champions league", "la liga",
                             "bundesliga", "serie a", "ligue 1", "copa",
                             "super bowl", "world cup", "olympics"]):
        return "Sports"

    # Esports
    if any(w in q for w in ["dota", "counter-strike", "cs2", "csgo", "league of legends",
                             "lol ", "valorant", "esport"]):
        return "Esports"

    # Crypto (non-short-term, since short-term already excluded)
    if any(w in q for w in ["bitcoin", "btc", "ethereum", "eth ", "crypto",
                             "xrp", "solana", "sol ", "dogecoin", "doge"]):
        return "Crypto"

    # Politics
    if any(w in q for w in ["trump", "biden", "election", "congress", "senate",
                             "president", "democrat", "republican", "political",
                             "governor", "mayor", "vote", "poll", "legislation",
                             "executive order"]):
        return "Politics"

    # Economics/Finance
    if any(w in q for w in ["stock", "s&p", "nasdaq", "dow jones", "fed ",
                             "interest rate", "inflation", "gdp", "unemployment",
                             "earnings", "revenue", "ipo", "market cap"]):
        return "Economics/Finance"

    # Entertainment/Pop Culture
    if any(w in q for w in ["oscar", "grammy", "emmy", "movie", "album",
                             "celebrity", "taylor swift", "kanye", "elon musk",
                             "twitter", "tiktok", "youtube"]):
        return "Entertainment"

    # Weather/Science
    if any(w in q for w in ["weather", "temperature", "hurricane", "earthquake",
                             "climate", "nasa", "spacex", "launch"]):
        return "Science/Weather"

    return "Other"


def analyze_q1_opportunity_frequency(markets, prices):
    """Q1: How often do winning tokens trade at ≤$0.99 after market end?"""
    print("\n--- Q1: Opportunity Frequency ---")

    if prices.empty:
        print("  No price data available")
        return {"error": "No price data"}

    # Parse dates on markets
    markets_with_dates = markets.copy()
    markets_with_dates["end_epoch"] = pd.to_datetime(
        markets_with_dates["endDate"], errors="coerce"
    ).astype("int64") // 10**9

    # Merge prices with market end dates
    merged = prices.merge(
        markets_with_dates[["id", "end_epoch", "closedTime"]],
        left_on="market_id", right_on="id", how="inner"
    )

    # Filter: price <= threshold AND timestamp >= end_epoch (post-resolution)
    post_res_99 = merged[
        (merged["price"] <= BUY_THRESHOLD) &
        (merged["timestamp"] >= merged["end_epoch"])
    ]

    # Also find all post-resolution data points for context
    post_res_all = merged[merged["timestamp"] >= merged["end_epoch"]]

    # Count unique markets per day with ≤threshold prices
    post_res_99 = post_res_99.copy()
    post_res_99["date"] = pd.to_datetime(post_res_99["timestamp"], unit="s").dt.date
    daily_counts = post_res_99.groupby("date")["market_id"].nunique()

    # Trend: split into halves and compare
    if len(daily_counts) > 10:
        midpoint = len(daily_counts) // 2
        first_half_avg = daily_counts.iloc[:midpoint].mean()
        second_half_avg = daily_counts.iloc[midpoint:].mean()
        trend = "increasing" if second_half_avg > first_half_avg * 1.1 else \
                "decreasing" if second_half_avg < first_half_avg * 0.9 else "stable"
    else:
        first_half_avg = second_half_avg = daily_counts.mean() if len(daily_counts) > 0 else 0
        trend = "insufficient data"

    # What fraction of all post-resolution data is at ≤threshold?
    pct_at_99 = len(post_res_99) / len(post_res_all) * 100 if len(post_res_all) > 0 else 0

    results = {
        "total_markets_with_99_post_resolution": int(post_res_99["market_id"].nunique()),
        "total_price_points_at_99": int(len(post_res_99)),
        "total_post_resolution_points": int(len(post_res_all)),
        "pct_post_resolution_at_99": round(pct_at_99, 2),
        "mean_daily_opportunities": round(daily_counts.mean(), 2) if len(daily_counts) > 0 else 0,
        "median_daily_opportunities": round(daily_counts.median(), 2) if len(daily_counts) > 0 else 0,
        "min_daily": int(daily_counts.min()) if len(daily_counts) > 0 else 0,
        "max_daily": int(daily_counts.max()) if len(daily_counts) > 0 else 0,
        "first_half_avg": round(first_half_avg, 2),
        "second_half_avg": round(second_half_avg, 2),
        "trend": trend,
        "num_days_observed": int(len(daily_counts)),
        "daily_series": {str(k): int(v) for k, v in daily_counts.items()} if len(daily_counts) < 200 else {},
    }

    print(f"  Markets with ≤$0.99 post-resolution: {results['total_markets_with_99_post_resolution']}")
    print(f"  Mean daily opportunities: {results['mean_daily_opportunities']}")
    print(f"  Trend: {results['trend']}")
    return results


def analyze_q2_liquidity_depth(markets, prices, orderbook):
    """Q2: What liquidity is available at $0.99?"""
    print("\n--- Q2: Liquidity Depth ---")

    results = {}

    # From order book snapshots
    if not orderbook.empty and "bid_at_99_plus" in orderbook.columns:
        ob = orderbook.copy()
        ob["bid_at_99_plus"] = pd.to_numeric(ob["bid_at_99_plus"], errors="coerce")
        has_depth = ob[ob["bid_at_99_plus"] > 0]

        results["orderbook"] = {
            "total_snapshots": int(len(ob)),
            "snapshots_with_99_bids": int(len(has_depth)),
            "pct_with_99_bids": round(len(has_depth) / len(ob) * 100, 2) if len(ob) > 0 else 0,
            "mean_depth_at_99": round(has_depth["bid_at_99_plus"].mean(), 2) if len(has_depth) > 0 else 0,
            "median_depth_at_99": round(has_depth["bid_at_99_plus"].median(), 2) if len(has_depth) > 0 else 0,
            "p25_depth": round(has_depth["bid_at_99_plus"].quantile(0.25), 2) if len(has_depth) > 0 else 0,
            "p75_depth": round(has_depth["bid_at_99_plus"].quantile(0.75), 2) if len(has_depth) > 0 else 0,
            "max_depth": round(has_depth["bid_at_99_plus"].max(), 2) if len(has_depth) > 0 else 0,
        }
        print(f"  Order book: {results['orderbook']['snapshots_with_99_bids']}/{results['orderbook']['total_snapshots']} "
              f"had $0.99+ bids, median depth: {results['orderbook']['median_depth_at_99']}")
    else:
        results["orderbook"] = {"note": "No order book data available"}
        print("  No order book data available")

    # From market volume data (proxy for liquidity)
    markets_copy = markets.copy()
    markets_copy["vol"] = pd.to_numeric(markets_copy["volumeNum"], errors="coerce").fillna(0)
    results["market_volume"] = {
        "mean_volume": round(markets_copy["vol"].mean(), 2),
        "median_volume": round(markets_copy["vol"].median(), 2),
        "p25_volume": round(markets_copy["vol"].quantile(0.25), 2),
        "p75_volume": round(markets_copy["vol"].quantile(0.75), 2),
        "total_volume": round(markets_copy["vol"].sum(), 2),
    }
    print(f"  Market volume: median=${results['market_volume']['median_volume']}, "
          f"mean=${results['market_volume']['mean_volume']}")

    # From price history: how long do prices stay at ≤0.99 post-resolution?
    if not prices.empty:
        markets_with_dates = markets.copy()
        markets_with_dates["end_epoch"] = pd.to_datetime(
            markets_with_dates["endDate"], errors="coerce"
        ).astype("int64") // 10**9

        merged = prices.merge(
            markets_with_dates[["id", "end_epoch"]],
            left_on="market_id", right_on="id", how="inner"
        )

        post_res = merged[merged["timestamp"] >= merged["end_epoch"]]
        if len(post_res) > 0:
            at_99 = post_res[post_res["price"] <= BUY_THRESHOLD]
            pct_time_at_99 = len(at_99) / len(post_res) * 100
            results["price_time_at_99_pct"] = round(pct_time_at_99, 2)
            print(f"  Price history: {pct_time_at_99:.1f}% of post-resolution time at ≤${BUY_THRESHOLD}")

    return results


def analyze_q3_settlement_time(markets):
    """Q3: How long does settlement take?"""
    print("\n--- Q3: Settlement Time ---")

    m = markets.copy()
    m["end_dt"] = pd.to_datetime(m["endDate"], errors="coerce")
    m["closed_dt"] = pd.to_datetime(m["closedTime"], errors="coerce")
    m["settlement_hours"] = (m["closed_dt"] - m["end_dt"]).dt.total_seconds() / 3600

    # Filter to reasonable values (>= 0, < 720 hours / 30 days)
    valid = m[(m["settlement_hours"] >= 0) & (m["settlement_hours"] < 720)].copy()

    if valid.empty:
        print("  No valid settlement time data")
        return {"error": "No valid settlement data"}

    # Add category
    valid["category"] = valid["question"].apply(infer_category)

    # Overall stats
    overall = {
        "mean_hours": round(valid["settlement_hours"].mean(), 2),
        "median_hours": round(valid["settlement_hours"].median(), 2),
        "p25_hours": round(valid["settlement_hours"].quantile(0.25), 2),
        "p75_hours": round(valid["settlement_hours"].quantile(0.75), 2),
        "min_hours": round(valid["settlement_hours"].min(), 4),
        "max_hours": round(valid["settlement_hours"].max(), 2),
        "pct_under_4hrs": round((valid["settlement_hours"] < 4).mean() * 100, 2),
        "pct_under_24hrs": round((valid["settlement_hours"] < 24).mean() * 100, 2),
        "pct_under_48hrs": round((valid["settlement_hours"] < 48).mean() * 100, 2),
        "total_markets": int(len(valid)),
    }

    # By category
    by_cat = valid.groupby("category")["settlement_hours"].agg(
        ["mean", "median", "count"]
    ).round(2)
    by_cat.columns = ["mean_hours", "median_hours", "count"]
    by_cat = by_cat.sort_values("count", ascending=False)

    results = {
        "overall": overall,
        "by_category": by_cat.reset_index().to_dict("records"),
    }

    print(f"  Overall: median {overall['median_hours']:.1f}h, mean {overall['mean_hours']:.1f}h")
    print(f"  {overall['pct_under_4hrs']:.1f}% settle within 4 hours")
    print(f"  {overall['pct_under_24hrs']:.1f}% settle within 24 hours")

    # Save settlement data for chart generation
    valid[["id", "category", "settlement_hours"]].to_csv(
        os.path.join(DATA_DIR, "settlement_times.csv"), index=False
    )

    return results


def analyze_q4_categories(markets, prices):
    """Q4: Which categories have the most opportunities and best liquidity?"""
    print("\n--- Q4: Market Categories ---")

    m = markets.copy()
    m["category"] = m["question"].apply(infer_category)
    m["vol"] = pd.to_numeric(m["volumeNum"], errors="coerce").fillna(0)

    # Basic category stats
    cat_stats = m.groupby("category").agg(
        total_markets=("id", "count"),
        avg_volume=("vol", "mean"),
        median_volume=("vol", "median"),
        total_volume=("vol", "sum"),
    ).round(2)

    # Add opportunity rate (if price data available)
    if not prices.empty:
        markets_with_dates = m.copy()
        markets_with_dates["end_epoch"] = pd.to_datetime(
            markets_with_dates["endDate"], errors="coerce"
        ).astype("int64") // 10**9

        merged = prices.merge(
            markets_with_dates[["id", "end_epoch", "category"]],
            left_on="market_id", right_on="id", how="inner"
        )
        post_res = merged[merged["timestamp"] >= merged["end_epoch"]]
        at_99 = post_res[post_res["price"] <= BUY_THRESHOLD]

        markets_with_99 = at_99.groupby("category")["market_id"].nunique().reset_index()
        markets_with_99.columns = ["category", "markets_with_99_opps"]
        cat_stats = cat_stats.reset_index().merge(markets_with_99, on="category", how="left")
        cat_stats["markets_with_99_opps"] = cat_stats["markets_with_99_opps"].fillna(0).astype(int)
        cat_stats["opp_rate_pct"] = (cat_stats["markets_with_99_opps"] / cat_stats["total_markets"] * 100).round(2)
    else:
        cat_stats = cat_stats.reset_index()

    cat_stats = cat_stats.sort_values("total_markets", ascending=False)

    results = {
        "categories": cat_stats.to_dict("records"),
        "total_categories": int(len(cat_stats)),
    }

    print(f"  Found {len(cat_stats)} categories:")
    for _, row in cat_stats.iterrows():
        print(f"    {row['category']}: {row['total_markets']} markets, "
              f"median vol ${row['median_volume']}")

    return results


def analyze_q5_profitability(q1_results, q2_results, q3_results):
    """Q5: Three-scenario profitability model with $1K capital."""
    print("\n--- Q5: Profitability Model ---")

    capital = 1000
    daily_opps = q1_results.get("mean_daily_opportunities", 0)
    settlement_hours = q3_results.get("overall", {}).get("median_hours", 4)

    # Estimate liquidity per opportunity from available data
    if "orderbook" in q2_results and isinstance(q2_results["orderbook"], dict):
        avg_liquidity = q2_results["orderbook"].get("median_depth_at_99", 50)
    else:
        avg_liquidity = 50  # conservative default

    # If no real data, use reasonable estimates
    if daily_opps == 0:
        print("  WARNING: No opportunity frequency data. Using estimates.")
        daily_opps = 10  # conservative estimate for fee-free markets

    if avg_liquidity == 0:
        avg_liquidity = 50

    scenarios = {
        "Conservative": {
            "fill_rate": 0.30,
            "avg_fill_size": min(avg_liquidity * 0.5, 50),
            "buy_price": BUY_THRESHOLD,
            "gas_per_trade": 0.10,
            "dispute_loss_rate": 0.015,
            "description": "30% fill rate, $50 max position, high gas"
        },
        "Base Case": {
            "fill_rate": 0.50,
            "avg_fill_size": min(avg_liquidity * 0.7, 100),
            "buy_price": BUY_THRESHOLD,
            "gas_per_trade": 0.05,
            "dispute_loss_rate": 0.005,
            "description": "50% fill rate, $100 max position, normal gas"
        },
        "Optimistic": {
            "fill_rate": 0.80,
            "avg_fill_size": min(avg_liquidity, 200),
            "buy_price": BUY_THRESHOLD,
            "gas_per_trade": 0.03,
            "dispute_loss_rate": 0.002,
            "description": "80% fill rate, $200 max position, low gas"
        }
    }

    results = {}
    for name, s in scenarios.items():
        fills_per_day = daily_opps * s["fill_rate"]
        gross_per_trade = (1.00 - s["buy_price"]) * s["avg_fill_size"]
        net_per_trade = gross_per_trade - s["gas_per_trade"]

        # Capital turnover constraint
        if settlement_hours > 0:
            turnover_per_day = 24 / settlement_hours
        else:
            turnover_per_day = 6  # assume 4-hour settlement
        max_concurrent_positions = capital / s["avg_fill_size"] if s["avg_fill_size"] > 0 else 0
        max_daily_fills = max_concurrent_positions * turnover_per_day
        effective_fills = min(fills_per_day, max_daily_fills)

        daily_gross = net_per_trade * effective_fills
        daily_dispute_loss = s["dispute_loss_rate"] * s["avg_fill_size"] * effective_fills
        daily_net = daily_gross - daily_dispute_loss

        monthly_net = daily_net * 30
        annual_net = daily_net * 365
        daily_roi = (daily_net / capital) * 100 if capital > 0 else 0
        annual_roi = daily_roi * 365

        results[name] = {
            "description": s["description"],
            "fills_per_day": round(fills_per_day, 1),
            "effective_fills_per_day": round(effective_fills, 1),
            "avg_fill_size": round(s["avg_fill_size"], 2),
            "gross_per_trade": round(gross_per_trade, 4),
            "net_per_trade": round(net_per_trade, 4),
            "capital_turnover_per_day": round(turnover_per_day, 2),
            "daily_net_profit": round(daily_net, 2),
            "monthly_net_profit": round(monthly_net, 2),
            "annual_net_profit": round(annual_net, 2),
            "daily_roi_pct": round(daily_roi, 4),
            "annual_roi_pct": round(annual_roi, 2),
        }

        print(f"  {name}: ${daily_net:.2f}/day, ${monthly_net:.2f}/month, "
              f"{annual_roi:.1f}% annual ROI")

    results["_inputs"] = {
        "capital": capital,
        "daily_opportunities": daily_opps,
        "avg_liquidity_estimate": avg_liquidity,
        "settlement_hours": settlement_hours,
    }

    return results


def analyze_q6_competition(markets, prices):
    """Q6: Competition trends and margin compression."""
    print("\n--- Q6: Competition & Trends ---")

    if prices.empty:
        print("  No price data for trend analysis")
        return {"error": "No price data"}

    markets_with_dates = markets.copy()
    markets_with_dates["end_epoch"] = pd.to_datetime(
        markets_with_dates["endDate"], errors="coerce"
    ).astype("int64") // 10**9

    merged = prices.merge(
        markets_with_dates[["id", "end_epoch"]],
        left_on="market_id", right_on="id", how="inner"
    )

    post_res = merged[merged["timestamp"] >= merged["end_epoch"]].copy()

    if post_res.empty:
        print("  No post-resolution price data")
        return {"error": "No post-resolution data"}

    post_res["month"] = pd.to_datetime(post_res["timestamp"], unit="s").dt.to_period("M")

    monthly = post_res.groupby("month").agg(
        mean_price=("price", "mean"),
        median_price=("price", "median"),
        pct_at_threshold=("price", lambda x: (x <= BUY_THRESHOLD).mean() * 100),
        pct_at_99=("price", lambda x: (x <= 0.99).mean() * 100),
        pct_at_995=("price", lambda x: (x <= 0.995).mean() * 100),
        num_markets=("market_id", "nunique"),
        num_points=("price", "count"),
    ).round(4)

    # Trend detection
    if len(monthly) >= 3:
        prices_series = monthly["mean_price"].values
        x = np.arange(len(prices_series))
        if len(x) > 1:
            slope = np.polyfit(x, prices_series, 1)[0]
            trend_direction = "compressing (more competition)" if slope > 0.001 else \
                              "expanding (less competition)" if slope < -0.001 else "stable"
        else:
            slope = 0
            trend_direction = "insufficient data"
    else:
        slope = 0
        trend_direction = "insufficient data"

    results = {
        "monthly_stats": [
            {
                "month": str(idx),
                "mean_price": round(row["mean_price"], 4),
                "pct_at_threshold": round(row["pct_at_threshold"], 2),
                "pct_at_99": round(row["pct_at_99"], 2),
                "pct_at_995": round(row["pct_at_995"], 2),
                "num_markets": int(row["num_markets"]),
            }
            for idx, row in monthly.iterrows()
        ],
        "price_trend_slope": round(float(slope), 6),
        "trend_direction": trend_direction,
    }

    print(f"  Trend: {trend_direction} (slope: {slope:.6f})")
    print(f"  Months analyzed: {len(monthly)}")

    return results


def analyze_q7_risk(markets, prices):
    """Q7: Risk assessment — false positives, disputes, loss scenarios."""
    print("\n--- Q7: Risk Assessment ---")

    m = markets.copy()
    m["category"] = m["question"].apply(infer_category)

    total = len(m)

    # Check for markets where YES was the winning outcome vs not
    # In our dataset, all should be resolved, but check the distribution
    outcome_dist = m["winning_outcome"].value_counts()

    # Look for potential dispute signals
    # Markets with unusual resolution patterns
    m["vol"] = pd.to_numeric(m["volumeNum"], errors="coerce").fillna(0)

    # Risk proxy: markets with very low volume might have less reliable resolution
    low_vol = m[m["vol"] < 100]
    high_vol = m[m["vol"] >= 100]

    # Price-based risk: if prices data shows the winning token was < 0.50 at some point
    # near close, that's a sign the outcome was uncertain
    risk_from_prices = {}
    if not prices.empty:
        markets_with_dates = m.copy()
        markets_with_dates["end_epoch"] = pd.to_datetime(
            markets_with_dates["endDate"], errors="coerce"
        ).astype("int64") // 10**9

        merged = prices.merge(
            markets_with_dates[["id", "end_epoch"]],
            left_on="market_id", right_on="id", how="inner"
        )

        # Find markets where price was very low near the end but still won
        # These represent potential "surprise" resolutions
        near_end = merged[
            (merged["timestamp"] >= merged["end_epoch"] - 3600) &  # 1 hour before end
            (merged["timestamp"] <= merged["end_epoch"])
        ]

        if len(near_end) > 0:
            near_end_stats = near_end.groupby("market_id")["price"].agg(["min", "mean"])
            surprise_wins = near_end_stats[near_end_stats["min"] < 0.50]
            risk_from_prices = {
                "markets_with_late_price_data": int(len(near_end_stats)),
                "surprise_wins_below_50": int(len(surprise_wins)),
                "surprise_rate_pct": round(len(surprise_wins) / len(near_end_stats) * 100, 2) if len(near_end_stats) > 0 else 0,
            }

    # Category-level risk (some categories more disputeable)
    cat_risk = m.groupby("category").agg(
        total=("id", "count"),
        avg_volume=("vol", "mean"),
    ).round(2)

    results = {
        "total_resolved_markets": int(total),
        "winning_outcome_distribution": outcome_dist.to_dict(),
        "low_volume_markets_pct": round(len(low_vol) / total * 100, 2) if total > 0 else 0,
        "price_based_risk": risk_from_prices,
        "category_risk": cat_risk.reset_index().to_dict("records"),
        "dispute_estimate": {
            "note": "UMA oracle data shows ~1.5% dispute rate across all Polymarket markets",
            "estimated_dispute_rate_pct": 1.5,
            "estimated_loss_per_dispute": "Full position value ($0.99/share)",
        },
        "key_risks": [
            "Resolution flip during UMA 2-hour challenge period",
            "Market trading at 0.99 before actual resolution (premature entry)",
            "Gas costs exceeding profit on small positions",
            "Capital lockup during settlement reducing effective ROI",
            "API latency causing missed fills or stale data",
        ]
    }

    print(f"  Total markets analyzed: {total}")
    print(f"  Low volume (<$100) markets: {results['low_volume_markets_pct']:.1f}%")
    if risk_from_prices:
        print(f"  Surprise wins (price <$0.50 near close): {risk_from_prices.get('surprise_rate_pct', 'N/A')}%")

    return results


def main():
    global BUY_THRESHOLD, RESULTS_PATH
    if len(sys.argv) > 1:
        try:
            BUY_THRESHOLD = float(sys.argv[1])
        except ValueError:
            pass

    # Use threshold-specific results file
    threshold_label = f"{BUY_THRESHOLD:.2f}".replace(".", "")
    RESULTS_PATH = os.path.join(DATA_DIR, f"analysis_results_{threshold_label}.json")

    print("=" * 60)
    print(f"Polymarket Arbitrage Analysis (threshold: ${BUY_THRESHOLD:.2f})")
    print("=" * 60)

    markets, prices, orderbook = load_data()

    # Run all analyses
    q1 = analyze_q1_opportunity_frequency(markets, prices)
    q2 = analyze_q2_liquidity_depth(markets, prices, orderbook)
    q3 = analyze_q3_settlement_time(markets)
    q4 = analyze_q4_categories(markets, prices)
    q5 = analyze_q5_profitability(q1, q2, q3)
    q6 = analyze_q6_competition(markets, prices)
    q7 = analyze_q7_risk(markets, prices)

    # Save all results
    all_results = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "buy_threshold": BUY_THRESHOLD,
        "data_summary": {
            "total_markets": int(len(markets)),
            "total_price_points": int(len(prices)),
            "total_orderbook_snapshots": int(len(orderbook)),
        },
        "q1_opportunity_frequency": q1,
        "q2_liquidity_depth": q2,
        "q3_settlement_time": q3,
        "q4_categories": q4,
        "q5_profitability": q5,
        "q6_competition": q6,
        "q7_risk": q7,
    }

    # Custom serializer for numpy/pandas types
    def default_serializer(obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        return str(obj)

    with open(RESULTS_PATH, "w") as f:
        json.dump(all_results, f, indent=2, default=default_serializer)

    print(f"\n{'=' * 60}")
    print(f"Analysis complete. Results saved to {RESULTS_PATH}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
