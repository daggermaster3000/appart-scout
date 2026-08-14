"""Photo evaluation with OpenAI.

Listing photos carry most of what a description hides: whether the kitchen was
last touched in 1978, whether "hell und freundlich" means daylight or a
north-facing lightwell, whether the bathroom is worn.

Cost control is the whole design here. Vision calls are the only part of a run
that costs money, so:

* metadata scoring runs first and only the top N candidates get photographed;
* at most `vision_max_photos` images per listing, downscaled to 768 px;
* results are persisted per listing id and **never recomputed**, so cost tracks
  new listings, not catalogue size.

In practice that is a few cents per run.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from typing import Any

import httpx

from .config import get_config
from .models import Criteria, Image, Listing, VisionResult

log = logging.getLogger(__name__)

MAX_EDGE = 768  # plenty for judging a room; keeps tokens down

SYSTEM_PROMPT = """\
You evaluate photos of Swiss rental apartments for a couple who are house-hunting.
Judge only what the photographs actually show. Be sceptical: wide-angle lenses
exaggerate size, and staged shots hide wear. If the photos are too few, too dark
or too uninformative to judge, say so and score conservatively rather than
guessing.

Reply with JSON only, matching exactly this shape:
{
  "score": <integer 0-100, how well the photos match the brief>,
  "verdict": "<one sentence, max 20 words, plain and specific>",
  "condition": "<new | renovated | dated | worn | unclear>",
  "brightness": "<bright | mixed | dark | unclear>",
  "kitchen": "<short phrase, or 'not shown'>",
  "bathroom": "<short phrase, or 'not shown'>",
  "renovation_era": "<rough decade or 'unclear'>",
  "red_flags": ["<short phrase>", ...]
}"""


class VisionScorer:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        settings: Any = None,
    ) -> None:
        # The UI-editable setting outranks .env; explicit arguments outrank both.
        creds = get_config().resolve(settings)
        self.model = model or creds.openai_model
        self.api_key = api_key or creds.openai_api_key
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _openai(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self.api_key)
        return self._client

    async def score_listing(
        self,
        client: httpx.AsyncClient,
        listing: Listing,
        criteria: Criteria,
        max_photos: int = 4,
    ) -> tuple[VisionResult | None, int]:
        """Return (result, photos used). None if there was nothing to judge."""
        if not self.available:
            return None, 0

        photos = await self._download(client, listing.images[:max_photos])
        if not photos:
            return None, 0

        facts = ", ".join(
            part
            for part in (
                f"{listing.rooms:g} rooms" if listing.rooms else "",
                f"{listing.living_space_m2} m2" if listing.living_space_m2 else "",
                f"CHF {listing.price_chf}/month" if listing.price_chf else "",
                listing.city,
            )
            if part
        )
        user_text = (
            f"What this couple is looking for:\n{criteria.vision_brief}\n\n"
            f"Listing facts (for context, do not score these): {facts}\n\n"
            f"Judge the {len(photos)} attached photo(s)."
        )

        content: list[dict] = [{"type": "text", "text": user_text}]
        for photo in photos:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{photo}", "detail": "low"},
                }
            )

        try:
            response = await self._openai().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                response_format={"type": "json_object"},
                max_tokens=400,
                temperature=0.2,
            )
        except Exception as exc:
            log.warning("vision call failed for %s: %s", listing.source_id, exc)
            return None, 0

        raw = (response.choices[0].message.content or "").strip()
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("vision returned non-JSON for %s: %.120s", listing.source_id, raw)
            return None, len(photos)

        flags = data.get("red_flags") or []
        return (
            VisionResult(
                score=max(0, min(100, int(data.get("score") or 0))),
                verdict=str(data.get("verdict") or "")[:200],
                condition=str(data.get("condition") or ""),
                brightness=str(data.get("brightness") or ""),
                kitchen=str(data.get("kitchen") or ""),
                bathroom=str(data.get("bathroom") or ""),
                renovation_era=str(data.get("renovation_era") or ""),
                red_flags=[str(f)[:120] for f in flags if f][:5],
            ),
            len(photos),
        )

    async def _download(
        self, client: httpx.AsyncClient, images: list[Image]
    ) -> list[str]:
        async def one(image: Image) -> str | None:
            try:
                resp = await client.get(image.url, timeout=20.0)
                resp.raise_for_status()
                return _to_jpeg_b64(resp.content)
            except Exception as exc:
                log.debug("photo download failed (%s): %s", image.url, exc)
                return None

        results = await asyncio.gather(*(one(i) for i in images))
        return [r for r in results if r]


def _to_jpeg_b64(data: bytes) -> str | None:
    """Downscale and re-encode so we send kilobytes, not megabytes."""
    try:
        from PIL import Image as PILImage

        img = PILImage.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((MAX_EDGE, MAX_EDGE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80, optimize=True)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as exc:
        log.debug("image re-encode failed: %s", exc)
        return None
