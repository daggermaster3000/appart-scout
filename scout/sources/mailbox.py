"""Saved-search alert emails, read over IMAP.

ImmoScout24, Homegate, Newhome and Comparis all sit behind DataDome or
Cloudflare: a plain HTTP client is refused outright and even a headed Chromium
only sometimes gets through. But every one of them will happily *push* the same
listings to you as saved-search alert mail, for free, with no anti-bot layer in
front of it — and it arrives when the listing appears rather than up to six
hours later.

So this adapter treats the mailbox as the source. You create the saved searches
by hand once, point their alerts at a dedicated address, and this reads that
folder over IMAP.

What you give up is field depth. An alert email carries the link, the headline
price, rooms, surface and the town — enough for every hard filter and for the
commute lookup (`geo.py` resolves a town name when there are no coordinates) —
but no structured amenity flags, no year built, and only the one thumbnail.
Anything missing stays `None` and the scorer treats it as unknown rather than
absent. Nothing here invents a value it did not read.

Parsing is deliberately generic rather than four hand-written template parsers:
the portals restyle their mail regularly, but all of them state prices as
`CHF 2'450`, rooms as `3.5 Zimmer` and surface as `85 m²`. So we locate the
listing links (which are stable, being the entire point of the mail) and read
the fields out of the text block around each one.

Setup:

    IMAP_HOST=imap.gmail.com
    IMAP_USER=you@gmail.com
    IMAP_PASSWORD=<app password, not your account password>
    IMAP_FOLDER=Alerts          # optional, defaults to INBOX

Mail is only ever read — never deleted, moved, or marked seen.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

import httpx
from selectolax.parser import HTMLParser, Node

from ..config import get_config
from ..models import Criteria, Image, Listing, Settings
from ..normalize import clean_text, detect_amenities, to_float, to_int
from .base import Source, SourceError

log = logging.getLogger(__name__)


class Portal:
    """How to recognize one portal's mail and its listing links."""

    def __init__(self, source: str, sender_domains: tuple[str, ...], link_host: str) -> None:
        self.source = source
        self.sender_domains = sender_domains
        self.link_host = link_host


PORTALS: tuple[Portal, ...] = (
    Portal("immoscout", ("immoscout24.ch", "immostreet.ch"), "immoscout24.ch"),
    Portal("homegate", ("homegate.ch",), "homegate.ch"),
    Portal("newhome", ("newhome.ch",), "newhome.ch"),
    Portal("comparis", ("comparis.ch",), "comparis.ch"),
    Portal("flatfox", ("flatfox.ch",), "flatfox.ch"),
)

#: Query parameters that portals' click-trackers hide the real destination in.
REDIRECT_PARAMS = ("url", "u", "redirect", "redirect_url", "target", "link", "destination")

#: Swiss thousand separators: 2'450, 2’450, 2.450
_PRICE_RE = re.compile(r"(?:CHF|Fr\.?)\s*([\d'’.  ]{3,12})", re.I)
_PRICE_SUFFIX_RE = re.compile(r"([\d'’.  ]{3,12})\s*(?:CHF|Fr\.)", re.I)
_ROOMS_RE = re.compile(r"([\d]+(?:[.,]\d)?)\s*(?:Zimmer|Zi\.?|rooms?|pièces?|locali)\b", re.I)
_SPACE_RE = re.compile(r"(\d{2,4})\s*(?:m²|m2|qm)\b", re.I)
_PLACE_RE = re.compile(r"\b(\d{4})\s+([^\d,;|\n]{2,40}?)(?=\s*(?:[,;|\n]|$))")
_FLOOR_RE = re.compile(r"(\d{1,2})\.\s*(?:OG|Obergeschoss|étage|floor)\b", re.I)
#: last numeric path segment of a listing URL — every portal's stable id
_ID_RE = re.compile(r"(\d{5,})")

# A listing block that says nothing but "Wohnung" is a layout artefact, not a
# listing; require enough text to plausibly carry the fields we want.
MIN_BLOCK_CHARS = 40
MAX_BLOCK_CHARS = 1200


