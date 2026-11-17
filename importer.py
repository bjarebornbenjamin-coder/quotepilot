"""CSV-import av offertlistor. Tål semikolon (svensk Excel), kommatecken och olika rubriker."""
import csv
import io
import re
from datetime import date, datetime

# Accepterade kolumnnamn -> internt fältnamn
ALIASES = {
    "customer_name": ["kund", "kundnamn", "namn", "customer", "customer_name", "name"],
    "customer_email": ["epost", "e-post", "mejl", "mail", "email", "e-mail", "customer_email"],
    "property": ["objekt", "adress", "fastighet", "property", "address"],
    "job_description": ["beskrivning", "arbete", "jobb", "description", "job", "job_description"],
    "amount_sek": ["belopp", "summa", "pris", "offertbelopp", "amount", "amount_sek", "price"],
    "sent_date": ["skickad", "datum", "skickat", "offertdatum", "sent", "sent_date", "date"],
    "status": ["status"],
}

STATUS_MAP = {
    "": "waiting", "väntar": "waiting", "skickad": "waiting", "waiting": "waiting", "open": "waiting",
    "svarat": "replied", "replied": "replied",
    "vunnen": "won", "vunnit": "won", "accepterad": "won", "won": "won",
    "förlorad": "lost", "nej": "lost", "lost": "lost",
}

REQUIRED = ["customer_name", "customer_email", "property", "sent_date"]


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_amount(value: str) -> int:
    v = (value or "").lower().replace("kr", "").replace("sek", "")
    v = re.sub(r"[\s ]", "", v)
    if not v:
        return 0
    # "84.000" eller "84,000" som tusentalsavgränsare; "84000,50" som decimal
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", v):
        v = re.sub(r"[.,]", "", v)
    v = v.replace(",", ".")
    return int(round(float(v)))


def parse_date(value: str) -> str:
    v = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"okänt datumformat: '{v}'")


def parse_csv(raw: bytes):
    """Returnerar (rader, fel). Rader är dictar redo för db.add_quote."""
    text = _decode(raw)
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if text.count(";") > text.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return [], ["Filen verkar vara tom."]

    colmap = {}
    for col in reader.fieldnames:
        key = (col or "").strip().lower()
        for field, names in ALIASES.items():
            if key in names and field not in colmap:
                colmap[field] = col
    missing = [f for f in REQUIRED if f not in colmap]
    if missing:
        nice = {"customer_name": "kund", "customer_email": "epost", "property": "objekt", "sent_date": "skickad"}
        return [], ["Saknar kolumn(er): " + ", ".join(nice[m] for m in missing)
                    + ". Hittade: " + ", ".join(reader.fieldnames)]

    rows, errors = [], []
    for i, rec in enumerate(reader, start=2):
        get = lambda f: (rec.get(colmap[f]) or "").strip() if f in colmap else ""
        if not any((v or "").strip() for v in rec.values() if isinstance(v, str)):
            continue
        try:
            email = get("customer_email")
            if "@" not in email:
                raise ValueError(f"ogiltig e-post '{email}'")
            sent = parse_date(get("sent_date"))
            if date.fromisoformat(sent) > date.today():
                raise ValueError("datumet ligger i framtiden")
            status_raw = get("status").lower()
            if status_raw not in STATUS_MAP:
                raise ValueError(f"okänd status '{status_raw}'")
            rows.append({
                "customer_name": get("customer_name"),
                "customer_email": email,
                "property": get("property"),
                "job_description": get("job_description"),
                "amount_sek": parse_amount(get("amount_sek")),
                "sent_date": sent,
                "status": STATUS_MAP[status_raw],
            })
        except ValueError as e:
            errors.append(f"Rad {i}: {e}")
    return rows, errors
