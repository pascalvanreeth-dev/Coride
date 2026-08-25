from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.config import settings
from app.http import client

MIN_INTERVAL_S = 1.1
CACHE_TTL_S = 6 * 3600
MAX_RETRIES = 2

_lock = asyncio.Lock()
_last_request = 0.0
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    row = _cache.get(key)
    if not row:
        return None
    saved_at, value = row
    if time.monotonic() - saved_at > CACHE_TTL_S:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)
    if len(_cache) > 500:
        oldest = sorted(_cache.items(), key=lambda item: item[1][0])[:100]
        for stale_key, _ in oldest:
            _cache.pop(stale_key, None)


async def _throttle() -> None:
    global _last_request
    async with _lock:
        now = time.monotonic()
        wait = MIN_INTERVAL_S - (now - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request = time.monotonic()


async def _get(path: str, params: dict[str, Any]) -> Any:
    cache_key = f"{path}?{'&'.join(f'{k}={params[k]}' for k in sorted(params))}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        await _throttle()
        try:
            async with client() as http:
                response = await http.get(f"{settings.nominatim_url}{path}", params=params)
                if response.status_code == 429:
                    last_error = httpx.HTTPStatusError(
                        "Nominatim rate limit",
                        request=response.request,
                        response=response,
                    )
                    await asyncio.sleep(MIN_INTERVAL_S * (attempt + 2))
                    continue
                response.raise_for_status()
                payload = response.json()
                _cache_set(cache_key, payload)
                return payload
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code == 429 and attempt < MAX_RETRIES:
                await asyncio.sleep(MIN_INTERVAL_S * (attempt + 2))
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError("Nominatim request failed")


async def search(query: str, limit: int = 5) -> list[dict[str, Any]]:
    payload = await _get(
        "/search",
        {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": limit,
            "countrycodes": "be",
            "accept-language": "nl,fr,de,en",
        },
    )
    return payload if isinstance(payload, list) else []


async def reverse(
    lat: float,
    lng: float,
    *,
    zoom: int = 14,
    extratags: bool = False,
    namedetails: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "lat": lat,
        "lon": lng,
        "format": "jsonv2",
        "addressdetails": 1,
        "zoom": zoom,
        "accept-language": "nl,fr,de,en",
    }
    if extratags:
        params["extratags"] = 1
    if namedetails:
        params["namedetails"] = 1
    payload = await _get("/reverse", params)
    return payload if isinstance(payload, dict) else {}
