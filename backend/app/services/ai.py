from __future__ import annotations

import json
import re
from typing import Any

from app.config import settings
from app.services.wikipedia import first_sentences

LEVEL_HINTS = {
    "kort": "arrived: 1 zin. approaching: 1 korte zin. why: max 1 korte zin.",
    "normaal": "arrived: 2 zinnen. approaching: 1 zin. why: 1 zin.",
    "uitgebreid": "arrived: 4 tot 5 zinnen met details. approaching: 2 zinnen. why: 2 zinnen.",
}

SENTENCE_COUNTS = {"kort": 1, "normaal": 2, "uitgebreid": 4}


def fallback_scripts(poi: dict[str, Any], wiki: dict[str, str], level: str = "normaal") -> dict[str, str]:
    kind = poi.get("kind_label") or "plek"
    count = SENTENCE_COUNTS.get(level, 2)
    extract = first_sentences(wiki.get("summary") or poi.get("description") or "", count)
    name = poi["name"]
    approaching = f"Je nadert {name}, een {kind}."
    arrived = extract or f"{name} is een {kind} langs je fietsroute."
    why = {
        "geschiedenis": f"{name} past bij je interesse in geschiedenis.",
        "natuur": f"{name} is een groene stop langs je fietsroute.",
        "landbouw": f"{name} laat iets zien van het landschap en de streek.",
        "horeca": f"{name} is een plek om even af te stappen en iets te nuttigen.",
        "oorlog": f"{name} herinnert aan een stuk oorlogsgeschiedenis.",
        "architectuur": f"{name} is architecturaal de moeite om even bij stil te staan.",
        "activiteiten": f"{name} is een plek om even af te stappen.",
        "evenementen": f"{name} is een evenement of evenementenlocatie.",
    }.get(poi.get("interest"), f"{name} hoort bij de criteria die je koos.")
    if level == "kort":
        why = ""
    return {"approaching": approaching, "arrived": arrived, "why": why, "summary": extract or arrived}


def has_ai() -> bool:
    return bool(settings.gemini_api_key or settings.openai_api_key)


async def enrich_with_ai(
    start_label: str,
    interests: list[str],
    notes: str,
    candidates: list[dict[str, Any]],
    mode: str,
    distance_km: int,
    knoop_chain: str = "",
    nodes: list[dict[str, Any]] | None = None,
    profile: Any | None = None,
) -> dict[str, Any] | None:
    if not has_ai():
        return None
    compact = [
        {
            "id": c["id"],
            "name": c["name"],
            "kind": c.get("kind_label"),
            "interest": c.get("interest"),
            "summary": (c.get("wiki") or {}).get("summary", "")[:220],
        }
        for c in (candidates or [])[:16]
    ]
    node_pack = []
    for node in (nodes or [])[:36]:
        node_pack.append(
            {
                "id": node["id"],
                "number": node["number"],
                "match_score": node.get("match_score", 0),
                "nearby": [
                    f"{p['name']} ({p['kind']})" for p in (node.get("nearby") or [])[:4]
                ],
            }
        )
    start_id = (nodes or [{}])[0].get("id") if nodes else ""
    rider = {}
    if profile:
        rider = {
            "leeftijd": getattr(profile, "age_band", ""),
            "tempo": getattr(profile, "fitness", ""),
            "fiets": getattr(profile, "bike", ""),
            "horeca": list(getattr(profile, "horeca", []) or []),
            "commentaar": getattr(profile, "commentary", "normaal"),
        }
    prompt = (
        "Pas de fietsroute AAN op de extra wens en het fietsersprofiel. "
        "Kies knooppunten waar die wens het best klopt. "
        "Voorbeeld: extra='cafe' -> knooppunten met cafés/pubs in de buurt, niet een willekeurige lus.\n"
        f"{json.dumps({'start': start_label, 'start_knoop_id': start_id, 'mode': mode, 'distance_km': distance_km, 'interests': interests, 'extra_wens': notes, 'fietser': rider, 'knooppunten': node_pack, 'plekken_voor_uitleg': compact}, ensure_ascii=False)}"
    )
    system = (
        "Je bent een fietsrouteplanner in Vlaanderen. "
        "Kies een fietsbare knooppuntenlus die de extra wens volgt. "
        "JSON keys: knoop_ids (verplichte lijst van knooppunt-id's in volgorde, 4 tot 7 stuks, "
        "begin met start_knoop_id), title, intro, reason (1 zin waarom deze knooppunten), "
        "stop_ids (plekken voor uitleg), scripts (id -> {approaching, arrived, why}). "
        "Nederlands, kort, niet verzinnen. Alleen bestaande ids."
    )
    parsed = await _generate_json(system, prompt)
    return parsed if isinstance(parsed, dict) else None


async def polish_scripts(stops: list[dict[str, Any]], explanation_level: str = "normaal") -> list[dict[str, Any]]:
    if not has_ai() or not stops:
        return stops
    payload = [
        {"id": s["id"], "name": s["name"], "kind": s.get("kind_label"), "summary": s.get("summary", "")[:500]}
        for s in stops
    ]
    hint = LEVEL_HINTS.get(explanation_level, LEVEL_HINTS["normaal"])
    parsed = await _generate_json(
        "Herschrijf tot gesproken fietsgids-teksten in het Nederlands. "
        f"Uitlegniveau: {explanation_level}. {hint} "
        "JSON: {scripts: {id: {approaching, arrived, why}}}. Niet verzinnen.",
        json.dumps(payload, ensure_ascii=False),
    )
    scripts = (parsed or {}).get("scripts") or {}
    for stop in stops:
        extra = scripts.get(stop["id"]) or {}
        stop["approaching"] = extra.get("approaching") or stop["approaching"]
        stop["arrived"] = extra.get("arrived") or stop["arrived"]
        stop["why"] = extra.get("why") or stop["why"]
    return stops


