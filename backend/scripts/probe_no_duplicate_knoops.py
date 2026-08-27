"""Check expanded knoop chains avoid revisiting the same geoid when possible."""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import knooppunten as knoop_service
from app.services.planner import _build_knoop_route


def duplicate_numbers(chain: list[dict]) -> list[str]:
    counts = Counter(str(n["number"]) for n in chain)
    return [num for num, count in counts.items() if count > 1]


def duplicate_geoids(chain: list[dict]) -> list[int]:
    geoids = [int(n["geoid"]) for n in chain if n.get("geoid") is not None]
    counts = Counter(geoids)
    return [g for g, count in counts.items() if count > 1]


async def main() -> None:
    nodes, _ = await knoop_service.fetch_network_for_chain(
        [{"lat": 51.16, "lng": 4.36, "number": "42"}]
    )
    if len(nodes) < 4:
        print("SKIP: not enough network nodes")
        return
    chain = nodes[:4]
    numbers = [n["number"] for n in chain]
    print("spine", numbers)
    expanded, route = await _build_knoop_route(
        chain[0]["lat"],
        chain[0]["lng"],
        chain,
        close_loop=False,
    )
    expanded_numbers = [n["number"] for n in expanded]
    print("expanded", expanded_numbers)
    dup_num = duplicate_numbers(expanded)
    dup_geo = duplicate_geoids(expanded)
    print("duplicate numbers", dup_num or "none")
    print("duplicate geoids", dup_geo or "none")
    print("km", round(route["distance_m"] / 1000, 2))
    if dup_num or dup_geo:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
