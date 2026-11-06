"""Announced-dividend and price lookups (public endpoints, best effort).

Announced dividends come from Nasdaq's public dividend calendar, which lists
every declared dividend (stocks and ETFs, all exchanges) by ex-dividend date.
Prices come from Yahoo Finance's public chart endpoint. Neither is an official
API; both can change or rate-limit, so failures are reported, never fatal.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from statistics import median

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (dividend-tracker)"}
CAL_URL = "https://api.nasdaq.com/api/calendar/dividends"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"


def _iso(s):
    try:
        return datetime.strptime(s, "%m/%d/%Y").date().isoformat()
    except (TypeError, ValueError):
        return None


def fetch_calendar_day(day, get=requests.get):
    r = get(CAL_URL, params={"date": day.isoformat()}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = (r.json().get("data") or {}).get("calendar") or {}
    return data.get("rows") or []


def refresh_announced(conn, symbols, today=None, back_days=45, ahead_days=75, get=requests.get):
    """Scan the dividend calendar and store declared dividends for `symbols`.

    back_days covers dividends whose ex-date has passed but are not yet paid.
    Returns {"stored": n, "days_failed": n}.
    """
    today = today or date.today()
    symbols = {s.upper() for s in symbols}
    days = [today - timedelta(days=back_days) + timedelta(days=i)
            for i in range(back_days + ahead_days + 1)
            if (today - timedelta(days=back_days) + timedelta(days=i)).weekday() < 5]

    def work(d):
        try:
            return d, fetch_calendar_day(d, get), None
        except Exception as e:  # network / parse problems are per-day
            return d, [], e

    stored = failed = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(work, days))
    if not any(err for _, _, err in results):
        # Complete scan: drop stale rows first so cancelled/changed ones vanish.
        conn.execute("DELETE FROM announced WHERE source='nasdaq' AND ex_date >= ?",
                     ((today - timedelta(days=back_days)).isoformat(),))
    for d, rows, err in results:
        if err:
            failed += 1
            continue
        for r in rows:
            sym = (r.get("symbol") or "").upper()
            if sym not in symbols:
                continue
            ex = _iso(r.get("dividend_Ex_Date"))
            amt = r.get("dividend_Rate")
            if not ex or not isinstance(amt, (int, float)) or amt <= 0:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO announced(symbol,ex_date,pay_date,record_date,"
                "announce_date,amount,source) VALUES(?,?,?,?,?,?,'nasdaq')",
                (sym, ex, _iso(r.get("payment_Date")), _iso(r.get("record_Date")),
                 _iso(r.get("announcement_Date")), float(amt)))
            stored += 1
    conn.commit()
    ys = refresh_yahoo_pending(conn, symbols, today=today, back_days=back_days, get=get)
    return {"stored": stored + ys["stored"], "days_failed": failed, "days_total": len(days),
            "from_yahoo": ys["stored"], "yahoo_failed": ys["failed"]}


def fetch_yahoo_dividends(symbol, get=requests.get, range_="1y"):
    """[(ex_date_iso, per_share_amount)] for past ex-dividend dates, oldest first."""
    r = get(YAHOO_URL.format(symbol), params={"range": range_, "interval": "1d", "events": "div"},
            headers=HEADERS, timeout=20)
    r.raise_for_status()
    result = r.json()["chart"]["result"][0]
    events = (result.get("events") or {}).get("dividends") or {}
    return sorted((datetime.fromtimestamp(v["date"], UTC).date().isoformat(), float(v["amount"]))
                  for v in events.values())


def refresh_yahoo_pending(conn, symbols, today, back_days=45, get=requests.get):
    """Second source: dividends whose ex-date has passed but that haven't shown up
    as received in the imported history yet. Covers symbols the Nasdaq calendar omits.

    The pay date isn't in Yahoo's data, so it's estimated as ex-date + the symbol's
    typical ex-to-pay lag (median over past payments, default 14 days) and flagged.
    """
    conn.execute("DELETE FROM announced WHERE source='yahoo'")
    stored, failed = 0, []
    for sym in sorted(symbols):
        try:
            evs = fetch_yahoo_dividends(sym, get)
        except Exception:
            failed.append(sym)
            continue
        received = [r["run_date"] for r in conn.execute(
            "SELECT run_date FROM transactions WHERE kind='dividend' AND symbol=?", (sym,))]
        lags = []
        for rd in received:
            rdd = date.fromisoformat(rd)
            prior = [date.fromisoformat(e) for e, _ in evs if date.fromisoformat(e) <= rdd]
            if prior and (rdd - max(prior)).days <= 60:
                lags.append((rdd - max(prior)).days)
        lag = int(median(lags)) if lags else 14
        cutoff = today - timedelta(days=back_days)
        for ex, amt in evs:
            exd = date.fromisoformat(ex)
            if amt <= 0 or not (cutoff <= exd <= today):
                continue
            if any(r >= ex for r in received):
                continue  # already paid out
            if conn.execute("SELECT 1 FROM announced WHERE symbol=? AND source='nasdaq' AND "
                            "ABS(julianday(ex_date)-julianday(?)) <= 3", (sym, ex)).fetchone():
                continue
            pay = exd + timedelta(days=lag)
            if pay < today:
                continue  # estimated pay date already passed: history is likely just stale
            conn.execute(
                "INSERT OR REPLACE INTO announced(symbol,ex_date,pay_date,record_date,"
                "announce_date,amount,source,pay_estimated) VALUES(?,?,?,NULL,NULL,?,'yahoo',1)",
                (sym, ex, pay.isoformat(), amt))
            stored += 1
    conn.commit()
    return {"stored": stored, "failed": failed}


def fetch_price(symbol, get=requests.get):
    r = get(YAHOO_URL.format(symbol), params={"range": "1d", "interval": "1d"},
            headers=HEADERS, timeout=20)
    r.raise_for_status()
    meta = r.json()["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"])


def refresh_prices(conn, symbols, now=None, get=requests.get):
    now = (now or datetime.now()).isoformat(timespec="seconds")
    ok, bad = 0, []
    for s in sorted(symbols):
        try:
            p = fetch_price(s, get)
        except Exception:
            bad.append(s)
            continue
        conn.execute("INSERT OR REPLACE INTO prices(symbol,price,fetched_at) VALUES(?,?,?)",
                     (s, p, now))
        ok += 1
    conn.commit()
    return {"updated": ok, "failed": bad}
