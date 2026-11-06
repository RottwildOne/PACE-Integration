from datetime import date
from pathlib import Path

import pytest

from app import create_app
from tracker import analytics, db, importer, market

FIXTURE = (Path(__file__).parent / "fixture_history.csv").read_text()
TODAY = date(2026, 11, 6)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    importer.import_history(c, FIXTURE)
    return c


def test_parse_skips_footer_and_unknown_actions():
    rows, ignored = importer.parse_history(FIXTURE)
    assert len(rows) == 6
    assert ignored == {"INTEREST EARNED SOMETHING": 1}
    assert {r["kind"] for r in rows} == {"dividend", "reinvestment", "buy"}


def test_reimport_is_idempotent(conn):
    res = importer.import_history(conn, FIXTURE)
    assert res["added"] == 0 and res["duplicates"] == 6


def test_identical_lines_in_one_file_are_both_kept():
    c = db.connect(":memory:")
    line = 'DIVIDEND RECEIVED X (X) (Cash),X,X,Cash,"",0,"","","",1.00,5,""'
    text = f'Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date\n01/02/2026,{line}\n01/02/2026,{line}\n'
    assert importer.import_history(c, text)["added"] == 2
    assert importer.import_history(c, text)["added"] == 0


def test_rejects_non_fidelity_file():
    with pytest.raises(importer.ImportError_):
        importer.parse_history("a,b\n1,2\n")


def test_shares_and_manual_override(conn):
    assert analytics.shares(conn)["O"] == (10.04, "history")
    conn.execute("INSERT INTO holdings_override VALUES('O', 100)")
    assert analytics.shares(conn)["O"] == (100, "manual")


def test_income_summaries(conn):
    assert analytics.income_by_year(conn) == {"2025": 2.10, "2026": pytest.approx(5.28)}
    assert analytics.ttm_by_symbol(conn, TODAY)["O"] == pytest.approx(4.46)  # 2025-11-15 is inside 365 days


def fake_get_factory(rows_by_day, yahoo_divs=None):
    class Resp:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    def fake_get(url, params=None, **kw):
        if "nasdaq" in url:
            rows = rows_by_day.get(params["date"], [])
            return Resp({"data": {"calendar": {"rows": rows}}})
        if params and params.get("events") == "div":
            import calendar
            evs = {str(i): {"date": calendar.timegm(date.fromisoformat(d).timetuple()) + 50000, "amount": a}
                   for i, (d, a) in enumerate((yahoo_divs or {}).get(url.rsplit("/", 1)[1], []))}
            return Resp({"chart": {"result": [{"events": {"dividends": evs}}]}})
        return Resp({"chart": {"result": [{"meta": {"regularMarketPrice": 50.0}}]}})
    return fake_get


CAL = {"2026-11-10": [
    {"symbol": "O", "dividend_Ex_Date": "11/10/2026", "payment_Date": "11/16/2026",
     "record_Date": "11/10/2026", "dividend_Rate": 0.27, "announcement_Date": "10/20/2026"},
    {"symbol": "ZZZZ", "dividend_Ex_Date": "11/10/2026", "payment_Date": "11/16/2026",
     "dividend_Rate": 1.0},
]}


def test_refresh_announced_only_stores_held_symbols(conn):
    res = market.refresh_announced(conn, ["O", "JEPQ"], today=TODAY, get=fake_get_factory(CAL))
    assert res["stored"] == 1 and res["days_failed"] == 0
    rows = conn.execute("SELECT * FROM announced").fetchall()
    assert [(r["symbol"], r["pay_date"], r["amount"]) for r in rows] == [("O", "2026-11-16", 0.27)]


def test_failed_scan_keeps_old_announced(conn):
    market.refresh_announced(conn, ["O"], today=TODAY, get=fake_get_factory(CAL))
    def boom(*a, **k): raise RuntimeError("down")
    res = market.refresh_announced(conn, ["O"], today=TODAY, get=boom)
    assert res["days_failed"] == res["days_total"]
    assert conn.execute("SELECT COUNT(*) FROM announced").fetchone()[0] == 1


