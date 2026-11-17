"""Databaslager för QuotePilot.

Postgres (t.ex. Neon via Vercel) om DATABASE_URL är satt, annars en lokal SQLite-fil.
SQL skrivs med ?-platshållare och översätts till %s för Postgres.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Stockholm")


def _database_url():
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")


def _sqlite_path():
    return os.environ.get("QUOTEPILOT_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "quotepilot.db"))


def is_postgres() -> bool:
    return bool(_database_url())


SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    id {pk},
    customer_name TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    property TEXT NOT NULL,
    job_description TEXT NOT NULL DEFAULT '',
    amount_sek INTEGER NOT NULL DEFAULT 0,
    sent_date TEXT NOT NULL,               -- YYYY-MM-DD
    status TEXT NOT NULL DEFAULT 'waiting', -- waiting | replied | won | lost
    followups_sent INTEGER NOT NULL DEFAULT 0,
    last_followup_date TEXT,
    replied_after_followup INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id {pk},
    quote_id INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL,                   -- ai | mall
    status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | discarded
    created_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "company_name": "Takfirman",
    "signer_name": "",
    "phone": "",
    "followup_days": "5",
    "max_followups": "3",
}


class _Conn:
    """Tunn wrapper så att resten av koden kan skriva samma SQL för båda databaserna."""

    def __init__(self, raw, pg: bool):
        self.raw, self.pg = raw, pg

    def execute(self, sql, params=()):
        if self.pg:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)


@contextmanager
def connect():
    """Öppnar en anslutning, committar vid lyckat block, rullar tillbaka vid fel, stänger alltid."""
    url = _database_url()
    if url:
        import psycopg
        from psycopg.rows import dict_row
        raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
    else:
        raw = sqlite3.connect(_sqlite_path())
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
    try:
        yield _Conn(raw, bool(url))
        raw.commit()
    except BaseException:
        raw.rollback()
        raw.close()
        raise
    raw.close()


def init_db():
    pk = "SERIAL PRIMARY KEY" if is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with connect() as conn:
        for stmt in SCHEMA.format(pk=pk).split(";"):
            if stmt.strip():
                conn.execute(stmt)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING", (k, v))


def now_iso():
    return datetime.now(TZ).replace(tzinfo=None).isoformat(timespec="seconds")


def today():
    """Dagens datum i svensk tid (Vercels servrar går på UTC)."""
    return datetime.now(TZ).date()


# ---------- inställningar ----------

def get_settings():
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in rows})
    return s


def save_settings(values: dict):
    with connect() as conn:
        for k, v in values.items():
            if k in DEFAULT_SETTINGS:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, str(v).strip()),
                )


# ---------- offerter ----------

def add_quote(q: dict) -> bool:
    """Lägger till en offert. Returnerar False om den redan finns (dubblett)."""
    with connect() as conn:
        dup = conn.execute(
            "SELECT 1 FROM quotes WHERE lower(customer_email) = lower(?) AND sent_date = ? AND amount_sek = ?",
            (q["customer_email"], q["sent_date"], q["amount_sek"]),
        ).fetchone()
        if dup:
            return False
        conn.execute(
            "INSERT INTO quotes(customer_name, customer_email, property, job_description, amount_sek, "
            "sent_date, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                q["customer_name"], q["customer_email"], q["property"], q.get("job_description", ""),
                q["amount_sek"], q["sent_date"], q.get("status", "waiting"), now_iso(),
            ),
        )
    return True


def get_quote(quote_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()


def all_quotes():
    with connect() as conn:
        return conn.execute("SELECT * FROM quotes ORDER BY sent_date DESC, id DESC").fetchall()


def set_status(quote_id: int, status: str):
    """Markerar svar/vunnen/förlorad. Sparar om svaret kom efter en uppföljning (pilotmätning)."""
    assert status in ("waiting", "replied", "won", "lost")
    with connect() as conn:
        q = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
        if q is None:
            return
        after = q["replied_after_followup"]
        if q["status"] == "waiting" and status != "waiting":
            after = 1 if q["followups_sent"] > 0 else 0
        if status == "waiting":
            after = 0
        conn.execute(
            "UPDATE quotes SET status = ?, replied_after_followup = ? WHERE id = ?",
            (status, after, quote_id),
        )
        if status != "waiting":
            # Kunden har svarat -> stoppa alla väntande påminnelser.
            conn.execute(
                "UPDATE drafts SET status = 'discarded' WHERE quote_id = ? AND status = 'pending'",
                (quote_id,),
            )


def delete_quote(quote_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))


# ---------- uppföljningslogik ----------

def _parse(d):
    return date.fromisoformat(d) if d else None


def days_since_contact(q) -> int:
    last = _parse(q["last_followup_date"]) or _parse(q["sent_date"])
    return (today() - last).days


def followup_state(q, settings=None) -> str:
    """Returnerar: follow_up | waiting | maxed | replied | won | lost"""
    if q["status"] != "waiting":
        return q["status"]
    s = settings or get_settings()
    if q["followups_sent"] >= int(s["max_followups"]):
        return "maxed"
    if days_since_contact(q) >= int(s["followup_days"]):
        return "follow_up"
    return "waiting"


def quotes_needing_followup(settings=None):
    s = settings or get_settings()
    return [q for q in all_quotes() if followup_state(q, s) == "follow_up"]


# ---------- utkast ----------

def pending_draft(quote_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM drafts WHERE quote_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (quote_id,),
        ).fetchone()


def quote_ids_with_pending_draft() -> set:
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT quote_id FROM drafts WHERE status = 'pending'").fetchall()
    return {r["quote_id"] for r in rows}


def get_draft(draft_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()


def save_draft(quote_id: int, subject: str, body: str, source: str) -> int:
    with connect() as conn:
        conn.execute(
            "UPDATE drafts SET status = 'discarded' WHERE quote_id = ? AND status = 'pending'",
            (quote_id,),
        )
        row = conn.execute(
            "INSERT INTO drafts(quote_id, subject, body, source, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (quote_id, subject, body, source, now_iso()),
        ).fetchone()
        return row["id"]


def update_draft_text(draft_id: int, subject: str, body: str):
    with connect() as conn:
        conn.execute("UPDATE drafts SET subject = ?, body = ? WHERE id = ?", (subject, body, draft_id))


def approve_draft(draft_id: int):
    """Ägaren har godkänt (och skickat) utkastet -> räkna upp uppföljningar."""
    with connect() as conn:
        d = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if d is None or d["status"] != "pending":
            return
        conn.execute("UPDATE drafts SET status = 'approved', approved_at = ? WHERE id = ?", (now_iso(), draft_id))
        conn.execute(
            "UPDATE quotes SET followups_sent = followups_sent + 1, last_followup_date = ? WHERE id = ?",
            (today().isoformat(), d["quote_id"]),
        )


def discard_draft(draft_id: int):
    with connect() as conn:
        conn.execute("UPDATE drafts SET status = 'discarded' WHERE id = ?", (draft_id,))


def history(quote_id: int):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM drafts WHERE quote_id = ? AND status = 'approved' ORDER BY approved_at",
            (quote_id,),
        ).fetchall()


# ---------- pilotmätning ----------

def pilot_stats():
    with connect() as conn:
        r = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM drafts WHERE status = 'approved') AS followups_sent,
              (SELECT COUNT(*) FROM quotes WHERE followups_sent > 0) AS quotes_followed_up,
              (SELECT COUNT(*) FROM quotes WHERE replied_after_followup = 1) AS replies_after,
              (SELECT COUNT(*) FROM quotes WHERE replied_after_followup = 1 AND status = 'won') AS won_after,
              (SELECT COALESCE(SUM(amount_sek), 0) FROM quotes
                 WHERE replied_after_followup = 1 AND status = 'won') AS won_value_after
            """
        ).fetchone()
    return dict(r)
