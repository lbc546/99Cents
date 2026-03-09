# 99Cents Bot — Operations Guide

## Deployment (Ubuntu 22.04)

### First-time setup

```bash
# 1. Clone/copy code to server
scp -r . user@vps:/tmp/99cents

# 2. SSH in and run deploy
ssh user@vps
sudo cp -r /tmp/99cents /opt/99cents
cd /opt/99cents

# 3. Edit .env with your secrets
sudo cp .env.example .env
sudo nano .env   # Add POLYMARKET_PRIVATE_KEY and POLYGON_RPC_URL

# 4. Run deploy script (creates user, venv, service, etc.)
sudo ./deploy/deploy.sh
```

### Updating

```bash
ssh user@vps
cd /opt/99cents
sudo ./deploy/deploy.sh   # pulls code, installs deps, health check, restarts
```

### Service management

```bash
sudo systemctl status 99cents      # Check status
sudo systemctl restart 99cents     # Restart
sudo systemctl stop 99cents        # Stop
sudo systemctl start 99cents       # Start
journalctl -u 99cents -f           # Live logs (systemd journal)
tail -f /opt/99cents/bot.log       # Live logs (JSON file)
```

---

## Monitoring Checklist

### First 2 weeks — check daily

- [ ] **Dashboard**: Run with `dashboard_enabled: true` or review `bot.log`
- [ ] **Daily P&L**: Target is ~$4.74/day. Below $1.00/day for 5 consecutive days → pause and reassess
- [ ] **Opportunity rate**: Should see ~15-25 opportunities/day across Crypto + Esports
- [ ] **Filter pass rate**: Expect ~5-10% pass rate (most markets get filtered)
- [ ] **Settlement times**: Crypto avg ~3h, Esports avg ~2h. Flagged trades indicate delays
- [ ] **Circuit breakers**: Should remain closed (OK). If triggered, investigate immediately
- [ ] **WebSocket status**: Should stay connected. Frequent disconnects = network issue

### Esports settlement variance

Esports markets can have unpredictable settlement timing. Watch for:
- **CV threshold alerts** (esports_variance_cv_threshold: 1.5): high variance in settlement times
- Markets stuck in "pending" state for >24 hours
- If Esports consistently underperforms, consider narrowing to Crypto-only

### Red flags — investigate immediately

1. Circuit breaker triggered (dispute, loss limit, consecutive failures)
2. Blacklist growing rapidly (>10 entries/week)
3. Multiple flagged trades in anomaly monitor
4. WebSocket disconnected for >5 minutes
5. Daily net profit negative for 3+ consecutive days

---

## Key Configuration Knobs

| Parameter | Default | Effect |
|---|---|---|
| `price_threshold` | 0.98 | Higher = safer but fewer opps |
| `max_position_per_market` | 50 | Max $ per single market |
| `max_total_deployed` | 400 | Max $ across all positions |
| `daily_loss_limit` | 15.0 | Trips circuit breaker |
| `dry_run` | true | **Set to false for live trading** |

---

## Log Files

| File | Contents |
|---|---|
| `bot.log` | Main JSON log (rotated: 7 days, 100MB max) |
| `daily_summary_YYYY-MM-DD.log` | Daily P&L summary (generated at midnight UTC) |
| `report/dry_run_report.txt` | Dry-run simulation report (generated at shutdown) |
| `data/blacklist.json` | Blacklisted market IDs |

---

## Decision Framework: When to Pause

**Pause if ANY of these are true:**
- Daily profit consistently < $1.00 for 5 days
- Circuit breaker trips 3+ times in a week
- Dispute rate exceeds 3% of traded positions
- Total blacklisted markets exceeds 50
- Wallet balance drops below capital floor ($100)

**Resume after:**
- Investigating root cause
- Adjusting parameters (tighten threshold, reduce position size)
- Running another 48-hour dry run to validate changes

---

## Emergency Procedures

### Stop trading immediately
```bash
sudo systemctl stop 99cents
```

### Check what happened
```bash
# Last 100 log lines
journalctl -u 99cents -n 100 --no-pager

# Search for errors
grep '"level":"ERROR"' /opt/99cents/bot.log | tail -20

# Check circuit breaker events
grep 'CIRCUIT_BREAKER' /opt/99cents/bot.log | tail -10
```

### Recover from dispute
1. Bot auto-blacklists disputed market (permanent)
2. Circuit breaker trips — trading paused
3. Investigate the market on Polymarket UI
4. If isolated incident: `sudo systemctl restart 99cents` (breaker resets on restart)
5. If systematic: tighten `end_date_grace_minutes` or add to `blacklist_manual`