class MailboxSource(Source):
    name = "mailbox"
    label = "Alert emails"
    needs_browser = False
    #: IMAP is one connection, not per-request HTTP; the base throttle is moot.
    rate_limit = 0.0
    timeout = 60.0

    #: never process more than this in a single run, so a first connection to a
    #: mailbox with years of history cannot stall the run
    max_messages = 300

    async def fetch(
        self,
        client: httpx.AsyncClient,
        criteria: Criteria,
        settings: Settings,
        state: dict[str, Any],
    ) -> tuple[list[Listing], dict[str, Any]]:
        cfg = get_config().resolve(settings)
        if not cfg.has_imap:
            raise SourceError(
                "mailbox: IMAP is not configured; fill in the IMAP host, user and "
                "password on the Settings page (or IMAP_* in .env)"
            )

        # imaplib is blocking, and the rest of the pipeline is not.
        messages, new_state = await asyncio.to_thread(self._collect, cfg, state)

        listings: list[Listing] = []
        for raw in messages:
            try:
                listings.extend(parse_alert_email(raw))
            except Exception:
                log.exception("mailbox: failed to parse one message; skipping")

        log.info(
            "mailbox: %d messages -> %d listings (uid %s -> %s)",
            len(messages),
            len(listings),
            state.get("max_uid"),
            new_state.get("max_uid"),
        )
        return listings, new_state

    # -- IMAP ---------------------------------------------------------------

    def _collect(self, cfg: Any, state: dict[str, Any]) -> tuple[list[bytes], dict[str, Any]]:
        """Fetch raw messages newer than the stored cursor. Runs off-loop."""
        try:
            conn = (
                imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
                if cfg.imap_ssl
                else imaplib.IMAP4(cfg.imap_host, cfg.imap_port)
            )
        except Exception as exc:
            raise SourceError(f"mailbox: cannot reach {cfg.imap_host}: {exc}") from exc

        try:
            try:
                conn.login(cfg.imap_user, cfg.imap_password)
            except imaplib.IMAP4.error as exc:
                raise SourceError(f"mailbox: IMAP login rejected: {exc}") from exc

            # Read-only: this must never mark mail seen or touch flags.
            status, data = conn.select(f'"{cfg.imap_folder}"', readonly=True)
            if status != "OK":
                raise SourceError(
                    f"mailbox: cannot open folder {cfg.imap_folder!r}: "
                    f"{_first(data)!r}"
                )

            uidvalidity = self._uidvalidity(conn, cfg.imap_folder)
            last_uid = int(state.get("max_uid") or 0)
            # UIDs are only monotonic within one UIDVALIDITY. If the server
            # renumbers the folder, the old cursor points at unrelated messages
            # and must be abandoned rather than trusted.
            if uidvalidity and uidvalidity != state.get("uidvalidity"):
                if state.get("uidvalidity") is not None:
                    log.warning(
                        "mailbox: UIDVALIDITY changed (%s -> %s); rescanning folder",
                        state.get("uidvalidity"),
                        uidvalidity,
                    )
                last_uid = 0

            uids = self._search(conn, last_uid)
            if not uids:
                return [], {"uidvalidity": uidvalidity, "max_uid": last_uid}

            trimmed = uids[-self.max_messages :]
            if len(trimmed) < len(uids):
                log.warning(
                    "mailbox: %d unread-by-us messages, processing the newest %d; "
                    "the rest follow next run",
                    len(uids),
                    len(trimmed),
                )

            bodies: list[bytes] = []
            highest = last_uid
            for uid in trimmed:
                status, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    log.warning("mailbox: FETCH failed for uid %s", uid)
                    continue
                body = _first_payload(data)
                if body:
                    bodies.append(body)
                highest = max(highest, uid)

            # Only advance past messages we actually trimmed to, so a capped run
            # resumes rather than skips.
            return bodies, {"uidvalidity": uidvalidity, "max_uid": highest}
        finally:
            try:
                conn.logout()
            except Exception:  # already broken; nothing useful to do
                pass

    def _uidvalidity(self, conn: imaplib.IMAP4, folder: str) -> int | None:
        """SELECT already reported it as an untagged response; read that.

        Asking with STATUS instead is not portable — several servers refuse
        STATUS on the mailbox that is currently selected.
        """
        raw = _first(conn.untagged_responses.get("UIDVALIDITY"))
        if raw is None:
            status, data = conn.status(f'"{folder}"', "(UIDVALIDITY)")
            if status != "OK":
                return None
            match = re.search(rb"UIDVALIDITY\s+(\d+)", _first(data) or b"")
            return int(match.group(1)) if match else None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("ascii", errors="ignore")
        try:
            return int(str(raw).strip())
        except ValueError:
            return None

    def _search(self, conn: imaplib.IMAP4, last_uid: int) -> list[int]:
        criterion = f"{last_uid + 1}:*" if last_uid else "ALL"
        status, data = conn.uid("SEARCH", None, criterion)
        if status != "OK":
            raise SourceError(f"mailbox: IMAP SEARCH failed: {_first(data)!r}")
        raw = _first(data) or b""
        uids = [int(tok) for tok in raw.split()]
        # `N:*` is defined to return the highest UID even when it is below N, so
        # a folder with no new mail comes back with one stale UID. Filter it.
        return sorted(uid for uid in uids if uid > last_uid)