def test_projection_prefers_announced_then_last_year(conn):
    market.refresh_announced(conn, ["O"], today=TODAY, get=fake_get_factory(CAL))
    proj = analytics.projection(conn, TODAY)
    nov = proj[0]
    assert nov["month"] == "2026-11"
    assert nov["announced"] == pytest.approx(0.27 * 10.04)
    # O paid in Nov-2025 but Nov-2026 is covered by the announcement -> no double count
    assert nov["estimated"] == 0
    # JEPQ: paid this month already, no estimate; Oct-2026 O payment repeats in Oct-2027 window
    oct_ = next(p for p in proj if p["month"] == "2027-10")
    assert oct_["estimated"] == pytest.approx(2.36)


def test_web_flow(tmp_path):
    app = create_app(db_path=tmp_path / "t.db", today_fn=lambda: TODAY,
                     http_get=fake_get_factory(CAL))
    c = app.test_client()
    assert b"Welcome" in c.get("/").data
    r = c.post("/import", data={"file": (__import__("io").BytesIO(FIXTURE.encode()), "h.csv")},
               follow_redirects=True)
    assert b"Imported 6 new rows" in r.data
    r = c.post("/refresh", follow_redirects=True)
    assert b"Found 1 announced" in r.data and b"updated 2 prices" in r.data
    for path in ("/", "/dividends", "/dividends?symbol=O", "/income", "/upcoming", "/holdings"):
        assert c.get(path).status_code == 200, path
    assert b"$2.71" in c.get("/upcoming").data  # 0.27 * 10.04 = 2.7108
    c.post("/holdings/O", data={"shares": "100"})
    assert b"$27.00" in c.get("/upcoming").data
    assert b"Shares must be a number" in c.post("/holdings/O", data={"shares": "abc"},
                                                follow_redirects=True).data


def test_month_format_is_mm_yyyy(tmp_path):
    import io
    app = create_app(db_path=tmp_path / "m.db", today_fn=lambda: TODAY, http_get=fake_get_factory({}))
    c = app.test_client()
    c.post("/import", data={"file": (io.BytesIO(FIXTURE.encode()), "h.csv")})
    inc = c.get("/income").get_data(as_text=True)
    assert "11-2026" in inc and "2026-11" not in inc
    up = c.get("/upcoming").get_data(as_text=True)
    assert "11-2026" in up and "2026-11" not in up
    dash = c.get("/").get_data(as_text=True)
    assert "11-2026" in dash and "2026-11" not in dash


def test_yahoo_fallback_adds_unpaid_ex_dividend_with_estimated_pay_date(conn):
    # O: paid 10/15 for an earlier ex-date (10/01, lag 14d); new ex-date 10/30 not yet received
    yahoo = {"O": [("2026-10-01", 0.27), ("2026-10-30", 0.272)], "JEPQ": []}
    res = market.refresh_announced(conn, ["O", "JEPQ"], today=TODAY,
                                   get=fake_get_factory({}, yahoo))
    assert res["from_yahoo"] == 1
    row = conn.execute("SELECT * FROM announced").fetchone()
    assert (row["symbol"], row["ex_date"], row["amount"], row["pay_estimated"]) == ("O", "2026-10-30", 0.272, 1)
    assert row["pay_date"] == "2026-11-13"  # ex + 14 day lag
    up = analytics.upcoming_announced(conn, TODAY)
    assert up[0]["expected"] == pytest.approx(0.272 * 10.04)


def test_yahoo_skips_dividends_already_received_or_covered_by_nasdaq(conn):
    yahoo = {"O": [("2026-10-01", 0.27), ("2026-11-02", 0.27)]}  # both <= already received 10/15? 11/02 > 10/15
    cal = {"2026-11-02": [{"symbol": "O", "dividend_Ex_Date": "11/02/2026", "payment_Date": "11/16/2026",
                           "dividend_Rate": 0.27}]}
    market.refresh_announced(conn, ["O"], today=TODAY, get=fake_get_factory(cal, yahoo))
    rows = conn.execute("SELECT source FROM announced").fetchall()
    assert [r["source"] for r in rows] == ["nasdaq"]  # yahoo duplicate suppressed
