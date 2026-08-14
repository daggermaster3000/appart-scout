"""Playwright browser layer for the portals that block plain HTTP.

Four of the five portals sit behind DataDome or Cloudflare and return 403 to
anything that is not a real browser - including to `httpx` with a perfect set of
browser headers, because the fingerprinting happens at the TLS and behavioural
level, not in the headers.

What was measured while building this (from a non-Swiss datacentre-ish IP):

===========  ================================  ==============================
portal       plain httpx                       Playwright, headed Chromium
===========  ================================  ==============================
flatfox      works (public JSON API)           not needed
immoscout24  403 (host does not even resolve)  **works**, full data in page
comparis     403 DataDome captcha              passes DataDome, URL TBD
newhome      403 Cloudflare challenge          passes challenge, URL TBD
homegate     403 DataDome captcha              still 403
===========  ================================  ==============================

Two findings shaped the design:

* **Headed beats headless.** Headless Chromium (and especially the "headless
  shell" Playwright installs by default) is detected and blocked; the same
  navigation in headed mode goes through. On a headless Raspberry Pi that means
  running under `xvfb-run`, which the systemd unit does.
* **Don't scrape the DOM.** These pages ship their full result set as JSON in a
  `window.__INITIAL_STATE__` / `__NEXT_DATA__` blob. Reading that is far more
  stable than CSS selectors, and gives richer data than the rendered cards.

Homegate is not currently reachable. That costs less than it looks: ImmoScout24
and Homegate are both SMG properties and the ImmoScout24 payload lists
`platforms: ["homegate", "immoscout24", ...]` per listing, i.e. the same
inventory is syndicated across both.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PROFILE_DIR = Path.home() / ".appart-scout" / "browser-profiles"

# Detecting a block is trickier than grepping for "datadome": a page that loaded
# perfectly still embeds DataDome's own script, so the vendor name appears in
# healthy content. Only these mean we were actually served an interstitial.
BLOCK_MARKERS = (
    "geo.captcha-delivery.com",
    "verifying you are human",
    "enable javascript and cookies to continue",
)

#: Challenge pages announce themselves in the title and are always tiny.
CHALLENGE_TITLES = ("just a moment", "nur einen moment", "un instant", "un momento")
CHALLENGE_MAX_BYTES = 60_000

CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#uc-btn-accept-banner",
    "[data-testid='uc-accept-all-button']",
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Alle Cookies akzeptieren')",
    "button:has-text('Akzeptieren')",
    "button:has-text('Zustimmen')",
    "button:has-text('Einverstanden')",
    "button:has-text('Accept all')",
)

# Trim the most obvious automation tells. This is not a serious anti-detection
# suite - it just avoids failing the cheapest checks.
INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['de-CH', 'de', 'en']});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
window.chrome = {runtime: {}, loadTimes: function () {}, csi: function () {}};
"""


class BrowserUnavailable(RuntimeError):
    """Playwright is not installed, or no Chromium could be launched."""


class Blocked(RuntimeError):
    """The portal served an anti-bot interstitial instead of content."""


def is_blocked(html: str, title: str = "") -> bool:
    """Whether this response is an anti-bot interstitial rather than content.

    Deliberately conservative in both directions: a captcha iframe is conclusive,
    a challenge title only counts on a page too small to be real content, and
    the mere presence of an anti-bot vendor's script is not evidence of anything
    (every protected site loads one on its normal pages too).
    """
    lowered = html.lower()
    if any(marker in lowered for marker in BLOCK_MARKERS):
        return True
    if len(html) < CHALLENGE_MAX_BYTES and any(t in title.lower() for t in CHALLENGE_TITLES):
        return True
    # A 403 shell is a few KB with nothing in it.
    return len(html) < 4000 and "captcha" in lowered


