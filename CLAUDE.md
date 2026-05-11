# 99Cents Bot — operational notes

## Loss report

All losing positions are recorded as JSONL in `data/losses.log`. One line per
loss event with full context — meant to be human-readable and greppable.

Two event types:
- `cut_loss` — bot sold the position via cut-loss before settlement.
  `loss` is the realized loss (cost minus sell proceeds).
- `resolved_loss` — market resolved against us. `loss` equals `cost`
  (shares are worth $0).

Fields per line: `timestamp, type, loss, cost, entry_price, size, category,
question, market_id, condition_id, token_id, order_id, outcome_index,
neg_risk, placed_at, filled_at, source, score, net_profit, gross_profit`.

The log is written:
- On startup via `audit_losses()` — backfills any positions in
  `data/positions.json` with status `cut_loss` or `resolved_loss` that
  aren't already in the file. Idempotent — dedupes by `(order_id, type)`.
- On each new event from `mark_cut_loss()` and `mark_resolved_loss()`.

### Reports

Total realized losses to date:
```bash
jq -s 'map(.loss) | add' data/losses.log
```

Losses by category:
```bash
jq -s 'group_by(.category) | map({category: .[0].category, count: length, total: (map(.loss) | add)})' data/losses.log
```

Losses by type:
```bash
jq -s 'group_by(.type) | map({type: .[0].type, count: length, total: (map(.loss) | add)})' data/losses.log
```

Top 10 individual losses:
```bash
jq -s 'sort_by(-.loss) | .[:10] | .[] | "\(.loss) \(.category) \(.question[:80])"' -r data/losses.log
```

Weather losses by city (regex-extract from question):
```bash
jq -s 'map(select(.category=="Science/Weather")) | group_by(.question | capture("in (?<c>[A-Z][a-z]+)").c) | map({city: .[0].question, count: length, total: (map(.loss) | add)})' data/losses.log
```

Recent losses (last 7 days):
```bash
jq -s --arg cutoff "$(date -u -d '7 days ago' +%FT%T)" 'map(select(.timestamp > $cutoff))' data/losses.log
```
