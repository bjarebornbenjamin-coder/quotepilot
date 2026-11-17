"""QuotePilot – AI-uppföljare för takfirmor (MVP)."""
import hmac
import os
import time
import urllib.parse
from datetime import timedelta

import click
from flask import (Flask, Response, flash, jsonify, redirect, render_template, request, session,
                   url_for)

import ai
import db
import importer

# Vercel serverar statiska filer från public/, så CSS ligger i public/static/.
app = Flask(__name__, static_folder="public/static", static_url_path="/static")

ON_VERCEL = os.environ.get("VERCEL") == "1"
PASSWORD = os.environ.get("QUOTEPILOT_PASSWORD", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")
# Hur länge en körning får skapa utkast innan den slutar (Vercel stänger funktionen efter 300 s).
TIME_BUDGET_SECONDS = 240

CONFIG_ERRORS = []
if ON_VERCEL:
    if not db.is_postgres():
        CONFIG_ERRORS.append("Ingen databas kopplad. Skapa en Neon-databas under Storage i Vercel-projektet.")
    if not PASSWORD:
        CONFIG_ERRORS.append("Miljövariabeln QUOTEPILOT_PASSWORD saknas.")
    if not os.environ.get("QUOTEPILOT_SECRET"):
        CONFIG_ERRORS.append("Miljövariabeln QUOTEPILOT_SECRET saknas.")

app.secret_key = os.environ.get("QUOTEPILOT_SECRET", "dev-endast-lokalt")
app.config.update(
    SESSION_COOKIE_SECURE=ON_VERCEL,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

STATE_LABELS = {
    "follow_up": ("Följ upp", "warn"),
    "waiting": ("Väntar", "muted"),
    "maxed": ("Max uppföljningar", "muted"),
    "replied": ("Svarat", "ok"),
    "won": ("Vunnen", "ok"),
    "lost": ("Förlorad", "bad"),
}

if not CONFIG_ERRORS:
    db.init_db()


# ---------------- inloggning ----------------

OPEN_ENDPOINTS = {"login", "static", "cron_check_followups"}


@app.before_request
def require_login():
    if request.endpoint in OPEN_ENDPOINTS:
        return None
    if CONFIG_ERRORS:
        return render_template("config_error.html", errors=CONFIG_ERRORS), 503
    if PASSWORD and not session.get("auth"):
        return redirect(url_for("login", next=request.path))
    return None


@app.context_processor
def inject_auth():
    return {"login_enabled": bool(PASSWORD)}


@app.route("/login", methods=["GET", "POST"])
def login():
    if CONFIG_ERRORS:
        return render_template("config_error.html", errors=CONFIG_ERRORS), 503
    if not PASSWORD:
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", "").encode(), PASSWORD.encode()):
            session.clear()
            session["auth"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        time.sleep(1)  # bromsa gissningsförsök
        error = "Fel lösenord."
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.template_filter("kr")
def kr(value):
    return f"{int(value or 0):,} kr".replace(",", " ")


def _create_draft(quote, settings):
    days = db.days_since_contact(quote)
    result = ai.generate_followup(quote, settings, days)
    return db.save_draft(quote["id"], result["subject"], result["body"], result["source"])


def run_followup_check(budget_seconds=TIME_BUDGET_SECONDS):
    """Skapar utkast för offerter som behöver följas upp och saknar utkast.

    Slutar när tidsbudgeten är slut; resten tas vid nästa körning.
    Returnerar (antal som behöver uppföljning, antal nya utkast, antal kvar).
    """
    start = time.monotonic()
    settings = db.get_settings()
    due = db.quotes_needing_followup(settings)
    has_draft = db.quote_ids_with_pending_draft()
    todo = [q for q in due if q["id"] not in has_draft]
    created = 0
    for q in todo:
        if time.monotonic() - start > budget_seconds:
            break
        _create_draft(q, settings)
        created += 1
    return len(due), created, len(todo) - created


# ---------------- vyer ----------------

@app.route("/")
def dashboard():
    settings = db.get_settings()
    quotes = db.all_quotes()
    has_draft = db.quote_ids_with_pending_draft()
    rows = []
    for q in quotes:
        state = db.followup_state(q, settings)
        rows.append({"q": q, "state": state, "label": STATE_LABELS[state],
                     "days": db.days_since_contact(q), "has_draft": q["id"] in has_draft})
    # Följ upp först, sedan väntar, sedan resten
    order = {"follow_up": 0, "waiting": 1, "maxed": 2, "replied": 3, "won": 4, "lost": 5}
    rows.sort(key=lambda r: (order[r["state"]], -r["days"]))

    selected_id = request.args.get("q", type=int)
    if selected_id is None:
        first = next((r for r in rows if r["state"] == "follow_up"), None)
        selected_id = first["q"]["id"] if first else None
    selected = db.get_quote(selected_id) if selected_id else None
    draft = db.pending_draft(selected_id) if selected else None
    mailto = None
    if draft:
        mailto = "mailto:{}?subject={}&body={}".format(
            urllib.parse.quote(selected["customer_email"]),
            urllib.parse.quote(draft["subject"]),
            urllib.parse.quote(draft["body"]),
        )

    active = [r for r in rows if r["q"]["status"] == "waiting"]
    stats = {
        "active": len(active),
        "follow_up": sum(1 for r in rows if r["state"] == "follow_up"),
        "pipeline": sum(r["q"]["amount_sek"] for r in active),
    }
    return render_template(
        "dashboard.html", rows=rows, stats=stats, pilot=db.pilot_stats(), settings=settings,
        selected=selected, selected_state=db.followup_state(selected, settings) if selected else None,
        selected_days=db.days_since_contact(selected) if selected else None,
        draft=draft, mailto=mailto, history=db.history(selected_id) if selected else [],
        ai_enabled=ai.ai_enabled(), labels=STATE_LABELS,
    )


@app.post("/quote/<int:quote_id>/draft")
def make_draft(quote_id):
    quote = db.get_quote(quote_id)
    if quote is None:
        return redirect(url_for("dashboard"))
    _create_draft(quote, db.get_settings())
    return redirect(url_for("dashboard", q=quote_id))


@app.post("/generate-all")
def generate_all():
    _, n, left = run_followup_check()
    msg = f"{n} nya utkast skapade." if n else "Alla offerter som behöver uppföljning har redan ett utkast."
    if left:
        msg += f" {left} återstår – klicka igen för att fortsätta."
    flash(msg)
    return redirect(url_for("dashboard"))


@app.post("/draft/<int:draft_id>/approve")
def approve(draft_id):
    d = db.get_draft(draft_id)
    if d is None:
        return redirect(url_for("dashboard"))
    db.update_draft_text(draft_id, request.form.get("subject", d["subject"]), request.form.get("body", d["body"]))
    db.approve_draft(draft_id)
    flash("Uppföljningen är godkänd och markerad som skickad. Nästa påminnelse räknas från idag.")
    return redirect(url_for("dashboard", q=d["quote_id"]))


@app.post("/draft/<int:draft_id>/save")
def save(draft_id):
    d = db.get_draft(draft_id)
    if d is None:
        return redirect(url_for("dashboard"))
    db.update_draft_text(draft_id, request.form.get("subject", d["subject"]), request.form.get("body", d["body"]))
    flash("Utkastet sparat.")
    return redirect(url_for("dashboard", q=d["quote_id"]))


@app.post("/draft/<int:draft_id>/discard")
def discard(draft_id):
    d = db.get_draft(draft_id)
    if d:
        db.discard_draft(draft_id)
        return redirect(url_for("dashboard", q=d["quote_id"]))
    return redirect(url_for("dashboard"))


@app.post("/quote/<int:quote_id>/status")
def change_status(quote_id):
    status = request.form.get("status", "")
    if status in ("waiting", "replied", "won", "lost"):
        db.set_status(quote_id, status)
        if status != "waiting":
            flash("Status uppdaterad. Inga fler påminnelser skickas för den här offerten.")
    return redirect(url_for("dashboard", q=quote_id))


@app.post("/quote/<int:quote_id>/delete")
def delete(quote_id):
    db.delete_quote(quote_id)
    flash("Offerten är borttagen.")
    return redirect(url_for("dashboard"))


@app.route("/import", methods=["GET", "POST"])
def import_csv():
    result = None
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Välj en CSV-fil först.")
            return redirect(url_for("import_csv"))
        rows, errors = importer.parse_csv(f.read())
        added = sum(1 for r in rows if db.add_quote(r))
        result = {"added": added, "dupes": len(rows) - added, "errors": errors}
    return render_template("import.html", result=result)


@app.get("/import/exempel.csv")
def sample_csv():
    t = db.today()
    content = (
        "kund;epost;objekt;beskrivning;belopp;skickad;status\n"
        f"Anna Lind;anna.lind@example.com;Villa Bromma;Takrenovering med nya betongpannor;84 000;{(t - timedelta(days=6)).isoformat()};\n"
        f"Per Ek;per.ek@example.com;Radhus Sundbyberg;Byte av hängrännor och stuprör;23 500;{(t - timedelta(days=2)).isoformat()};\n"
    )
    return Response(content.encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=quotepilot-exempel.csv"})


@app.post("/demo")
def demo():
    n = load_demo_data()
    flash(f"{n} demoofferter inlästa." if n else "Demodatan finns redan.")
    return redirect(url_for("dashboard"))


@app.route("/installningar", methods=["GET", "POST"])
def settings_view():
    if request.method == "POST":
        try:
            days = int(request.form.get("followup_days", "5"))
            mx = int(request.form.get("max_followups", "3"))
            if not (1 <= days <= 60 and 1 <= mx <= 10):
                raise ValueError
        except ValueError:
            flash("Dagar måste vara 1–60 och max antal uppföljningar 1–10.")
            return redirect(url_for("settings_view"))
        db.save_settings(request.form.to_dict())
        flash("Inställningarna är sparade.")
        return redirect(url_for("settings_view"))
    return render_template("settings.html", s=db.get_settings(), ai_enabled=ai.ai_enabled(), model=ai.MODEL)


@app.get("/kalkyl")
def calculator():
    return render_template("calculator.html")


# ---------------- demodata & CLI ----------------

def load_demo_data() -> int:
    t = db.today()
    demo_rows = [
        ("Anna Lind", "anna.lind@example.com", "Villa Bromma", "Takrenovering med nya betongpannor", 84000, 5, "waiting"),
        ("Johan Berg", "johan.berg@example.com", "Villa Solna", "Omläggning av plåttak inkl. ny underlagspapp", 126000, 9, "replied"),
        ("Sara Nyström", "sara.nystrom@example.com", "Villa Täby", "Byte av takpannor på garage och carport", 69000, 2, "waiting"),
        ("Mikael Holm", "mikael.holm@example.com", "Radhus Huddinge", "Ny takavvattning och snörasskydd", 31500, 12, "waiting"),
        ("Lena Karlsson", "lena.karlsson@example.com", "Fritidshus Värmdö", "Tätning runt skorsten och byte av skorstensplåt", 18900, 7, "waiting"),
        ("Erik Sandberg", "erik.sandberg@example.com", "Villa Danderyd", "Komplett takbyte till tegelpannor", 245000, 21, "won"),
        ("Maria Ekholm", "maria.ekholm@example.com", "Villa Nacka", "Takfönster och ny takpapp", 57000, 16, "lost"),
        ("Oskar Lund", "oskar.lund@example.com", "Parhus Sollentuna", "Takrengöring och målning av plåttak", 42000, 1, "waiting"),
    ]
    n = 0
    for name, email, prop, job, amount, days_ago, status in demo_rows:
        n += db.add_quote({
            "customer_name": name, "customer_email": email, "property": prop, "job_description": job,
            "amount_sek": amount, "sent_date": (t - timedelta(days=days_ago)).isoformat(), "status": status,
        })
    return n


@app.get("/cron/check-followups")
def cron_check_followups():
    """Anropas av Vercel Cron (se vercel.json). Vercel skickar 'Authorization: Bearer <CRON_SECRET>'."""
    if not CRON_SECRET:
        return jsonify(error="CRON_SECRET är inte satt"), 503
    expected = f"Bearer {CRON_SECRET}".encode()
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected):
        return jsonify(error="unauthorized"), 401
    if CONFIG_ERRORS:
        return jsonify(error="konfiguration saknas", details=CONFIG_ERRORS), 503
    due, created, left = run_followup_check()
    return jsonify(due=due, created=created, remaining=left)


@app.cli.command("check-followups")
def check_followups():
    """Skapar utkast för alla offerter som behöver följas upp (kör dagligen via cron)."""
    due, created, left = run_followup_check(budget_seconds=10**9)
    click.echo(f"{due} offerter behöver uppföljning, {created} nya utkast skapade.")


@app.cli.command("load-demo")
def load_demo_cmd():
    """Läser in demoofferter."""
    click.echo(f"{load_demo_data()} demoofferter inlästa.")


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
