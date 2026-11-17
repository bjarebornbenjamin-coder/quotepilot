import importlib
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


PG_URL = os.environ.get("TEST_DATABASE_URL")  # t.ex. postgresql://postgres@127.0.0.1:55432/qp_test
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])
ENV_VARS = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DATABASE_URL", "POSTGRES_URL", "VERCEL",
            "QUOTEPILOT_PASSWORD", "QUOTEPILOT_SECRET", "CRON_SECRET"]


def load_app(monkeypatch, tmp_path, backend="sqlite", **env):
    for k in ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("QUOTEPILOT_DB", str(tmp_path / "test.db"))
    if backend == "postgres":
        monkeypatch.setenv("DATABASE_URL", PG_URL)
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS drafts, quotes, settings")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import db, app as app_module
    importlib.reload(db)
    importlib.reload(app_module)
    app_module.app.config["TESTING"] = True
    return app_module, db


@pytest.fixture(params=BACKENDS)
def client(request, tmp_path, monkeypatch):
    app_module, db = load_app(monkeypatch, tmp_path, request.param)
    return app_module.app.test_client(), db


def ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def test_parse_csv_semicolon_and_formats():
    import importer
    raw = ("kund;epost;objekt;beskrivning;belopp;skickad;status\n"
           f"Anna Lind;anna@ex.se;Villa Bromma;Tak;84 000 kr;{ago(6)};\n"
           "Per Ek;per@ex.se;Radhus;Rännor;23.500;01/11/2026;Svarat\n"
           "Fel;inte-epost;X;Y;1;2026-01-01;\n").encode("cp1252")
    rows, errors = importer.parse_csv(raw)
    assert [r["amount_sek"] for r in rows] == [84000, 23500]
    assert rows[1]["sent_date"] == "2026-11-01" and rows[1]["status"] == "replied"
    assert len(errors) == 1 and "Rad 4" in errors[0]


def test_parse_csv_missing_columns():
    import importer
    rows, errors = importer.parse_csv(b"name,amount\nA,1\n")
    assert rows == [] and "Saknar kolumn" in errors[0]


def test_followup_flow(client):
    c, db = client
    base = {"customer_email": "a@ex.se", "job_description": "Takbyte", "amount_sek": 1000}
    db.add_quote({**base, "customer_name": "Gammal", "property": "Villa A", "sent_date": ago(6)})
    db.add_quote({**base, "customer_name": "Ny", "property": "Villa B", "sent_date": ago(2), "amount_sek": 2})
    assert not db.add_quote({**base, "customer_name": "Gammal", "property": "Villa A", "sent_date": ago(6)})

    due = db.quotes_needing_followup()
    assert [q["property"] for q in due] == ["Villa A"]
    qid = due[0]["id"]

    c.post("/generate-all")
    d = db.pending_draft(qid)
    assert d and d["source"] == "mall" and "Villa A" in d["body"]

    c.post(f"/draft/{d['id']}/approve", data={"subject": "Ämne", "body": "Redigerad text"})
    q = db.get_quote(qid)
    assert q["followups_sent"] == 1 and q["last_followup_date"] == date.today().isoformat()
    assert db.followup_state(q) == "waiting"  # klockan börjar om
    assert db.history(qid)[0]["body"] == "Redigerad text"

    c.post(f"/quote/{qid}/status", data={"status": "won"})
    stats = db.pilot_stats()
    assert stats["followups_sent"] == 1 and stats["replies_after"] == 1 and stats["won_value_after"] == 1000


def test_reply_stops_reminders(client):
    c, db = client
    db.add_quote({"customer_name": "A", "customer_email": "a@ex.se", "property": "Villa", "amount_sek": 1,
                  "sent_date": ago(10)})
    qid = db.all_quotes()[0]["id"]
    c.post(f"/quote/{qid}/draft")
    assert db.pending_draft(qid)
    c.post(f"/quote/{qid}/status", data={"status": "replied"})
    assert db.pending_draft(qid) is None
    assert db.quotes_needing_followup() == []
    assert db.pilot_stats()["replies_after"] == 0  # svarade innan någon uppföljning skickades


