"""Base class shared by every portal adapter.

Adapters are deliberately database-free: `fetch()` gets a `state` dict (loaded
from the `cursor` table) and returns an updated one. That keeps them trivially
testable against recorded fixtures, and means a broken adapter can never corrupt
the database - the pipeline decides what to persist.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..models import Criteria, Listing, Settings

log = logging.getLogger(__name__)

# A plain, current desktop UA. These portals serve the same JSON to a browser;
# sending python-httpx/x.y just gets us a 403 for no reason.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class SourceError(RuntimeError):
    """Adapter could not complete. Recorded per-run, never fatal to the run."""


class Source(ABC):
    name: str = ""
    label: str = ""
    #: Portals behind DataDome/Cloudflare need a real browser; the pipeline
    #: starts one shared Chromium and assigns it to `self.session` before
    #: calling `fetch()`. See `scout.browser`.
    needs_browser: bool = False
    session: Any = None
    #: minimum seconds between requests to this host
    rate_limit: float = 1.0
    #: give up on a single source after this long so one slow portal cannot
    #: stall the whole run
    timeout: float = 30.0
    budget: float = 300.0

    def __init__(self) -> None:
        self._last_request = 0.0

    # -- required ----------------------------------------------------------

    @abstractmethod
    async def fetch(
        self,
        client: httpx.AsyncClient,
        criteria: Criteria,
        settings: Settings,
        state: dict[str, Any],
    ) -> tuple[list[Listing], dict[str, Any]]:
        """Return listings plus the state to persist for the next run."""

    # -- helpers -----------------------------------------------------------

    async def _throttle(self) -> None:
        loop = asyncio.get_running_loop()
        wait = self._last_request + self.rate_limit - loop.time()
        if wait > 0:
            # Jitter so repeated runs don't hammer in a machine-gun rhythm.
            await asyncio.sleep(wait + random.uniform(0, 0.25))
        self._last_request = asyncio.get_running_loop().time()

    @retry(
        retry=retry_if_exception_type(
            (httpx.TransportError, httpx.HTTPStatusError, httpx.TimeoutException)
        ),
        wait=wait_exponential(multiplier=1.5, min=2, max=20),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        await self._throttle()
        resp = await client.request(method, url, timeout=self.timeout, **kwargs)
        # 4xx other than 429 are not worth retrying - the request itself is wrong.
        if resp.status_code == 429 or resp.status_code >= 500:
            resp.raise_for_status()
        return resp

    async def get_json(
        self, client: httpx.AsyncClient, url: str, **kwargs: Any
    ) -> dict[str, Any]:
        resp = await self._request(client, "GET", url, **kwargs)
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: GET {url} -> HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:  # HTML error page where JSON was expected
            raise SourceError(f"{self.name}: non-JSON response from {url}") from exc

    async def post_json(
        self, client: httpx.AsyncClient, url: str, payload: Any, **kwargs: Any
    ) -> dict[str, Any]:
        resp = await self._request(client, "POST", url, json=payload, **kwargs)
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: POST {url} -> HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"{self.name}: non-JSON response from {url}") from exc

    async def get_text(
        self, client: httpx.AsyncClient, url: str, **kwargs: Any
    ) -> str:
        resp = await self._request(client, "GET", url, **kwargs)
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: GET {url} -> HTTP {resp.status_code}")
        return resp.text


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
        },
        follow_redirects=True,
        timeout=30.0,
    )
