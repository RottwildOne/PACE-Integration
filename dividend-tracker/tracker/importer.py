"""Parser for Fidelity "Accounts History" CSV exports.

Expected header (extra/reordered columns are fine, matched by name):
Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),
Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date

The export begins with blank lines and ends with a legal disclaimer; both are
skipped. Only rows whose first cell is a MM/DD/YYYY date are considered.
"""
import csv
import hashlib
import io
from collections import Counter
from datetime import datetime


class ImportError_(ValueError):
    pass


def _money(s):
    s = (s or "").strip().replace("$", "").replace(",", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _date(s):
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def classify(action):
    a = action.upper()
    if a.startswith("DIVIDEND RECEIVED"):
        return "dividend"
    if a.startswith("REINVESTMENT"):
        return "reinvestment"
    if a.startswith("YOU BOUGHT"):
        return "buy"
    if a.startswith("YOU SOLD"):
        return "sell"
    return None


def parse_history(text):
    """Return (rows, ignored) where rows are dicts ready for insertion and
    ignored is a Counter of action prefixes that were skipped."""
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    header = None
    rows, ignored, seen = [], Counter(), Counter()
    for cells in reader:
        if not cells:
            continue
        if header is None:
            if cells[0].strip() == "Run Date":
                header = [c.strip() for c in cells]
            continue
        row = dict(zip(header, cells))
        run_date = _date(row.get("Run Date", ""))
        if not run_date:
            continue  # disclaimer / footer
        action = (row.get("Action") or "").strip()
        symbol = (row.get("Symbol") or "").strip().upper()
        kind = classify(action)
        if kind is None or not symbol:
            ignored[" ".join(action.split()[:3]) or "(blank)"] += 1
            continue
        price = _money(row.get("Price ($)"))
        qty = _money(row.get("Quantity"))
        amount = _money(row.get("Amount ($)"))
        if kind == "sell" and qty is not None:
            qty = -abs(qty)
        key = "|".join(str(x) for x in (run_date, action, symbol, price, qty, amount,
                                        row.get("Cash Balance ($)", "").strip()))
        seen[key] += 1  # distinguishes genuine identical lines within one file
        digest = hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()
        rows.append({"row_hash": digest, "run_date": run_date, "kind": kind,
                     "symbol": symbol, "description": (row.get("Description") or "").strip(),
                     "price": price, "quantity": qty, "amount": amount})
    if header is None:
        raise ImportError_("This doesn't look like a Fidelity history export "
                           "(no 'Run Date' header row found).")
    return rows, ignored


def import_history(conn, text):
    rows, ignored = parse_history(text)
    added = 0
    for r in rows:
        cur = conn.execute(
            "INSERT OR IGNORE INTO transactions(row_hash,run_date,kind,symbol,description,"
            "price,quantity,amount) VALUES(:row_hash,:run_date,:kind,:symbol,:description,"
            ":price,:quantity,:amount)", r)
        added += cur.rowcount
    conn.commit()
    return {"parsed": len(rows), "added": added, "duplicates": len(rows) - added,
            "ignored": dict(ignored)}