async def answer_about_stop(
    name: str,
    kind: str,
    summary: str,
    arrived: str,
    question: str,
    explanation_level: str = "normaal",
    lat: float | None = None,
    lng: float | None = None,
    heading: float | None = None,
    place_name: str | None = None,
    interests: list[str] | None = None,
) -> str:
    length = {
        "kort": "Antwoord in 1 tot 2 zinnen.",
        "normaal": "Antwoord in 3 tot 5 zinnen.",
        "uitgebreid": "Antwoord in een kort, duidelijk alineaatje van 6 tot 10 zinnen.",
    }.get(explanation_level, "Antwoord in 3 tot 5 zinnen.")
    fallback = first_sentences(summary or arrived or "", SENTENCE_COUNTS.get(explanation_level, 2))
    if not has_ai():
        return fallback or f"Over {name or 'deze plek'} heb ik nu geen extra details."

    nearby = []
    if lat is not None and lng is not None:
        try:
            from app.services import pois as pois_service

            nearby = await pois_service.fetch_pois(lat, lng, 350, interests or ["geschiedenis", "architectuur"], None)
            nearby = nearby[:6]
        except Exception:
            nearby = []

    direction = ""
    if heading is not None:
        dirs = ["noord", "noordoost", "oost", "zuidoost", "zuid", "zuidwest", "west", "noordwest"]
        direction = dirs[int((heading + 22.5) % 360 // 45)]

    parsed = await _generate_json(
        "Je bent een fietsgids in Vlaanderen die live meefietst. "
        "Beantwoord een gesproken vraag van de fietser. "
        f"{length} Nederlands. Gebruik alleen de gegeven context en nabije plekken. "
        "Als de vraag over 'rechts/links/dat gebouw' gaat, kies de meest waarschijnlijke nabije plek. "
        "Verzin geen feiten; zeg het als je het niet zeker weet. JSON: {answer: string}.",
        json.dumps(
            {
                "vraag": question,
                "actieve_plek": name,
                "soort": kind,
                "dorp": place_name or "",
                "rijrichting": direction,
                "positie": {"lat": lat, "lng": lng} if lat is not None else None,
                "samenvatting": (summary or "")[:900],
                "gids_tekst": (arrived or "")[:500],
                "interesses": interests or [],
                "nabije_plekken": [
                    {
                        "name": p.get("name"),
                        "kind": p.get("kind_label") or p.get("kind"),
                        "lat": p.get("lat"),
                        "lng": p.get("lng"),
                    }
                    for p in nearby
                ],
            },
            ensure_ascii=False,
        ),
    )
    answer = (parsed or {}).get("answer") if isinstance(parsed, dict) else None
    return (answer or fallback or f"Ik heb geen extra info over {name or 'deze plek'}.").strip()


async def describe_surroundings(
    place_ctx: dict[str, Any],
    pois: list[dict[str, Any]],
    landscape: list[str],
    interests: list[str],
    explanation_level: str = "normaal",
    heading: float | None = None,
) -> str | None:
    if not has_ai():
        return None
    length = {
        "kort": "Eén zin over de omgeving.",
        "normaal": "Twee tot drie zinnen over landschap en wat opvalt in de buurt.",
        "uitgebreid": "Drie tot vijf zinnen met landschap, streek en opvallende plekken.",
    }.get(explanation_level, "Twee tot drie zinnen.")
    direction = ""
    if heading is not None:
        dirs = ["noord", "noordoost", "oost", "zuidoost", "zuid", "zuidwest", "west", "noordwest"]
        direction = dirs[int((heading + 22.5) % 360 // 45)]
    parsed = await _generate_json(
        "Je bent een fietsgids in Vlaanderen die live meefietst. "
        f"Beschrijf proactief de omgeving binnen 350 meter. {length} Nederlands. "
        "Focus op landschap, streek en plekken die passen bij de interesses van de fietser. "
        "Gebruik alleen de gegeven context. Verzin geen feiten of plekken. "
        "JSON: {summary: string}.",
        json.dumps(
            {
                "dorp": place_ctx.get("place_name") or "",
                "gemeente": place_ctx.get("municipality") or "",
                "streekfeit": (place_ctx.get("local_fact") or "")[:400],
                "landschap": landscape,
                "interesses": interests,
                "rijrichting": direction,
                "plekken_in_buurt": [
                    {
                        "name": p.get("name"),
                        "kind": p.get("kind_label") or p.get("kind"),
                        "interest": p.get("interest"),
                    }
                    for p in pois
                ],
            },
            ensure_ascii=False,
        ),
    )
    summary = (parsed or {}).get("summary") if isinstance(parsed, dict) else None
    return summary.strip() if summary else None


async def _generate_json(system: str, user: str) -> dict[str, Any] | None:
    if settings.gemini_api_key:
        result = await _gemini_json(system, user)
        if result:
            return result
    return None


async def _gemini_json(system: str, user: str) -> dict[str, Any] | None:
    try:
        from google import genai
        from google.genai import types
    except Exception:
        return None

    models = [settings.gemini_model, "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]
    seen: set[str] = set()
    client = genai.Client(api_key=settings.gemini_api_key)
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            config_kwargs: dict[str, Any] = {
                "system_instruction": system,
                "response_mime_type": "application/json",
                "temperature": 0.4,
            }
            try:
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass
            response = await client.aio.models.generate_content(
                model=model,
                contents=user,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            text = getattr(response, "text", None) or ""
            parsed = _parse_json(text)
            if parsed:
                return parsed
        except Exception as exc:
            print(f"Gemini {model} failed: {type(exc).__name__}: {exc}")
            continue
    return None


def _parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
