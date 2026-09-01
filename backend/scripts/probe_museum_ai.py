"""Probe museum extra-wens: preview suggesties + Gemini-antwoord bij plan."""
from __future__ import annotations

import asyncio
import json

from app.models import PlanRequest, RiderProfile
from app.services import pois as pois_service
from app.services.ai import enrich_with_ai
from app.services.planner import plan_route, preview_route


async def main() -> None:
    lat, lng = 51.05, 3.72
    notes = "museum"

    print("=== PREVIEW (suggestieoverzicht, geen Gemini) ===")
    print("herkende interesses:", pois_service.wish_interests_for_notes(notes))
    preview = await preview_route(
        lat, lng, 25, "lus", notes=notes, profile_interests=["geschiedenis"]
    )
    sugs = preview.get("suggestions", [])
    print("aantal suggesties:", len(sugs))
    for s in sugs[:10]:
        print(
            f"  - {s['name']} ({s.get('kind_label')}) "
            f"interest={s.get('interest')} on_route={s.get('on_route')}"
        )

    print("\n=== GEMINI (alleen bij 'Plan deze route') ===")
    req = PlanRequest(
        start="Gent",
        mode="lus",
        distance_km=25,
        interests=["geschiedenis"],
        notes=notes,
        profile=RiderProfile(),
    )
    try:
        result = await plan_route(req)
        print("titel:", result.get("title"))
        print("intro:", result.get("intro"))
        print("route_reason:", result.get("route_reason"))
        stops = result.get("stops") or []
        print("stops:", len(stops))
        for stop in stops[:5]:
            print(f"  - {stop['name']}: {stop.get('why', '')[:80]}")
    except Exception as exc:
        print("plan_route fout:", type(exc).__name__, exc)

    print("\n=== DIRECT enrich_with_ai (ruwe Gemini JSON) ===")
    ai = await enrich_with_ai(
        "Gent",
        ["geschiedenis"],
        notes,
        [{"id": "test1", "name": "STAM", "kind_label": "museum", "interest": "geschiedenis", "wiki": {"summary": "Stadsmuseum Gent."}}],
        "lus",
        25,
        "",
        [{"id": "n1", "number": "75", "match_score": 3, "nearby": [{"name": "STAM", "kind": "museum"}]}],
        RiderProfile(),
    )
    if ai:
        print(json.dumps(ai, ensure_ascii=False, indent=2))
    else:
        print("Geen AI-antwoord (key ontbreekt of Gemini faalde — check backend-console voor 'Gemini ... failed')")


if __name__ == "__main__":
    asyncio.run(main())
