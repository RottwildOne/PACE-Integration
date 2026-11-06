# Dividend Tracker

Flask + SQLite web app that tracks dividend income from a Fidelity account-history export,
and projects future income using dividends companies have already announced.

## Run

```bash
cd dividend-tracker
pip install -r requirements.txt
python app.py            # http://localhost:5000
pytest                   # tests
```

Data is stored in `data/tracker.db` (override with `DIVIDEND_DB`). Nothing leaves your machine except
the public market-data lookups below.

## Using it

1. **Import** a Fidelity history CSV (Activity & Orders → History → Download). Re-importing overlapping
   files is safe. Recognised rows: dividends received, reinvestments, buys, sells.
2. **Refresh market data** (header button). This fetches:
   - *Announced dividends* from Nasdaq's public dividend calendar (ex-date, pay date, per-share amount)
     for the symbols you've received dividends from; scans ~45 days back to 75 days ahead (takes under a minute).
   - *Prices* from Yahoo Finance's public chart endpoint, for yield.
3. Views: dashboard, dividend log, income (year/month/symbol), upcoming + 12-month projection, holdings & yield.

## How projections work

- **Announced**: declared per-share amount × your current shares, in the month of the pay date.
- **Estimated**: for months with no announcement, repeats what that symbol paid in the same month a year earlier.
  Needs ≥12 months of imported history; ignores dividend changes.
- Shares come from buys/reinvestments in the imported history (a lower bound). Override per symbol on the Holdings page.

## Limits

- No live Fidelity connection (Fidelity has no public retail API); CSV import only. An aggregator
  (Plaid/SnapTrade) could be added behind the importer later.
- Both market-data sources are unofficial public endpoints and may change or rate-limit; failures are reported, not fatal.
- Cost basis isn't in the history export, so there's no yield on cost.
