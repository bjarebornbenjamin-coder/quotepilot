# QuotePilot – AI-uppföljare för takfirmor (MVP)

QuotePilot håller koll på skickade offerter. Systemet visar vilka kunder som inte svarat och skriver förslag på personliga uppföljningsmejl. Ägaren läser, redigerar och godkänner varje mejl.

## Kom igång lokalt

```bash
pip install -r requirements.txt
python app.py            # öppna http://127.0.0.1:5000
```

Klicka på **Testa med demodata**, eller ladda upp er egen offertlista under **Importera**.
Lokalt sparas allt i en SQLite-fil (`quotepilot.db`) och ingen inloggning krävs.

## Publicera på Vercel (med Neon-databas)

Allt görs i Vercels webbgränssnitt. Du behöver bara konton på GitHub och Vercel.

1. **Importera projektet.** Gå till vercel.com/new, välj GitHub-repot och klicka **Deploy**. Första gången visar appen sidan "QuotePilot är inte färdigkonfigurerad" – det är meningen.
2. **Skapa databasen.** I projektet: **Storage → Create Database → Neon** (gratisnivån räcker). Koppla den till projektet; Vercel lägger då in `DATABASE_URL` automatiskt. Tabellerna skapas av appen själv.
3. **Lägg in miljövariabler** under **Settings → Environment Variables**:

   | Namn | Värde |
   |---|---|
   | `QUOTEPILOT_PASSWORD` | Lösenordet för att logga in. Använd minst 12 tecken. |
   | `QUOTEPILOT_SECRET` | En lång slumpsträng (signerar inloggningen). |
   | `CRON_SECRET` | En annan lång slumpsträng (skyddar den dagliga kontrollen). |
   | `ANTHROPIC_API_KEY` | Valfri. Utan den används mallen istället för AI. |

   Slumpsträngar skapar du till exempel med `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
4. **Deploya om:** fliken **Deployments → ⋯ → Redeploy**, så att variablerna läses in.
5. Öppna adressen, logga in och klicka **Testa med demodata** för att se att allt fungerar. Demoofferterna kan tas bort med **Ta bort** innan riktiga offerter importeras.

**Daglig kontroll:** `vercel.json` gör att Vercel anropar `/cron/check-followups` vardagar kl. 05:00 UTC (06:00/07:00 svensk tid) och skapar utkast för offerter som behöver följas upp. Det körs bara på produktionsdeployen. Om Vercel råkar köra den två gånger skapas inga dubbletter. Varje körning håller sig under 4 minuter; blir något över tas det nästa dag.

**Säkerhet:** appen vägrar starta på Vercel utan lösenord, hemlighet och databas, eftersom den innehåller kunders namn och mejladresser. Använd ett eget lösenord per takfirma (en Vercel-deploy och en databas per firma i piloten).

### AI-utkast (valfritt)

Utan API-nyckel skriver QuotePilot utkasten från en enkel mall (tre varianter: första, andra och sista uppföljningen). Med en nyckel skriver Claude ett personligt mejl utifrån offerten:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python app.py
```

Modell: `claude-opus-5-5` (ändras med `QUOTEPILOT_MODEL`). Om API-anropet misslyckas används mallen automatiskt, så demon fungerar alltid.

## Flödet

1. **Importera** – CSV med kolumnerna `kund;epost;objekt;beskrivning;belopp;skickad;status` (semikolon eller komma, export från svensk Excel fungerar). Dubbletter hoppas över.
2. **Bevaka** – en offert med status *Väntar* markeras **Följ upp** när kunden inte hörts av på X dagar (standard 5).
3. **Utkast** – AI:n (eller mallen) skriver ett mejl. Tonen ändras mellan första, andra och sista uppföljningen.
4. **Godkänn** – öppna mejlet i ditt mejlprogram, skicka och klicka på *Godkänn*. Klockan börjar då om från idag.
5. **Svar** – markera *Kunden har svarat*, *Vunnen* eller *Förlorad*. Då stoppas alla påminnelser för offerten.

Efter max antal uppföljningar (standard 3) slutar QuotePilot föreslå fler mejl.

## Daglig kontroll utan Vercel

På en egen server kan samma kontroll köras från kommandoraden, till exempel via cron:

```bash
flask --app app check-followups
```

## Pilotmätning

Längst ner på översikten finns de siffror du behöver efter en pilot: skickade uppföljningar, svar efter uppföljning, vunna jobb efter uppföljning och deras ordervärde. Ett svar räknas bara som "efter uppföljning" om minst en uppföljning hade godkänts innan kunden markerades som svarat.

## Filer

| Fil | Innehåll |
|---|---|
| `app.py` | Flask-routes, inloggning, cron-endpoint, demodata, CLI-kommandon |
| `db.py` | Postgres/SQLite, uppföljningslogik, pilotstatistik |
| `vercel.json` | Schemat för den dagliga kontrollen |
| `public/static/` | CSS (Vercel serverar statiska filer från `public/`) |
| `ai.py` | Prompt och anrop till Claude API, plus reservmall |
| `importer.py` | CSV-tolkning (datum- och beloppsformat, kolumnalias) |
| `tests/` | `python -m pytest tests` (sätt `TEST_DATABASE_URL` för att även testa mot Postgres) |

## Inte med i MVP:n (version 2)

- Själva mejlutskicket (idag via ditt eget mejlprogram)
- Gmail/Outlook-koppling som upptäcker skickade offerter och svar automatiskt
- Flera firmor i samma installation (idag: en deploy per firma) och personliga användarkonton