def test_max_followups(client):
    c, db = client
    db.save_settings({"max_followups": "1"})
    db.add_quote({"customer_name": "A", "customer_email": "a@ex.se", "property": "V", "amount_sek": 1,
                  "sent_date": ago(30)})
    qid = db.all_quotes()[0]["id"]
    c.post(f"/quote/{qid}/draft")
    c.post(f"/draft/{db.pending_draft(qid)['id']}/approve", data={})
    with db.connect() as conn:
        conn.execute("UPDATE quotes SET last_followup_date = ?", (ago(30),))
    assert db.followup_state(db.get_quote(qid)) == "maxed"


def test_pages_render(client):
    c, db = client
    assert "Välkommen" in c.get("/").get_data(as_text=True)
    c.post("/demo")
    html = c.get("/").get_data(as_text=True)
    assert "Villa Bromma" in html and "Behöver följas upp" in html
    for path in ("/import", "/kalkyl", "/installningar", "/import/exempel.csv"):
        assert c.get(path).status_code == 200
    r = c.post("/import", data={"file": (__import__("io").BytesIO(c.get("/import/exempel.csv").data), "x.csv")},
               content_type="multipart/form-data")
    assert "importerade" in r.get_data(as_text=True)


def test_login_required_when_password_set(tmp_path, monkeypatch):
    app_module, db = load_app(monkeypatch, tmp_path, QUOTEPILOT_PASSWORD="hemligt-lösen", QUOTEPILOT_SECRET="x" * 32)
    c = app_module.app.test_client()
    r = c.get("/kalkyl")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert c.post("/demo").status_code == 302 and db.all_quotes() == []  # skyddat
    assert "Fel lösenord" in c.post("/login", data={"password": "fel"}).get_data(as_text=True)
    r = c.post("/login?next=/kalkyl", data={"password": "hemligt-lösen"})
    assert r.headers["Location"] == "/kalkyl"
    assert c.get("/").status_code == 200
    r = c.post("/login?next=//evil.example", data={"password": "hemligt-lösen"})
    assert r.headers["Location"] == "/"  # ingen öppen omdirigering
    c.post("/logout")
    assert c.get("/").status_code == 302


def test_vercel_without_config_refuses(tmp_path, monkeypatch):
    app_module, _ = load_app(monkeypatch, tmp_path, VERCEL="1")
    c = app_module.app.test_client()
    r = c.get("/")
    html = r.get_data(as_text=True)
    assert r.status_code == 503 and "QUOTEPILOT_PASSWORD" in html and "Neon" in html


@pytest.mark.parametrize("backend", BACKENDS)
def test_cron_endpoint(tmp_path, monkeypatch, backend):
    app_module, db = load_app(monkeypatch, tmp_path, backend, CRON_SECRET="cron-hemlighet",
                              QUOTEPILOT_PASSWORD="pw", QUOTEPILOT_SECRET="s" * 32)
    c = app_module.app.test_client()
    db.add_quote({"customer_name": "A B", "customer_email": "a@ex.se", "property": "Villa", "amount_sek": 1,
                  "sent_date": ago(8)})
    assert c.get("/cron/check-followups").status_code == 401
    assert c.get("/cron/check-followups", headers={"Authorization": "Bearer fel"}).status_code == 401
    r = c.get("/cron/check-followups", headers={"Authorization": "Bearer cron-hemlighet"})
    assert r.status_code == 200 and r.get_json() == {"due": 1, "created": 1, "remaining": 0}
    # Vercel kan köra samma cron två gånger – då ska inget dubbelutkast skapas.
    r = c.get("/cron/check-followups", headers={"Authorization": "Bearer cron-hemlighet"})
    assert r.get_json()["created"] == 0


def test_time_budget_stops_early(client):
    c, db = client
    import app as app_module
    for i in range(3):
        db.add_quote({"customer_name": f"K{i}", "customer_email": f"k{i}@ex.se", "property": "V", "amount_sek": i,
                      "sent_date": ago(9)})
    assert app_module.run_followup_check(budget_seconds=-1) == (3, 0, 3)
    assert app_module.run_followup_check() == (3, 3, 0)