# -- parsing ----------------------------------------------------------------


def parse_alert_email(raw: bytes) -> list[Listing]:
    """Extract every listing linked from one alert email."""
    msg = email.message_from_bytes(raw)
    portal = match_portal(msg.get("From", ""))
    if portal is None:
        return []

    html = _body(msg, "html")
    text = _body(msg, "plain")
    if not html and not text:
        return []

    published = _sent_at(msg)
    if html:
        return _from_html(html, portal, published)
    return _from_text(text, portal, published)


def match_portal(sender: str) -> Portal | None:
    sender = (sender or "").lower()
    for portal in PORTALS:
        if any(domain in sender for domain in portal.sender_domains):
            return portal
    return None


def _from_html(html: str, portal: Portal, published: Any) -> list[Listing]:
    tree = HTMLParser(html)

    # One listing is usually linked several times per mail (photo, headline,
    # "details" button). Group the blocks by listing id and read the union,
    # because the price often sits next to one link and the rooms next to
    # another.
    blocks: dict[str, dict[str, Any]] = {}
    for anchor in tree.css("a[href]"):
        url = clean_url(anchor.attributes.get("href") or "", portal)
        if url is None:
            continue
        source_id = listing_id(url)
        if not source_id:
            continue

        node = _block(anchor)
        entry = blocks.setdefault(
            source_id, {"url": url, "texts": [], "titles": [], "image": None}
        )
        chunk = node.text(separator=" ", strip=True) if node else ""
        if chunk and chunk not in entry["texts"]:
            entry["texts"].append(chunk)
        # The headline is the text of the link itself, which is why the block
        # text alone is not enough: it also carries the price line and address.
        label = clean_text(anchor.text(separator=" ", strip=True))
        if label:
            entry["titles"].append(label)
        if entry["image"] is None and node is not None:
            entry["image"] = _image(node)

    out: list[Listing] = []
    for source_id, entry in blocks.items():
        listing = build_listing(
            portal=portal,
            source_id=source_id,
            url=entry["url"],
            text=" \n".join(entry["texts"]),
            image_url=entry["image"],
            published=published,
            title=_headline(entry["titles"]),
        )
        if listing is not None:
            out.append(listing)
    return out


def _from_text(text: str, portal: Portal, published: Any) -> list[Listing]:
    """Plain-text fallback: split on the listing links themselves."""
    out: list[Listing] = []
    seen: set[str] = set()
    matches = list(re.finditer(r"https?://\S+", text))
    for i, match in enumerate(matches):
        url = clean_url(match.group(0).rstrip(").,>"), portal)
        if url is None:
            continue
        source_id = listing_id(url)
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        # The text between this link and the next describes this listing.
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        start = matches[i - 1].end() if i else 0
        listing = build_listing(
            portal=portal,
            source_id=source_id,
            url=url,
            text=text[start:end],
            image_url=None,
            published=published,
        )
        if listing is not None:
            out.append(listing)
    return out


