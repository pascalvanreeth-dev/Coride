"""Test suggestieoverzicht met Gemini voor museum-wens."""
from __future__ import annotations

import asyncio

from app.services.planner import preview_route


async def main() -> None:
    preview = await preview_route(
        51.05,
        3.72,
        25,
        "lus",
        notes="museum",
        profile_interests=["geschiedenis"],
    )
    print("wish_summary:", preview.get("wish_summary"))
    sugs = preview.get("suggestions", [])
    print("suggestions:", len(sugs))
    for item in sugs[:8]:
        print(f"  - {item['name']} | {item.get('hint') or item.get('kind_label')}")


if __name__ == "__main__":
    asyncio.run(main())
