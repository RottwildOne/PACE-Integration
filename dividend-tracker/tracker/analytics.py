from collections import defaultdict
from datetime import date, timedelta


def _month_key(d):
    return d[:7]


def add_months(year, month, n):
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1


def dividends(conn, symbol=None):
    q = "SELECT * FROM transactions WHERE kind='dividend'"
    args = []
    if symbol:
        q += " AND symbol=?"
        args.append(symbol)
    return conn.execute(q + " ORDER BY run_date DESC, id DESC", args).fetchall()


def derived_shares(conn):
    """Shares implied by the imported history only (buys + reinvestments - sells).
    This is a lower bound for positions opened before the export's start date."""
    out = defaultdict(float)
    for r in conn.execute("SELECT symbol, kind, quantity FROM transactions "
                          "WHERE kind IN ('buy','reinvestment','sell') AND quantity IS NOT NULL"):
        out[r["symbol"]] += r["quantity"]
    return dict(out)


def shares(conn):
    """symbol -> (shares, source) where source is 'manual' or 'history'."""
    out = {s: (q, "history") for s, q in derived_shares(conn).items()}
    for r in conn.execute("SELECT symbol, shares FROM holdings_override"):
        out[r["symbol"]] = (r["shares"], "manual")
    return out


def payer_symbols(conn):
    return [r["symbol"] for r in conn.execute(
        "SELECT DISTINCT symbol FROM transactions WHERE kind='dividend' ORDER BY symbol")]


def income_by_month(conn):
    out = defaultdict(float)
    for r in dividends(conn):
        out[_month_key(r["run_date"])] += r["amount"] or 0
    return dict(sorted(out.items()))


def income_by_year(conn):
    out = defaultdict(float)
    for r in dividends(conn):
        out[r["run_date"][:4]] += r["amount"] or 0
    return dict(sorted(out.items()))


def income_by_symbol(conn, since=None):
    out = defaultdict(float)
    for r in dividends(conn):
        if since and r["run_date"] < since:
            continue
        out[r["symbol"]] += r["amount"] or 0
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def ttm_by_symbol(conn, today):
    return income_by_symbol(conn, since=(today - timedelta(days=365)).isoformat())


def history_span_days(conn):
    row = conn.execute("SELECT MIN(run_date) lo, MAX(run_date) hi FROM transactions").fetchone()
    if not row or not row["lo"]:
        return 0
    return (date.fromisoformat(row["hi"]) - date.fromisoformat(row["lo"])).days


def upcoming_announced(conn, today):
    """Declared dividends not yet paid, with expected cash for current share count."""
    sh = shares(conn)
    out = []
    for r in conn.execute("SELECT * FROM announced WHERE COALESCE(pay_date, ex_date) >= ? "
                          "ORDER BY COALESCE(pay_date, ex_date), symbol", (today.isoformat(),)):
        n = sh.get(r["symbol"], (0, ""))[0]
        if n <= 0:
            continue
        out.append({**dict(r), "shares": n, "expected": r["amount"] * n})
    return out


def projection(conn, today, months=12):
    """Month-by-month expected income for the next `months` months.

    Per symbol and month, an *announced* payment wins; otherwise the amount the
    symbol paid in the same calendar month a year earlier is used as an
    *estimate*. Returns a list of dicts: month, announced, estimated, detail.
    """
    ann = upcoming_announced(conn, today)
    divs = defaultdict(list)  # (symbol) -> [(date, amount)]
    for r in dividends(conn):
        divs[r["symbol"]].append((r["run_date"], r["amount"] or 0))
    cutoff = (today - timedelta(days=400)).isoformat()
    symbols = {s for s, rows in divs.items() if any(d >= cutoff for d, _ in rows)}
    ann_by = defaultdict(float)
    for a in ann:
        ann_by[(a["symbol"], (a["pay_date"] or a["ex_date"])[:7])] += a["expected"]
        symbols.add(a["symbol"])

    sh = shares(conn)
    result = []
    for i in range(months):
        y, m = add_months(today.year, today.month, i)
        key = f"{y:04d}-{m:02d}"
        last_year = f"{y - 1:04d}-{m:02d}"
        announced = estimated = 0.0
        detail = []
        for s in sorted(symbols):
            if sh.get(s, (0, ""))[0] <= 0:
                continue
            a = ann_by.get((s, key), 0.0)
            if a:
                announced += a
                detail.append((s, a, "announced"))
                continue
            if i == 0 and any(d.startswith(key) for d, _ in divs[s]):
                continue  # already paid this month
            est = sum(amt for d, amt in divs[s] if d.startswith(last_year)
                      and (i > 0 or int(d[8:10]) > today.day))
            if est:
                estimated += est
                detail.append((s, est, "estimate"))
        result.append({"month": key, "announced": announced, "estimated": estimated,
                       "detail": detail})
    return result


def holdings_table(conn, today):
    sh = shares(conn)
    ttm = ttm_by_symbol(conn, today)
    prices = {r["symbol"]: r for r in conn.execute("SELECT * FROM prices")}
    symbols = sorted(set(sh) | set(ttm))
    rows = []
    for s in symbols:
        n, src = sh.get(s, (0.0, "history"))
        price = prices[s]["price"] if s in prices else None
        value = n * price if price and n > 0 else None
        income = ttm.get(s, 0.0)
        rows.append({"symbol": s, "shares": n, "source": src, "price": price,
                     "value": value, "ttm": income,
                     "yield_pct": (income / value * 100) if value else None})
    return rows
