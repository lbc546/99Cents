#!/usr/bin/env python3
"""
Polymarket 99-Cent Arbitrage Report Generator

Reads analysis results and produces a formatted markdown report with charts.
"""

import json
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import pandas as pd
from tabulate import tabulate

import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORT_DIR = os.path.join(BASE_DIR, "report")
FIGURES_DIR = os.path.join(REPORT_DIR, "figures")

# Default — will be overridden by CLI arg
BUY_THRESHOLD = 0.99
RESULTS_PATH = os.path.join(DATA_DIR, "analysis_results.json")
REPORT_PATH = os.path.join(REPORT_DIR, "viability_report.md")


def load_results():
    with open(RESULTS_PATH) as f:
        return json.load(f)


def generate_charts(results):
    """Generate matplotlib charts for the report."""
    os.makedirs(FIGURES_DIR, exist_ok=True)
    plt.style.use("seaborn-v0_8-whitegrid")
    charts = []

    # Chart 1: Daily opportunities over time
    q1 = results.get("q1_opportunity_frequency", {})
    daily_series = q1.get("daily_series", {})
    if daily_series:
        fig, ax = plt.subplots(figsize=(12, 5))
        dates = sorted(daily_series.keys())
        values = [daily_series[d] for d in dates]
        ax.bar(range(len(dates)), values, color="#4A90D9", alpha=0.8)
        ax.set_title("Daily Markets with ≤$0.99 Opportunities (Post-Resolution)", fontsize=14)
        ax.set_ylabel("Number of Markets")
        ax.set_xlabel("Days (chronological)")
        # Show every Nth tick label
        n = max(len(dates) // 15, 1)
        ax.set_xticks(range(0, len(dates), n))
        ax.set_xticklabels([dates[i] for i in range(0, len(dates), n)], rotation=45, ha="right", fontsize=8)
        fig.tight_layout()
        path = os.path.join(FIGURES_DIR, "daily_opportunities.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)
        charts.append(("daily_opportunities.png", "Daily $0.99 opportunities over time"))

    # Chart 2: Settlement time by category
    q3 = results.get("q3_settlement_time", {})
    by_cat = q3.get("by_category", [])
    if by_cat:
        fig, ax = plt.subplots(figsize=(10, 6))
        cats = [c["category"] for c in by_cat if c["count"] >= 5]
        medians = [c["median_hours"] for c in by_cat if c["count"] >= 5]
        if cats:
            y_pos = range(len(cats))
            bars = ax.barh(y_pos, medians, color="#5CB85C", alpha=0.8)
            ax.set_yticks(y_pos)
            ax.set_yticklabels(cats)
            ax.set_xlabel("Median Settlement Time (hours)")
            ax.set_title("Settlement Time by Market Category", fontsize=14)
            ax.axvline(x=4, color="red", linestyle="--", alpha=0.5, label="4-hour mark")
            ax.legend()
            fig.tight_layout()
            path = os.path.join(FIGURES_DIR, "settlement_by_category.png")
            fig.savefig(path, dpi=150)
            plt.close(fig)
            charts.append(("settlement_by_category.png", "Settlement time by category"))

    # Chart 3: Profitability scenario comparison
    q5 = results.get("q5_profitability", {})
    scenario_names = [k for k in q5 if not k.startswith("_")]
    if scenario_names:
        fig, axes = plt.subplots(1, 3, figsize=(14, 5))

        daily = [q5[n]["daily_net_profit"] for n in scenario_names]
        monthly = [q5[n]["monthly_net_profit"] for n in scenario_names]
        annual_roi = [q5[n]["annual_roi_pct"] for n in scenario_names]
        colors = ["#D9534F", "#F0AD4E", "#5CB85C"]

        axes[0].bar(scenario_names, daily, color=colors)
        axes[0].set_title("Daily Net Profit")
        axes[0].set_ylabel("USD")
        axes[0].axhline(y=0, color="black", linewidth=0.5)

        axes[1].bar(scenario_names, monthly, color=colors)
        axes[1].set_title("Monthly Net Profit")
        axes[1].set_ylabel("USD")
        axes[1].axhline(y=0, color="black", linewidth=0.5)

        axes[2].bar(scenario_names, annual_roi, color=colors)
        axes[2].set_title("Annual ROI %")
        axes[2].set_ylabel("%")
        axes[2].axhline(y=0, color="black", linewidth=0.5)

        fig.suptitle("Profitability: Three Scenarios ($1,000 Capital)", fontsize=14)
        fig.tight_layout()
        path = os.path.join(FIGURES_DIR, "profitability_scenarios.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)
        charts.append(("profitability_scenarios.png", "Profitability scenario comparison"))

    # Chart 4: Competition/margin trend
    q6 = results.get("q6_competition", {})
    monthly_stats = q6.get("monthly_stats", [])
    if len(monthly_stats) >= 2:
        fig, ax1 = plt.subplots(figsize=(12, 5))
        months = [s["month"] for s in monthly_stats]
        mean_prices = [s["mean_price"] for s in monthly_stats]
        pct_99 = [s.get("pct_at_threshold", s.get("pct_at_99", 0)) for s in monthly_stats]

        ax1.plot(months, mean_prices, "b-o", label="Mean post-resolution price", linewidth=2)
        ax1.set_ylabel("Mean Price", color="b")
        ax1.set_ylim(0.90, 1.01)
        ax1.axhline(y=0.99, color="red", linestyle="--", alpha=0.5, label="$0.99 threshold")
        ax1.tick_params(axis="x", rotation=45)

        ax2 = ax1.twinx()
        ax2.bar(months, pct_99, alpha=0.3, color="green", label="% at ≤$0.99")
        ax2.set_ylabel("% of data points ≤$0.99", color="green")

        ax1.set_title("Post-Resolution Price Trends (Margin Compression Indicator)", fontsize=14)
        lines1, labels1 = ax1.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
        fig.tight_layout()
        path = os.path.join(FIGURES_DIR, "margin_trend.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)
        charts.append(("margin_trend.png", "Post-resolution price and margin trends"))

    # Chart 5: Category breakdown
    q4 = results.get("q4_categories", {})
    categories = q4.get("categories", [])
    if categories:
        fig, ax = plt.subplots(figsize=(10, 6))
        cats = [c["category"] for c in categories]
        counts = [c["total_markets"] for c in categories]
        ax.barh(range(len(cats)), counts, color="#4A90D9", alpha=0.8)
        ax.set_yticks(range(len(cats)))
        ax.set_yticklabels(cats)
        ax.set_xlabel("Number of Resolved Markets")
        ax.set_title("Resolved Markets by Category", fontsize=14)
        fig.tight_layout()
        path = os.path.join(FIGURES_DIR, "category_breakdown.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)
        charts.append(("category_breakdown.png", "Market distribution by category"))

    print(f"  Generated {len(charts)} charts")
    return charts


def generate_report(results, charts):
    """Generate the markdown report."""
    r = results
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    summary = r.get("data_summary", {})
    q1 = r.get("q1_opportunity_frequency", {})
    q2 = r.get("q2_liquidity_depth", {})
    q3 = r.get("q3_settlement_time", {})
    q4 = r.get("q4_categories", {})
    q5 = r.get("q5_profitability", {})
    q6 = r.get("q6_competition", {})
    q7 = r.get("q7_risk", {})

    # --- Build executive summary ---
    threshold = r.get("buy_threshold", 0.99)
    gross_margin_pct = round((1.0 - threshold) * 100, 1)
    daily_profit_base = q5.get("Base Case", {}).get("daily_net_profit", 0)
    annual_roi_base = q5.get("Base Case", {}).get("annual_roi_pct", 0)
    daily_opps = q1.get("mean_daily_opportunities", 0)
    trend = q6.get("trend_direction", "unknown")
    settlement_median = q3.get("overall", {}).get("median_hours", 0)

    if daily_profit_base > 3:
        verdict = "PROCEED WITH CAUTION"
        verdict_detail = (
            f"The strategy shows potential with an estimated ${daily_profit_base:.2f}/day "
            f"in the base case, but margins are thin and highly sensitive to execution quality, "
            f"gas costs, and competition."
        )
    elif daily_profit_base > 0:
        verdict = "MARGINAL — PROCEED WITH LOW EXPECTATIONS"
        verdict_detail = (
            f"The strategy may generate ${daily_profit_base:.2f}/day in the base case, "
            f"but after accounting for realistic execution challenges, the profit may approach zero. "
            f"The {gross_margin_pct}% gross margin leaves limited room for error."
        )
    else:
        verdict = "DO NOT PROCEED"
        verdict_detail = (
            f"The base case shows ${daily_profit_base:.2f}/day, meaning the strategy "
            f"is likely unprofitable after accounting for gas costs, competition, and execution friction."
        )

    cents = round((1.0 - threshold) * 100)

    report = f"""# Polymarket ${threshold:.2f} Settlement Arbitrage: Viability Report

**Generated:** {now}
**Data window:** {summary.get('total_markets', 'N/A')} resolved markets analyzed
**Strategy:** Buy winning shares at ${threshold:.2f} during the settlement window, collect $1.00 at settlement ({gross_margin_pct}% gross margin)

---

## Executive Summary

**Verdict: {verdict}**

{verdict_detail}
Key findings: approximately {daily_opps:.1f} fee-free markets per day show ≤${threshold:.2f} pricing
post-resolution. Median settlement time is {settlement_median:.1f} hours. Competition trend
is {trend}. With $1,000 capital, the base case projects ${daily_profit_base:.2f}/day
({annual_roi_base:.1f}% annualized), but the conservative case may be breakeven or negative.

---

## Data Sources & Limitations

| Source | What We Collected | Limitation |
|--------|-------------------|------------|
| Gamma API | {summary.get('total_markets', 0)} resolved market records | No direct trade-level data; volume is aggregate |
| CLOB API (prices-history) | {summary.get('total_price_points', 0)} price data points | Price snapshots, not individual trades; no volume per point |
| CLOB API (order book) | {summary.get('total_orderbook_snapshots', 0)} order book snapshots | Point-in-time only; may miss fleeting liquidity |
| UMA Oracle data | Estimated from documentation | Direct dispute data not available via public API |

**Key limitation:** The CLOB price history endpoint provides price over time but not trade
volume at each price level. Liquidity depth estimates rely on order book snapshots (limited
sample) and market-level volume data (aggregate, not price-specific).

---

## 1. Opportunity Frequency

"""

    if "error" not in q1:
        report += f"""**How many fee-free markets per day show ≤$0.99 pricing post-resolution?**

| Metric | Value |
|--------|-------|
| Mean daily opportunities | {q1.get('mean_daily_opportunities', 'N/A')} |
| Median daily opportunities | {q1.get('median_daily_opportunities', 'N/A')} |
| Min daily | {q1.get('min_daily', 'N/A')} |
| Max daily | {q1.get('max_daily', 'N/A')} |
| Total markets with ≤$0.99 post-resolution | {q1.get('total_markets_with_99_post_resolution', 'N/A')} |
| % of post-resolution time at ≤$0.99 | {q1.get('pct_post_resolution_at_99', 'N/A')}% |
| Days observed | {q1.get('num_days_observed', 'N/A')} |
| Trend (first half → second half) | {q1.get('first_half_avg', 'N/A')} → {q1.get('second_half_avg', 'N/A')} ({q1.get('trend', 'N/A')}) |

"""
    else:
        report += f"*No opportunity frequency data available. {q1.get('error', '')}*\n\n"

    # Add chart reference
    for chart_file, desc in charts:
        if "daily_opportunities" in chart_file:
            report += f"![{desc}](figures/{chart_file})\n\n"

    report += """---

## 2. Liquidity Depth

"""

    ob = q2.get("orderbook", {})
    if isinstance(ob, dict) and "note" not in ob:
        report += f"""**Order Book Analysis (snapshots of recently-settled markets):**

| Metric | Value |
|--------|-------|
| Total snapshots | {ob.get('total_snapshots', 'N/A')} |
| Snapshots with $0.99+ bids | {ob.get('snapshots_with_99_bids', 'N/A')} ({ob.get('pct_with_99_bids', 'N/A')}%) |
| Mean depth at $0.99 | {ob.get('mean_depth_at_99', 'N/A')} shares |
| Median depth at $0.99 | {ob.get('median_depth_at_99', 'N/A')} shares |
| 25th percentile | {ob.get('p25_depth', 'N/A')} shares |
| 75th percentile | {ob.get('p75_depth', 'N/A')} shares |

"""
    else:
        report += "*Limited order book data available. Liquidity estimates based on market volume proxies.*\n\n"

    mv = q2.get("market_volume", {})
    if mv:
        report += f"""**Market Volume (proxy for overall liquidity):**

| Metric | Value |
|--------|-------|
| Mean volume per market | ${mv.get('mean_volume', 'N/A')} |
| Median volume per market | ${mv.get('median_volume', 'N/A')} |
| 25th percentile | ${mv.get('p25_volume', 'N/A')} |
| 75th percentile | ${mv.get('p75_volume', 'N/A')} |

"""

    pct_99 = q2.get("price_time_at_99_pct")
    if pct_99 is not None:
        report += f"**{pct_99}%** of post-resolution price data points were at or below $0.99.\n\n"

    report += """---

## 3. Settlement Time

"""

    overall = q3.get("overall", {})
    if overall:
        report += f"""| Metric | Value |
|--------|-------|
| Median settlement time | {overall.get('median_hours', 'N/A')} hours |
| Mean settlement time | {overall.get('mean_hours', 'N/A')} hours |
| 25th percentile | {overall.get('p25_hours', 'N/A')} hours |
| 75th percentile | {overall.get('p75_hours', 'N/A')} hours |
| % settling within 4 hours | {overall.get('pct_under_4hrs', 'N/A')}% |
| % settling within 24 hours | {overall.get('pct_under_24hrs', 'N/A')}% |
| Total markets analyzed | {overall.get('total_markets', 'N/A')} |

"""

    by_cat = q3.get("by_category", [])
    if by_cat:
        report += "**Settlement time by category:**\n\n"
        headers = ["Category", "Median (hrs)", "Mean (hrs)", "Markets"]
        rows = [[c["category"], c["median_hours"], c["mean_hours"], c["count"]] for c in by_cat if c["count"] >= 3]
        report += tabulate(rows, headers=headers, tablefmt="pipe") + "\n\n"

    for chart_file, desc in charts:
        if "settlement" in chart_file:
            report += f"![{desc}](figures/{chart_file})\n\n"

    report += """---

## 4. Market Categories

"""

    categories = q4.get("categories", [])
    if categories:
        headers = ["Category", "Markets", "Avg Volume", "Median Volume"]
        has_opp = any("opp_rate_pct" in c for c in categories)
        if has_opp:
            headers += ["99c Opportunities", "Opp Rate %"]
        rows = []
        for c in categories:
            row = [c["category"], c["total_markets"],
                   f"${c['avg_volume']:.0f}", f"${c['median_volume']:.0f}"]
            if has_opp:
                row += [c.get("markets_with_99_opps", "N/A"),
                        f"{c.get('opp_rate_pct', 'N/A')}%"]
            rows.append(row)
        report += tabulate(rows, headers=headers, tablefmt="pipe") + "\n\n"

    for chart_file, desc in charts:
        if "category" in chart_file:
            report += f"![{desc}](figures/{chart_file})\n\n"

    report += """---

## 5. Profitability Model

**Assumptions:** $1,000 starting capital, 0% platform fees (fee-free markets only),
buying at ${threshold:.2f}, settling at $1.00 ({gross_margin_pct}% gross margin per trade).

"""

    inputs = q5.get("_inputs", {})
    if inputs:
        report += f"""**Model inputs (from data):**
- Daily opportunities: {inputs.get('daily_opportunities', 'N/A')}
- Avg liquidity estimate: {inputs.get('avg_liquidity_estimate', 'N/A')} shares
- Median settlement: {inputs.get('settlement_hours', 'N/A')} hours

"""

    scenario_names = [k for k in q5 if not k.startswith("_")]
    if scenario_names:
        headers = ["Metric", "Conservative", "Base Case", "Optimistic"]
        metrics = [
            ("Description", "description"),
            ("Fill rate", "fills_per_day", lambda x: f"{x:.1f}/day"),
            ("Effective fills (capital-limited)", "effective_fills_per_day", lambda x: f"{x:.1f}/day"),
            ("Avg fill size", "avg_fill_size", lambda x: f"${x:.0f}"),
            ("Gross per trade", "gross_per_trade", lambda x: f"${x:.4f}"),
            ("Net per trade (after gas)", "net_per_trade", lambda x: f"${x:.4f}"),
            ("**Daily net profit**", "daily_net_profit", lambda x: f"**${x:.2f}**"),
            ("**Monthly net profit**", "monthly_net_profit", lambda x: f"**${x:.2f}**"),
            ("**Annual net profit**", "annual_net_profit", lambda x: f"**${x:.2f}**"),
            ("**Annual ROI**", "annual_roi_pct", lambda x: f"**{x:.1f}%**"),
        ]

        rows = []
        for metric in metrics:
            if len(metric) == 2:
                name, key = metric
                fmt = str
            else:
                name, key, fmt = metric
            row = [name]
            for sn in scenario_names:
                val = q5[sn].get(key, "N/A")
                row.append(fmt(val) if callable(fmt) and val != "N/A" else val)
            rows.append(row)

        report += tabulate(rows, headers=headers, tablefmt="pipe") + "\n\n"

    for chart_file, desc in charts:
        if "profitability" in chart_file:
            report += f"![{desc}](figures/{chart_file})\n\n"

    report += """---

## 6. Competition & Margin Trends

"""

    trend_dir = q6.get("trend_direction", "unknown")
    slope = q6.get("price_trend_slope", 0)
    report += f"**Overall trend:** {trend_dir} (price slope: {slope})\n\n"

    monthly = q6.get("monthly_stats", [])
    if monthly:
        headers = ["Month", "Mean Price", "% at ≤$0.99", "% at ≤$0.995", "Markets"]
        rows = [[m["month"], f"{m['mean_price']:.4f}", f"{m['pct_at_99']:.1f}%",
                 f"{m.get('pct_at_995', 'N/A')}%", m["num_markets"]] for m in monthly]
        report += tabulate(rows, headers=headers, tablefmt="pipe") + "\n\n"

    for chart_file, desc in charts:
        if "margin" in chart_file:
            report += f"![{desc}](figures/{chart_file})\n\n"

    report += """**Interpretation:** Rising mean post-resolution prices indicate more bots/traders
competing for settlement arbitrage, compressing the available margin. A mean price approaching
$1.00 means opportunities are being captured almost instantly.

---

## 7. Risk Assessment

"""

    report += f"""| Risk Factor | Assessment |
|-------------|------------|
| Total markets analyzed | {q7.get('total_resolved_markets', 'N/A')} |
| Low volume (<$100) markets | {q7.get('low_volume_markets_pct', 'N/A')}% |
| Estimated dispute rate | {q7.get('dispute_estimate', {}).get('estimated_dispute_rate_pct', 1.5)}% |
| Loss per dispute | Full position value |

"""

    price_risk = q7.get("price_based_risk", {})
    if price_risk:
        report += f"""**Price-based risk analysis:**
- Markets with late price data (within 1hr of close): {price_risk.get('markets_with_late_price_data', 'N/A')}
- "Surprise" wins (traded <$0.50 near close): {price_risk.get('surprise_wins_below_50', 'N/A')} ({price_risk.get('surprise_rate_pct', 'N/A')}%)

"""

    risks = q7.get("key_risks", [])
    if risks:
        report += "**Key risk factors:**\n"
        for risk in risks:
            report += f"1. {risk}\n"
        report += "\n"

    report += """---

## 8. Final Recommendation

"""

    if daily_profit_base > 5:
        report += f"""### PROCEED WITH CAUTION

The data suggests the strategy is viable but with thin margins. Recommended starting parameters:

- **Starting capital:** $1,000
- **Max position size:** $100 per market
- **Target categories:** Focus on the categories with highest opportunity rates and 0% fees
- **Settlement window:** Only enter after resolution is proposed (not before)
- **Gas optimization:** Use low-gas periods (check Polygon gas tracker)
- **Risk limit:** Stop-loss at 3 consecutive dispute losses

**Expected outcome:** ${daily_profit_base:.2f}/day base case, which is {"above" if daily_profit_base > 3 else "near"} the target of "a few dollars per day."
"""
    elif daily_profit_base > 0:
        report += f"""### MARGINAL — LOW CONFIDENCE

The data shows the strategy can theoretically generate ${daily_profit_base:.2f}/day, but:

1. **The {gross_margin_pct}% gross margin is thin.** Gas costs, execution slippage, and occasional disputes can reduce net returns significantly.
2. **Capital efficiency is limited.** Settlement times of {settlement_median:.1f} hours mean your $1,000 turns over slowly.
3. **Competition is real.** Bot activity means many opportunities will be captured before you can act.

If proceeding despite thin margins:
- **Starting capital:** $500-1,000 (don't risk more until validating)
- **Max position size:** $50 per market (minimize risk per trade)
- **Focus on:** High-volume, fee-free categories only
- **Paper trade first:** Track opportunities for 1-2 weeks without real capital
"""
    else:
        report += f"""### DO NOT PROCEED

The analysis shows the strategy is **not profitable** under realistic conditions:

- Base case daily profit: ${daily_profit_base:.2f} (negative or negligible)
- Gas costs and competition consume the {gross_margin_pct}% gross margin
- Capital lockup during settlement further reduces effective returns

**Alternative considerations:**
- Look for wider spreads (≤$0.97) where the margin is 3%+ to absorb costs
- Consider cross-platform arbitrage (Polymarket vs Kalshi) for wider spreads
- Wait for Polymarket to reduce gas costs or improve settlement speed
"""

    report += f"""
---

*Report generated {now} using Polymarket Gamma API and CLOB API data.*
*This analysis is for informational purposes only. Past market behavior does not guarantee future results.*
*Polymarket fees, gas costs, and market dynamics may change at any time.*
"""

    return report


def main():
    global BUY_THRESHOLD, RESULTS_PATH, REPORT_PATH
    if len(sys.argv) > 1:
        try:
            BUY_THRESHOLD = float(sys.argv[1])
        except ValueError:
            pass

    threshold_label = f"{BUY_THRESHOLD:.2f}".replace(".", "")
    RESULTS_PATH = os.path.join(DATA_DIR, f"analysis_results_{threshold_label}.json")
    REPORT_PATH = os.path.join(REPORT_DIR, f"viability_report_{threshold_label}.md")

    os.makedirs(FIGURES_DIR, exist_ok=True)

    print("=" * 60)
    print(f"Generating Viability Report (threshold: ${BUY_THRESHOLD:.2f})")
    print("=" * 60)

    results = load_results()
    print("  Loaded analysis results")

    print("\n  Generating charts...")
    charts = generate_charts(results)

    print("\n  Generating report...")
    report = generate_report(results, charts)

    with open(REPORT_PATH, "w") as f:
        f.write(report)

    print(f"\n  Report saved to: {REPORT_PATH}")
    print(f"  Charts saved to: {FIGURES_DIR}/")
    print(f"\n{'=' * 60}")
    print("Done!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
