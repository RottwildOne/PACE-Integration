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


def fake_get_factory(rows_by_day):
    class Resp:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    def fake_get(url, params=None, **kw):
        if "nasdaq" in url:
            rows = rows_by_day.get(params["date"], [])
            return Resp({"data": {"calendar": {"rows": rows}}})
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