def build_listing(
    *,
    portal: Portal,
    source_id: str,
    url: str,
    text: str,
    image_url: str | None,
    published: Any,
    title: str = "",
) -> Listing | None:
    text = clean_text(text)
    if not text:
        return None

    price = _price(text)
    rooms = _rooms(text)
    space = _space(text)
    zipcode, city = _place(text)

    # A block with none of the four fields is chrome — a footer, an unsubscribe
    # link, a "more results" button — not a listing.
    if price is None and rooms is None and space is None and not city:
        return None

    return Listing(
        source=portal.source,
        source_id=source_id,
        url=url,
        title=title or _title(text),
        description=text,
        price_chf=price,
        rooms=rooms,
        living_space_m2=space,
        floor=_floor(text),
        zipcode=zipcode,
        city=city,
        category=_category(text),
        amenities=detect_amenities(text),
        images=[Image(url=image_url)] if image_url else [],
        published=published,
        raw={"via": "mailbox"},
    )


def clean_url(href: str, portal: Portal) -> str | None:
    """Unwrap click-trackers and keep only links to this portal's listings."""
    href = (href or "").strip()
    if not href or href.startswith(("mailto:", "tel:", "#")):
        return None

    url = _unwrap(href, portal, depth=0)
    if url is None:
        return None

    parsed = urlparse(url)
    if portal.link_host not in parsed.netloc.lower():
        return None
    # Unsubscribe / preferences / search-results links are not listings; they
    # have no id, so `listing_id` rejects them, but skip the obvious ones early.
    if re.search(r"unsubscribe|abmelden|preferences|einstellungen", parsed.path, re.I):
        return None

    # Drop tracking query strings and fragments so the same flat linked from two
    # mails dedups to one URL.
    return urlunparse(parsed._replace(query="", fragment=""))


def _unwrap(href: str, portal: Portal, depth: int) -> str | None:
    """Follow redirect wrappers as far as the query string reveals them."""
    if depth > 3:
        return href
    parsed = urlparse(href)
    if not parsed.scheme.startswith("http"):
        return None
    if portal.link_host in parsed.netloc.lower() and not parsed.query:
        return href

    params = parse_qs(parsed.query)
    for key in REDIRECT_PARAMS:
        for value in params.get(key, []):
            candidate = unquote(value)
            if candidate.startswith("http") and portal.link_host in candidate:
                return _unwrap(candidate, portal, depth + 1)
    return href


def listing_id(url: str) -> str | None:
    """The portal's own id, taken from the URL path.

    Every one of these portals puts a long numeric id in the listing path
    (`.../wohnung-mieten/zuerich/8046231`). Query strings are already stripped
    by `clean_url`, so a match here is a path segment, not a tracking token.
    """
    path = urlparse(url).path
    ids = _ID_RE.findall(path)
    return ids[-1] if ids else None


# -- field extraction --------------------------------------------------------


def _price(text: str) -> int | None:
    for pattern in (_PRICE_RE, _PRICE_SUFFIX_RE):
        for match in pattern.finditer(text):
            value = to_int(_digits(match.group(1)))
            # Alert mails quote monthly rent; anything outside this band is a
            # surface, a postcode or a phone number that happened to sit next
            # to a currency symbol.
            if value is not None and 200 <= value <= 20000:
                return value
    return None


def _digits(raw: str) -> str:
    return re.sub(r"[^\d]", "", raw or "")


def _rooms(text: str) -> float | None:
    match = _ROOMS_RE.search(text)
    if not match:
        return None
    rooms = to_float(match.group(1).replace(",", "."))
    return rooms if rooms is not None and 0.5 <= rooms <= 20 else None


def _space(text: str) -> int | None:
    match = _SPACE_RE.search(text)
    if not match:
        return None
    space = to_int(match.group(1))
    return space if space is not None and 10 <= space <= 1000 else None


def _floor(text: str) -> int | None:
    match = _FLOOR_RE.search(text)
    return to_int(match.group(1)) if match else None


# `normalize.map_category` maps a portal's structured enum token; an alert mail
# has no such field, so the category has to come out of the prose instead.
_SHARED_RE = re.compile(r"\bwg\b|wohngemeinschaft|wg-zimmer|zimmer in einer|colocation", re.I)
_HOUSE_RE = re.compile(
    r"einfamilienhaus|reihenhaus|doppelhaus|bauernhaus|chalet|villa|maison|\bhouse\b", re.I
)
# "3.5 Zimmer Wohnung in einem Mehrfamilienhaus" is a flat, not a house.
_NOT_HOUSE_RE = re.compile(r"mehrfamilienhaus|wohnhaus|hochhaus", re.I)
_APARTMENT_RE = re.compile(
    r"wohnung|attika|maisonette|loft|studio|dachwohnung|appartement|apartment|flat", re.I
)


