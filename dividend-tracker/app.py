import os
from datetime import date

from flask import Flask, flash, redirect, render_template, request, url_for

from tracker import analytics, db, importer, market


def create_app(db_path=None, today_fn=date.today, http_get=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
    db_path = db_path or os.environ.get(
        "DIVIDEND_DB", os.path.join(os.path.dirname(__file__), "data", "tracker.db"))
    conn = db.connect(db_path)
    get_kwargs = {"get": http_get} if http_get else {}

    @app.template_filter("usd")
    def usd(v):
        return "–" if v is None else f"${v:,.2f}"

    @app.template_filter("num")
    def num(v):
        return f"{v:,.3f}".rstrip("0").rstrip(".")

    @app.template_filter("longdate")
    def longdate(s):
        return date.fromisoformat(s).strftime("%b %d, %Y") if s else "–"

    @app.context_processor
    def inject():
        return {"last_refresh": db.get_meta(conn, "announced_refreshed")}

    def history_months(today, n=12):
        by_month = analytics.income_by_month(conn)
        out = []
        for i in range(-n + 1, 1):
            y, m = analytics.add_months(today.year, today.month, i)
            k = f"{y:04d}-{m:02d}"
            out.append({"month": k, "received": by_month.get(k, 0.0)})
        return out

    @app.route("/")
    def dashboard():
        today = today_fn()
        ttm = sum(analytics.ttm_by_symbol(conn, today).values())
        ytd = sum(v for k, v in analytics.income_by_month(conn).items()
                  if k.startswith(str(today.year)))
        proj = analytics.projection(conn, today)
        upcoming = analytics.upcoming_announced(conn, today)
        hist = history_months(today)
        peak = max([h["received"] for h in hist] +
                   [p["announced"] + p["estimated"] for p in proj] + [1])
        return render_template(
            "dashboard.html", ttm=ttm, ytd=ytd, hist=hist, proj=proj, peak=peak,
            announced_total=sum(u["expected"] for u in upcoming),
            forward=sum(p["announced"] + p["estimated"] for p in proj),
            next_up=upcoming[:5], has_data=bool(analytics.payer_symbols(conn)),
            span=analytics.history_span_days(conn))

    @app.route("/import", methods=["GET", "POST"])
    def import_csv():
        if request.method == "POST":
            f = request.files.get("file")
            if not f or not f.filename:
                flash("Choose a CSV file first.", "error")
                return redirect(url_for("import_csv"))
            try:
                res = importer.import_history(conn, f.read().decode("utf-8-sig", "replace"))
            except importer.ImportError_ as e:
                flash(str(e), "error")
                return redirect(url_for("import_csv"))
            msg = f"Imported {res['added']} new rows ({res['duplicates']} already present)."
            if res["ignored"]:
                msg += " Ignored: " + ", ".join(f"{k} ×{v}" for k, v in res["ignored"].items()) + "."
            flash(msg, "ok")
            return redirect(url_for("dashboard"))
        return render_template("import.html")

    @app.route("/dividends")
    def dividend_log():
        sym = (request.args.get("symbol") or "").upper() or None
        return render_template("dividends.html", rows=analytics.dividends(conn, sym),
                               symbols=analytics.payer_symbols(conn), selected=sym)

    @app.route("/income")
    def income():
        today = today_fn()
        monthly = analytics.income_by_month(conn)
        return render_template(
            "income.html", years=analytics.income_by_year(conn),
            months=list(reversed(list(monthly.items()))),
            by_symbol=analytics.income_by_symbol(conn),
            ttm=analytics.ttm_by_symbol(conn, today),
            span=analytics.history_span_days(conn))

    @app.route("/upcoming")
    def upcoming():
        today = today_fn()
        return render_template("upcoming.html", rows=analytics.upcoming_announced(conn, today),
                               proj=analytics.projection(conn, today))

    @app.route("/holdings")
    def holdings():
        return render_template("holdings.html", rows=analytics.holdings_table(conn, today_fn()))

    @app.post("/holdings/<symbol>")
    def set_shares(symbol):
        raw = (request.form.get("shares") or "").strip()
        if raw == "":
            conn.execute("DELETE FROM holdings_override WHERE symbol=?", (symbol,))
        else:
            try:
                conn.execute("INSERT OR REPLACE INTO holdings_override(symbol,shares) VALUES(?,?)",
                             (symbol, float(raw.replace(",", ""))))
            except ValueError:
                flash("Shares must be a number.", "error")
        conn.commit()
        return redirect(url_for("holdings"))

    @app.post("/refresh")
    def refresh():
        today = today_fn()
        symbols = analytics.payer_symbols(conn)
        if not symbols:
            flash("Import a history CSV first.", "error")
            return redirect(request.referrer or url_for("dashboard"))
        a = market.refresh_announced(conn, symbols, today=today, **get_kwargs)
        p = market.refresh_prices(conn, symbols, **get_kwargs)
        db.set_meta(conn, "announced_refreshed", today.isoformat())
        msg = f"Found {a['stored']} announced dividends; updated {p['updated']} prices."
        if a["days_failed"]:
            msg += f" {a['days_failed']}/{a['days_total']} calendar lookups failed — try again."
        if p["failed"]:
            msg += " No price for: " + ", ".join(p["failed"]) + "."
        flash(msg, "error" if a["days_failed"] else "ok")
        return redirect(request.referrer or url_for("dashboard"))

    return app


if __name__ == "__main__":
    create_app().run(debug=os.environ.get("FLASK_DEBUG") == "1",
                     port=int(os.environ.get("PORT", 5000)))
