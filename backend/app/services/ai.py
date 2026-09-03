from __future__ import annotations

import json
import re
import uuid
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


VALID_INTERESTS = frozenset(
    {
        "geschiedenis",
        "natuur",
        "landbouw",
        "horeca",
        "oorlog",
        "architectuur",
        "activiteiten",
        "evenementen",
    }
)


async def interpret_wish_notes(
    notes: str,
    profile_interests: list[str] | None = None,
) -> dict[str, Any] | None:
    """Gebruik Gemini om vrije extra-wens-tekst te interpreteren."""
    if not has_ai() or not (notes or "").strip():
        return None
    parsed = await _generate_json(
        "Je bent een fietsroute-assistent in Vlaanderen. "
        "Interpreteer de vrije extra_wens van een fietser voor een fietsroute. "
        "Map naar interesses: geschiedenis, natuur, landbouw, horeca, oorlog, architectuur, activiteiten, evenementen. "
        "JSON: {interests: [geldige keys], keywords: [1-6 zoektermen], summary: 1 korte zin in het Nederlands}. "
        "Alleen interesses die echt passen. Nederlands.",
        json.dumps(
            {"extra_wens": notes.strip(), "profiel_interesses": list(profile_interests or [])},
            ensure_ascii=False,
        ),
        temperature=0.25,
    )
    if not isinstance(parsed, dict):
        return None
    interests = [item for item in (parsed.get("interests") or []) if item in VALID_INTERESTS]
    keywords = [str(item).strip() for item in (parsed.get("keywords") or []) if str(item).strip()]
    summary = str(parsed.get("summary") or "").strip()
    if not interests and not keywords and not summary:
        return None
    return {"interests": interests[:4], "keywords": keywords[:6], "summary": summary}


async def rank_wish_poi_suggestions(
    notes: str,
    candidates: list[dict[str, Any]],
    profile_interests: list[str] | None = None,
    wish_interests: list[str] | None = None,
    *,
    target_count: int | None = None,
    route_km: float | None = None,
) -> dict[str, Any] | None:
    """Laat Gemini de beste plekken kiezen voor het suggestieoverzicht langs de route."""
    if not has_ai() or not (notes or "").strip() or not candidates:
        return None
    wanted = int(target_count or 12)
    wanted = max(8, min(36, wanted))
    lo = max(6, wanted - 4)
    hi = min(36, wanted + 4)
    compact = [
        {
            "id": str(c["id"]),
            "name": c["name"],
            "kind": c.get("kind_label") or c.get("kind"),
            "interest": c.get("interest"),
            "on_route": bool(c.get("on_route")),
            "progress": c.get("route_progress"),
            "lat": round(float(c["lat"]), 4) if c.get("lat") is not None else None,
            "lng": round(float(c["lng"]), 4) if c.get("lng") is not None else None,
        }
        for c in candidates[:80]
        if c.get("id") and c.get("name")
    ]
    if not compact:
        return None
    route_hint = ""
    if route_km and route_km > 0:
        route_hint = (
            f"De fietsroute is ongeveer {route_km:.0f} km. "
            "Spreid de suggesties over begin, midden en einde (gebruik progress 0–1 of lat/lng). "
        )
    parsed = await _generate_json(
        "Je bent een fietsgids in Vlaanderen. "
        "Bekijk de kandidaten langs de geplande fietsroute en kies een selectie suggesties "
        "die passen bij de extra_wens van de fietser. "
        f"{route_hint}"
        f"Kies bij voorkeur {wanted} plekken (minstens {lo}, max {hi}). "
        "Geef diversiteit (niet allemaal hetzelfde type of dezelfde stad). "
        "Prefer plekken met on_route=true als die passen. "
        "JSON: {pick_ids: [id strings, beste eerst], hints: {id: korte reden max 12 woorden}, summary: 1 zin}. "
        "Alleen ids uit plekken. Nederlands. Verzin geen plekken.",
        json.dumps(
            {
                "extra_wens": notes.strip(),
                "route_km": round(float(route_km), 1) if route_km else None,
                "doel_aantal": wanted,
                "profiel_interesses": list(profile_interests or []),
                "route_interesses": list(wish_interests or []),
                "plekken": compact,
            },
            ensure_ascii=False,
        ),
        temperature=0.35,
    )
    if not isinstance(parsed, dict):
        return None
    pick_ids = [str(item) for item in (parsed.get("pick_ids") or []) if str(item).strip()]
    hints_raw = parsed.get("hints") if isinstance(parsed.get("hints"), dict) else {}
    hints = {str(key): str(value).strip() for key, value in hints_raw.items() if str(value).strip()}
    summary = str(parsed.get("summary") or "").strip()
    if not pick_ids and not summary:
        return None
    return {"pick_ids": pick_ids[:hi], "hints": hints, "summary": summary}


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


def _normalize_answer(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


_STOPWORDS = {
    "aan", "als", "bij", "dan", "dat", "de", "dit", "een", "en", "er", "het", "hier",
    "hoe", "in", "is", "je", "met", "mijn", "nog", "of", "om", "ook", "op", "over",
    "te", "tot", "van", "voor", "wat", "wel", "wie", "zijn", "daar", "die", "deze",
}


def _tokens(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-zà-ÿ0-9]+", (text or "").lower())
        if word not in _STOPWORDS and len(word) > 2
    }


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if part.strip()]