class BrowserSession:
    """One Chromium context, reused by every browser-backed source in a run."""

    def __init__(self, context: Any) -> None:
        self.context = context
        self._warmed: set[str] = set()

    async def warm(self, url: str) -> None:
        """Visit a site's front page before deep-linking into a search.

        DataDome hands out a clearance cookie once it is satisfied, and it is
        markedly more willing to do so for someone arriving at the homepage than
        for a cold profile that jumps straight to a filtered result URL. Landing
        on the root first (and keeping the cookie in a persistent profile) is
        what got ImmoScout24 loading reliably.
        """
        origin = "/".join(url.split("/", 3)[:3])
        if not origin or origin in self._warmed:
            return
        self._warmed.add(origin)
        page = await self.context.new_page()
        try:
            await page.goto(origin, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(3000)
            await self._accept_consent(page)
            await page.wait_for_timeout(1500)
        except Exception as exc:
            log.debug("warm-up of %s failed: %s", origin, exc)
        finally:
            await page.close()

    async def load(
        self,
        url: str,
        *,
        wait_ms: int = 8000,
        settle_selector: str | None = None,
        accept_consent: bool = True,
        warm: bool = True,
        raise_on_block: bool = True,
    ) -> str:
        """Navigate and return the settled HTML.

        Raises `Blocked` if we were served an interstitial - unless
        `raise_on_block` is off, which `scout probe` uses so it can dump the
        blocked response for inspection instead of discarding it.
        """
        if warm:
            await self.warm(url)
        page = await self.context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

            # Cloudflare interstitials replace themselves once the challenge
            # completes, so give the title a few chances to change.
            for _ in range(6):
                await page.wait_for_timeout(2500)
                title = (await page.title() or "").lower()
                if title and not any(m in title for m in ("moment", "just a moment")):
                    break

            if accept_consent:
                await self._accept_consent(page)

            if settle_selector:
                try:
                    await page.wait_for_selector(settle_selector, timeout=wait_ms)
                except Exception:
                    log.debug("settle selector %s never appeared on %s", settle_selector, url)
            else:
                await page.wait_for_timeout(wait_ms)

            html = await page.content()
            title = (await page.title() or "").lower()
        finally:
            await page.close()

        if is_blocked(html, title) and raise_on_block:
            raise Blocked(f"anti-bot interstitial served for {url}")
        return html

    async def _accept_consent(self, page: Any) -> None:
        """Dismiss the cookie banner - these SPAs don't fetch results until it's gone."""
        for selector in CONSENT_SELECTORS:
            try:
                element = page.locator(selector).first
                if await element.count() and await element.is_visible():
                    await element.click(timeout=3000)
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue


@asynccontextmanager
async def browser_session(headless: bool | None = None):
    """Launch Chromium with a persistent profile.

    Defaults to headed, because headless is what gets blocked. On a Pi the
    systemd unit wraps the process in `xvfb-run`, so "headed" costs nothing but
    a virtual display. Set `SCOUT_BROWSER_HEADLESS=1` to override.

    `SCOUT_CHROMIUM_PATH` points at a system Chromium - needed on Raspberry Pi
    OS, where Playwright's own arm64 build is unreliable and
    `apt install chromium` is the sane path.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise BrowserUnavailable(
            "playwright is not installed; run: pip install 'appart-scout[browser]' "
            "&& playwright install chromium"
        ) from exc

    if headless is None:
        headless = os.environ.get("SCOUT_BROWSER_HEADLESS", "") == "1"

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    launch: dict[str, Any] = {
        "user_data_dir": str(PROFILE_DIR / "default"),
        "headless": headless,
        "locale": "de-CH",
        "timezone_id": "Europe/Zurich",
        "viewport": {"width": 1440, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        "ignore_default_args": ["--enable-automation"],
    }
    executable = os.environ.get("SCOUT_CHROMIUM_PATH")
    if executable:
        launch["executable_path"] = executable
    else:
        # The full Chromium build, not the far more detectable headless shell.
        launch["channel"] = "chromium"

    async with async_playwright() as pw:
        try:
            context = await pw.chromium.launch_persistent_context(**launch)
        except Exception as exc:
            raise BrowserUnavailable(f"could not launch Chromium: {exc}") from exc
        try:
            await context.add_init_script(INIT_SCRIPT)
            yield BrowserSession(context)
        finally:
            await context.close()


# --------------------------------------------------------------------------
# embedded-state extraction
# --------------------------------------------------------------------------

_STATE_PATTERNS = (
    re.compile(r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', re.S),
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
    re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\})\s*;?\s*</script>", re.S),
)


def extract_state(html: str) -> dict[str, Any] | None:
    """Pull the page's own hydration JSON out of the HTML."""
    for pattern in _STATE_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        try:
            return json.loads(match.group(1))
        except ValueError:
            continue
    return None


def dig(data: Any, *path: str) -> Any:
    """Walk a nested dict path, returning None instead of raising."""
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


async def gather_pages(
    session: BrowserSession,
    urls: list[str],
    *,
    delay: float = 2.0,
    **load_kwargs: Any,
) -> list[str]:
    """Load pages one at a time with a pause - concurrency is what gets noticed."""
    out: list[str] = []
    for i, url in enumerate(urls):
        if i:
            await asyncio.sleep(delay)
        out.append(await session.load(url, **load_kwargs))
    return out
