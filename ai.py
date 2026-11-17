"""Skriver uppföljningsmejl. Använder Claude API om nyckel finns, annars en enkel mall."""
import json
import logging
import os

log = logging.getLogger(__name__)

MODEL = os.environ.get("QUOTEPILOT_MODEL", "claude-opus-5-5")

SYSTEM_PROMPT = """Du skriver uppföljningsmejl åt en svensk takfirma. Firman har skickat en offert \
som kunden inte har svarat på. Mejlet skickas i ägarens namn efter att ägaren har läst och godkänt det.

Så ska mejlet vara:
- Skrivet på naturlig, vardaglig svenska, som en hantverkare som skriver själv. Inga säljfraser, \
inga utropstecken i rad, ingen emoji.
- Kort: 3–6 meningar i brödtexten.
- Personligt: nämn objektet och arbetet så att kunden direkt förstår vilken offert det gäller.
- Hjälpsamt, inte påstridigt. Kunden har sannolikt bara inte hunnit svara.
- Avsluta med en enkel fråga eller ett lätt nästa steg, t.ex. att svara på frågor eller ses på plats.

Hitta inte på något som inte står i underlaget: inga rabatter, inga deadlines, inga lediga datum, \
inga påståenden om material eller garantier. Nämn inte offertbeloppet om det inte känns naturligt.

Tonen beror på vilken uppföljning det är:
1: vänlig koll om offerten kommit fram och om det finns frågor.
2: erbjud att gå igenom offerten eller justera omfattningen.
3 eller senare: artigt avrundande — fråga om de vill gå vidare, och säg att det är helt okej om inte.

Signera med avsändarens namn (om det finns), firmanamn och telefon (om det finns), på separata rader."""

SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "Kort ämnesrad på svenska"},
        "body": {"type": "string", "description": "Hela mejlet inklusive hälsning och signatur"},
    },
    "required": ["subject", "body"],
    "additionalProperties": False,
}


def _first_name(full_name: str) -> str:
    return (full_name or "").strip().split(" ")[0]


def _signature(settings) -> str:
    lines = ["Vänliga hälsningar,"]
    if settings.get("signer_name"):
        lines.append(settings["signer_name"])
    lines.append(settings.get("company_name") or "Takfirman")
    if settings.get("phone"):
        lines.append(f"Tel: {settings['phone']}")
    return "\n".join(lines)


def template_followup(quote, settings, days: int) -> dict:
    """Reservmall när inget API-anrop görs. Varierar efter uppföljningsnummer."""
    n = quote["followups_sent"] + 1
    name = _first_name(quote["customer_name"])
    job = (quote["job_description"] or "takarbetet").strip().rstrip(".")
    greeting = f"Hej {name}!" if name else "Hej!"
    if n == 1:
        text = (
            f"Jag ville bara höra att offerten för {job.lower()} på {quote['property']} kom fram. "
            "Har ni några frågor eller funderingar så svarar jag gärna. "
            "Återkom när det passar, så hjälper vi er vidare."
        )
    elif n == 2:
        text = (
            f"Jag följer upp offerten för {quote['property']} igen. "
            "Om något i offerten känns oklart, eller om ni vill ändra omfattningen, "
            "går jag gärna igenom den med er – på telefon eller på plats."
        )
    else:
        text = (
            f"Jag hör av mig en sista gång angående offerten för {quote['property']}. "
            "Vill ni gå vidare är det bara att svara på det här mejlet. "
            "Har ni bestämt er för något annat är det helt okej – säg gärna till så stänger jag ärendet."
        )
    return {
        "subject": f"Offert för {quote['property']}",
        "body": f"{greeting}\n\n{text}\n\n{_signature(settings)}",
        "source": "mall",
    }


def _client():
    try:
        import anthropic
    except ImportError:
        return None
    # SDK:n hittar själv ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ant-profil.
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or os.environ.get("QUOTEPILOT_FORCE_AI")):
        return None
    # Kort timeout så att ett hängande anrop inte äter upp Vercel-funktionens tid.
    return anthropic.Anthropic(timeout=60.0, max_retries=1)


def ai_enabled() -> bool:
    return _client() is not None


def generate_followup(quote, settings, days: int) -> dict:
    """Returnerar {'subject', 'body', 'source'}. Faller tillbaka på mall vid fel."""
    client = _client()
    if client is None:
        return template_followup(quote, settings, days)

    import anthropic

    facts = {
        "kundens_namn": quote["customer_name"],
        "objekt": quote["property"],
        "arbete": quote["job_description"],
        "offertbelopp_kr": quote["amount_sek"],
        "offert_skickad": quote["sent_date"],
        "dagar_sedan_senaste_kontakt": days,
        "uppfoljning_nummer": quote["followups_sent"] + 1,
        "avsandare_namn": settings.get("signer_name") or "",
        "firmanamn": settings.get("company_name") or "Takfirman",
        "telefon": settings.get("phone") or "",
    }
    try:
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{
                "role": "user",
                "content": "Skriv uppföljningsmejlet utifrån det här underlaget:\n"
                           + json.dumps(facts, ensure_ascii=False, indent=2),
            }],
        )
    except anthropic.APIStatusError as e:
        log.warning("Claude API-fel (%s): %s – använder mall", e.status_code, e.message)
        return template_followup(quote, settings, days)
    except anthropic.APIConnectionError as e:
        log.warning("Nätverksfel mot Claude API: %s – använder mall", e)
        return template_followup(quote, settings, days)

    if response.stop_reason != "end_turn":
        log.warning("Oväntat stop_reason=%s – använder mall", response.stop_reason)
        return template_followup(quote, settings, days)

    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        data = json.loads(text)
        return {"subject": data["subject"].strip(), "body": data["body"].strip(), "source": "ai"}
    except (ValueError, KeyError):
        log.warning("Kunde inte tolka AI-svaret – använder mall")
        return template_followup(quote, settings, days)