def _grounded_answer(
    question: str,
    summary: str,
    arrived: str,
    name: str,
    last_answer: str,
    nearby: list[dict[str, Any]],
    explanation_level: str,
) -> str:
    wanted = SENTENCE_COUNTS.get(explanation_level, 2)
    used = _normalize_answer(last_answer)
    q_tokens = _tokens(question)
    pool: list[str] = []
    pool.extend(_split_sentences(summary or ""))
    pool.extend(_split_sentences(arrived or ""))
    for poi in nearby:
        label = poi.get("kind_label") or poi.get("kind") or "plek"
        poi_name = poi.get("name") or ""
        desc = poi.get("description") or poi.get("summary") or ""
        if poi_name:
            pool.append(f"{poi_name} is een {label} in de buurt.")
        pool.extend(_split_sentences(desc))

    unique: list[str] = []
    seen: set[str] = set()
    for sentence in pool:
        key = _normalize_answer(sentence)
        if len(key) < 12 or key in seen:
            continue
        if key.startswith("je staat ") or key.startswith("je nadert "):
            continue
        seen.add(key)
        unique.append(sentence)

    unused = [s for s in unique if _normalize_answer(s) not in used]
    candidates = unused or unique

    def score(sentence: str) -> int:
        return len(q_tokens & _tokens(sentence))

    matched_unused = [s for s in unused if score(s) > 0]
    matched_all = [s for s in unique if score(s) > 0]
    if q_tokens and matched_unused:
        picked = sorted(matched_unused, key=score, reverse=True)[:wanted]
    elif q_tokens and matched_all:
        picked = sorted(matched_all, key=score, reverse=True)[:wanted]
    else:
        picked = candidates[:wanted]

    text = " ".join(picked).strip()
    if last_answer and _normalize_answer(text) == _normalize_answer(last_answer):
        return "Ik heb daar geen extra details over in mijn bronnen."
    if text:
        return text
    return f"Over {name or 'deze plek'} heb ik nu geen extra details."


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
    history: list[Any] | None = None,
) -> str:
    length = {
        "kort": "Antwoord in 1 tot 2 zinnen.",
        "normaal": "Antwoord in 3 tot 5 zinnen.",
        "uitgebreid": "Antwoord in een kort, duidelijk alineaatje van 6 tot 10 zinnen.",
    }.get(explanation_level, "Antwoord in 3 tot 5 zinnen.")
    turns = []
    for item in history or []:
        q = getattr(item, "q", None) or (item.get("q") if isinstance(item, dict) else "")
        a = getattr(item, "a", None) or (item.get("a") if isinstance(item, dict) else "")
        if q and a:
            turns.append({"q": str(q)[:400], "a": str(a)[:800]})
    last_answer = turns[-1]["a"] if turns else ""
    nearby = []
    if lat is not None and lng is not None:
        try:
            from app.services import pois as pois_service

            nearby = await pois_service.fetch_pois(lat, lng, 350, interests or ["geschiedenis", "architectuur"], None)
            nearby = nearby[:6]
        except Exception:
            nearby = []
    grounded = _grounded_answer(
        question, summary, arrived, name, last_answer, nearby, explanation_level
    )
    if not has_ai():
        return grounded

    direction = ""
    if heading is not None:
        dirs = ["noord", "noordoost", "oost", "zuidoost", "zuid", "zuidwest", "west", "noordwest"]
        direction = dirs[int((heading + 22.5) % 360 // 45)]

    payload = {
        "huidige_vraag": question,
        "eerder_gesprek": turns,
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
    }
    system = (
        "Je bent een fietsgids in Vlaanderen die live meefietst. "
        "Beantwoord ALLEEN de huidige_vraag. "
        f"{length} Nederlands. Gebruik de context en nabije plekken. "
        "Als er een eerder_gesprek is, geef nieuwe informatie; herhaal dat vorige antwoord niet. "
        "Als de vraag over 'rechts/links/dat gebouw' gaat, kies de meest waarschijnlijke nabije plek. "
        "Verzin geen feiten; zeg het als je het niet zeker weet. JSON: {answer: string}."
    )

    async def _ask(extra: str = "") -> str:
        prompt = (
            f"verzoek {uuid.uuid4().hex[:8]}\n"
            f"Huidige vraag: {question}\n"
            f"{extra}"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        parsed = await _generate_json(system, prompt, temperature=0.7)
        answer = (parsed or {}).get("answer") if isinstance(parsed, dict) else None
        return (answer or "").strip()

    answer = await _ask()
    if last_answer and answer and _normalize_answer(answer) == _normalize_answer(last_answer):
        answer = await _ask(
            "BELANGRIJK: je vorige antwoord was identiek. Geef nu een ander antwoord op de nieuwe vraag.\n"
        )
    if last_answer and answer and _normalize_answer(answer) == _normalize_answer(last_answer):
        return grounded
    if answer:
        return answer
    return grounded


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


async def _generate_json(system: str, user: str, temperature: float = 0.4) -> dict[str, Any] | None:
    if settings.gemini_api_key:
        result = await _gemini_json(system, user, temperature=temperature)
        if result:
            return result
    return None


async def _gemini_json(system: str, user: str, temperature: float = 0.4) -> dict[str, Any] | None:
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
                "temperature": temperature,
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
