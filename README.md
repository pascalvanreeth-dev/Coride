# Veloverhaal

Fietsgids voor België: je vult startpunt, lus of eindpunt, en interesses in. De app plant een **fietsroute langs echte plekken** (erfgoed, activiteiten, evenementen) en **vertelt onderweg** wat je ziet.

Dit is de eerste werkende versie. We kunnen hem samen verder uitbouwen.

## Wat zit erin

- **React** frontend met CyclOSM-kaart
- **Python / FastAPI** backend
- **OSRM** fietsrouting op OpenStreetMap (open source)
- **Overpass API** voor historische gebouwen, parken, theaters, markten, …
- **Wikipedia / Wikidata** voor uitleg bij gebouwen
- **Open Data Brussels** voor evenementen als je in of rond Brussel start
- Optioneel **Gemini Flash-Lite** voor een warmere gesproken gids

## Starten

Twee terminals.

### 1. Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Optioneel: kopieer `.env.example` naar `.env` en vul `OPENAI_API_KEY` in.

### 2. Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open [http://localhost:5173](http://localhost:5173). Probeer de preset **Gent · geschiedenis**, daarna **Simuleer rit** om de gids te horen zonder te fietsen.

Op een andere pc in hetzelfde thuisnetwerk: `http://<jouw-lan-ip>:5173` (de frontend luistert op het LAN). De API blijft via de Vite-proxy lopen, dus alleen poort 5173 moet bereikbaar zijn.

Op de fiets: **Live gids** (GPS). Als je een gebouw nadert, spreekt de app uit wat het is en waarom het telt.

## Bronnen

| Onderdeel | Bron | Open? |
| --- | --- | --- |
| Kaart | OpenStreetMap + CyclOSM | ja |
| Fietsroute | OSRM / routing.openstreetmap.de | ja |
| Plekken | OSM Overpass | ja |
| Uitleg | Wikipedia, Wikidata | ja |
| Evenementen | Open Data Brussels | ja, beperkt tot Brussel |
| Extra gidsstem | OpenAI (optioneel) | nee, optioneel |

UiTdatabank (Vlaanderen) is de rijkste evenementenbron, maar de zoek-API is niet gratis. Die kunnen we later aansluiten met een key van [publiq](https://platform.publiq.be).

## Volgende stappen (samen)

- Fietsknooppuntennetwerk als extra routinglaag
- UiTdatabank / regionale evenementen
- Offline caching van de route voor onderweg
- Franse en Duitse UI voor Wallonië en Oost-België