def _category(text: str) -> str:
    if _SHARED_RE.search(text):
        return "SHARED"
    if _HOUSE_RE.search(text) and not _NOT_HOUSE_RE.search(text):
        return "HOUSE"
    if _APARTMENT_RE.search(text):
        return "APARTMENT"
    # These mails arrive because *you* configured a saved search, so an
    # unlabelled result is far more likely a flat the portal did not name than
    # a parking space. Assume the common case rather than dropping it.
    return "APARTMENT"


def _place(text: str) -> tuple[int | None, str]:
    """Swiss postcodes are four digits followed by the town."""
    match = _PLACE_RE.search(text)
    if not match:
        return None, ""
    code = to_int(match.group(1))
    town = clean_text(match.group(2))
    if code is None or not 1000 <= code <= 9999:
        return None, ""
    return code, town


#: link text that is a button, not a headline
_CTA_RE = re.compile(
    r"^(details?|mehr|more|ansehen|view|weiter|jetzt\b|zum inserat|voir|anzeigen)\W*$",
    re.I,
)


def _headline(labels: list[str]) -> str:
    """The best of a listing's link texts.

    A listing is linked two or three times per mail: once wrapping the photo
    (empty text), once as the headline, once as a "Details" button. The longest
    non-button label is the headline.
    """
    usable = [
        label for label in labels if len(label) >= 8 and not _CTA_RE.match(label)
    ]
    return max(usable, key=len)[:200] if usable else ""


def _title(text: str) -> str:
    """First meaningful line; alert mails lead with the headline."""
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 8:
            return line[:200]
    return text[:200]


# -- email plumbing ----------------------------------------------------------


def _body(msg: Message, subtype: str) -> str:
    """Concatenate every `text/<subtype>` part, decoded to str."""
    chunks: list[str] = []
    for part in msg.walk():
        if part.get_content_type() != f"text/{subtype}":
            continue
        if "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            chunks.append(payload.decode(charset, errors="replace"))
        except LookupError:  # charset the stdlib does not know
            chunks.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _sent_at(msg: Message) -> Any:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def subject(msg: Message) -> str:
    try:
        return str(make_header(decode_header(msg.get("Subject", ""))))
    except Exception:
        return msg.get("Subject", "")


def _block(anchor: Node) -> Node | None:
    """The smallest ancestor of `anchor` that carries the listing's text.

    Alert mails are nested tables; the anchor itself is often just an image or
    the word "Details". Walk outward until there is enough text to read fields
    from, but stop before the ancestor that contains *every* listing in the
    mail — that would attribute the first price to all of them.
    """
    node: Node | None = anchor
    fallback: Node | None = None
    for _ in range(8):
        if node is None:
            break
        length = len(node.text(separator=" ", strip=True))
        if MIN_BLOCK_CHARS <= length <= MAX_BLOCK_CHARS:
            return node
        if length > MAX_BLOCK_CHARS:
            # Overshot: everything above is bigger still.
            return fallback or node
        fallback = node
        node = node.parent
    return fallback


def _image(node: Node) -> str | None:
    for img in node.css("img"):
        src = (img.attributes.get("src") or "").strip()
        if not src.startswith("http"):
            continue
        # Open/click tracking pixels, spacers and logos are not the flat.
        if re.search(r"pixel|spacer|track|logo|icon|1x1", src, re.I):
            continue
        if _tiny(img):
            continue
        return src
    return None


def _tiny(img: Node) -> bool:
    for attr in ("width", "height"):
        value = to_int(img.attributes.get(attr))
        if value is not None and value <= 20:
            return True
    return False


def _first(data: Any) -> Any:
    if isinstance(data, list) and data:
        return data[0]
    return data


def _first_payload(data: Any) -> bytes | None:
    """imaplib returns FETCH results as a list of tuples and bare bytes."""
    if not isinstance(data, list):
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            return bytes(item[1])
    return None
