from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app.config import settings

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@asynccontextmanager
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        headers={"User-Agent": settings.user_agent},
        follow_redirects=True,
    ) as http:
        yield http
