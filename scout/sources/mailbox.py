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
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

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

#: Thousand separators seen in real mail: 2'450, 2’450, 2.450 and the
#: English-locale 2,500 that Homegate sends.
_PRICE_RE = re.compile(r"(?:CHF|Fr\.?)\s*([\d'’.,  ]{3,12})", re.I)
_PRICE_SUFFIX_RE = re.compile(r"([\d'’.,  ]{3,12})\s*(?:CHF|Fr\.)", re.I)
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

        # Homegate's SendGrid variant hides every listing URL behind an opaque
        # tracker; one redirect hop per link recovers the real one.
        listings = await resolve_pending(client, listings)

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
                    # Stop rather than skip: UIDs are processed in ascending
                    # order and the cursor is a high-water mark, so advancing
                    # past a failed fetch would silently lose that message
                    # forever. Leave it (and everything after it) for the next
                    # run, when the transient error has hopefully cleared.
                    log.warning("mailbox: FETCH failed for uid %s; retrying next run", uid)
                    break
                body = _first_payload(data)
                if body:
                    bodies.append(body)
                highest = max(highest, uid)

            # `highest` only covers messages actually fetched, so a capped or
            # interrupted run resumes rather than skips.
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
        # The UID prefix inside the criterion is not redundant: in `UID SEARCH`
        # a bare `N:*` is a *sequence-number* set, and Gmail answers it with
        # just the newest message. Measured, not theoretical.
        criterion = f"UID {last_uid + 1}:*" if last_uid else "ALL"
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
    """Extract every listing linked from one alert email.

    HTML and plain text are parsed *and merged*, not either/or. Real Homegate
    mail needs both: one variant wraps every HTML link in an undecodable
    tracker while the plain-text part carries the naked listing URL. A listing
    found in both parts keeps the HTML version (it has the thumbnail).

    Listings whose only link is an opaque redirect (SendGrid's `/ls/click`)
    come back with `raw["resolve_url"]` set and a placeholder `source_id`; the
    caller must resolve those over HTTP (`resolve_pending`) or drop them.
    """
    msg = email.message_from_bytes(raw)
    portal = match_portal(msg.get("From", ""))
    if portal is None:
        return []

    html = _body(msg, "html")
    text = _body(msg, "plain")
    if not html and not text:
        return []

    published = _sent_at(msg)
    merged: dict[str, Listing] = {}
    if text:
        for listing in _from_text(text, portal, published):
            merged[listing.source_id] = listing
    if html:
        for listing in _from_html(html, portal, published):
            merged[listing.source_id] = listing
    return list(merged.values())


def match_portal(sender: str) -> Portal | None:
    sender = (sender or "").lower()
    for portal in PORTALS:
        if any(domain in sender for domain in portal.sender_domains):
            return portal
    return None


#: Click-trackers whose destination is an opaque token, recoverable only by
#: following the redirect. Seen live: Homegate via SendGrid.
_OPAQUE_REDIRECTOR_RE = re.compile(r"https?://[^/]*\.?(sendgrid\.net)/", re.I)


def opaque_redirect(href: str) -> str | None:
    """The href if it is a follow-me-to-find-out tracker, else None.

    Never returns anything smelling of unsubscribe: following such a link
    would cancel the very alert this source lives on.
    """
    href = (href or "").strip()
    if not _OPAQUE_REDIRECTOR_RE.match(href):
        return None
    if re.search(r"unsubscribe|/wf/|abmelden", href, re.I):
        return None
    return href


def pending_id(wrapper_url: str) -> str:
    """Stable placeholder id for a listing awaiting URL resolution."""
    import hashlib

    return "pending-" + hashlib.sha1(wrapper_url.encode()).hexdigest()[:12]


def _from_html(html: str, portal: Portal, published: Any) -> list[Listing]:
    tree = HTMLParser(html)

    # One listing is usually linked several times per mail (photo, headline,
    # "details" button). Group the blocks by listing id and read the union,
    # because the price often sits next to one link and the rooms next to
    # another.
    blocks: dict[str, dict[str, Any]] = {}
    for anchor in tree.css("a[href]"):
        href = anchor.attributes.get("href") or ""
        url = clean_url(href, portal)
        if url is None:
            # Opaque tracker: fields are parseable now, the URL only after an
            # HTTP hop. Emit a pending listing keyed on the wrapper itself.
            wrapper = opaque_redirect(href)
            if wrapper is None:
                continue
            url, source_id = wrapper, pending_id(wrapper)
        else:
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
            if source_id.startswith("pending-"):
                listing.raw["resolve_url"] = entry["url"]
            out.append(listing)
    return out


def _from_text(text: str, portal: Portal, published: Any) -> list[Listing]:
    """Plain-text fallback: split on the listing links themselves."""
    out: list[Listing] = []
    seen: set[str] = set()
    matches = list(re.finditer(r"https?://\S+", text))
    for i, match in enumerate(matches):
        href = match.group(0).rstrip(").,>")
        url = clean_url(href, portal)
        if url is None:
            wrapper = opaque_redirect(href)
            if wrapper is None:
                continue
            url, source_id = wrapper, pending_id(wrapper)
        else:
            source_id = listing_id(url)
            if not source_id:
                continue
        if source_id in seen:
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
            if source_id.startswith("pending-"):
                listing.raw["resolve_url"] = url
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
    if not city and title:
        # Homegate's single-listing mails put "Hauptstrasse 94 4450 Sissach"
        # in the headline while the body text runs the town straight into the
        # price line, where the place regex cannot end the match.
        zipcode, city = _place(title)

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


async def resolve_pending(
    client: httpx.AsyncClient, listings: list[Listing]
) -> list[Listing]:
    """Turn opaque-tracker listings into real ones by following the redirect.

    Only the Location headers are read — the loop stops as soon as the chain
    lands on the portal's own host, so the portal page itself (and its
    anti-bot layer) is never fetched. A chain that never reaches the portal,
    or reaches it without a listing id (logo and footer links), drops the
    entry rather than storing a guess.
    """
    out: list[Listing] = []
    for listing in listings:
        wrapper = listing.raw.get("resolve_url") if listing.raw else None
        if not wrapper:
            out.append(listing)
            continue

        portal = next((p for p in PORTALS if p.source == listing.source), None)
        resolved = await _follow(client, wrapper, portal) if portal else None
        final = clean_url(resolved, portal) if resolved else None
        source_id = listing_id(final) if final else None
        if not final or not source_id:
            log.debug("mailbox: dropped unresolvable link %.100s", wrapper)
            continue
        listing.url = final
        listing.source_id = source_id
        listing.raw.pop("resolve_url", None)
        out.append(listing)

    # Two wrappers can resolve to the same flat (headline + photo links).
    unique: dict[str, Listing] = {}
    for listing in out:
        unique.setdefault(f"{listing.source}:{listing.source_id}", listing)
    return list(unique.values())


async def _follow(
    client: httpx.AsyncClient, url: str, portal: Portal, hops: int = 5
) -> str | None:
    for _ in range(hops):
        try:
            resp = await client.get(url, follow_redirects=False, timeout=15.0)
        except httpx.HTTPError as exc:
            log.debug("mailbox: redirect hop failed for %.80s: %s", url, exc)
            return None
        location = resp.headers.get("location")
        if resp.status_code not in (301, 302, 303, 307, 308) or not location:
            return None
        url = urljoin(url, location)
        netloc = urlparse(url).netloc.lower()
        if portal.link_host in netloc and not netloc.startswith(_TRACKING_SUBDOMAINS):
            return url
    return None


def clean_url(href: str, portal: Portal) -> str | None:
    """Unwrap click-trackers and keep only links to this portal's listings."""
    href = (href or "").strip()
    if not href or href.startswith(("mailto:", "tel:", "#")):
        return None

    url = _unwrap(href, portal, depth=0)
    if url is None:
        return None

    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if portal.link_host not in netloc:
        return None
    # Still on a tracking subdomain means `_unwrap` found no embedded
    # destination. Whatever digits its path carries are tracking tokens, not a
    # listing id - treating them as one is how a homepage logo link once became
    # a "listing" in the database.
    if netloc.startswith(_TRACKING_SUBDOMAINS):
        return None
    # Unsubscribe / preferences / search-results links are not listings; they
    # have no id, so `listing_id` rejects them, but skip the obvious ones early.
    if re.search(r"unsubscribe|abmelden|preferences|einstellungen", parsed.path, re.I):
        return None

    # Drop tracking query strings and fragments so the same flat linked from two
    # mails dedups to one URL.
    return urlunparse(parsed._replace(query="", fragment=""))


#: An embedded absolute URL, found after percent-decoding a wrapper's path.
#: Real example: tracking.notification.homegate.ch/CL0/https:%2F%2Fwww.homegate.ch%2F…/1/…
_EMBEDDED_URL_RE = re.compile(r"https?://[^\s\"'<>]+")

#: Subdomains that are click-trackers even though they sit under the portal's
#: own domain. Never a listing themselves; only ever a wrapper around one.
_TRACKING_SUBDOMAINS = ("tracking.", "click.", "links.", "mailing.", "email.", "mandrillapp.")


def _unwrap(href: str, portal: Portal, depth: int) -> str | None:
    """Follow redirect wrappers as far as the URL itself reveals them.

    Two wrapper styles seen in real portal mail:
    * destination in a query parameter (`?url=https%3A%2F%2F…`);
    * destination percent-encoded into the *path*
      (`/CL0/https:%2F%2Fwww.homegate.ch%2F…/1/0107…`).
    A third (SendGrid's `/ls/click?upn=<opaque>`) encodes nothing recoverable
    and is handled later by actually following the redirect over HTTP.
    """
    if depth > 3:
        return href
    parsed = urlparse(href)
    if not parsed.scheme.startswith("http"):
        return None
    netloc = parsed.netloc.lower()
    if (
        portal.link_host in netloc
        and not netloc.startswith(_TRACKING_SUBDOMAINS)
        and not parsed.query
    ):
        return href

    params = parse_qs(parsed.query)
    for key in REDIRECT_PARAMS:
        for value in params.get(key, []):
            candidate = unquote(value)
            if candidate.startswith("http") and portal.link_host in candidate:
                return _unwrap(candidate, portal, depth + 1)

    # Path-embedded style: decode and look for a whole URL inside.
    for candidate in _EMBEDDED_URL_RE.findall(unquote(parsed.path)):
        if portal.link_host in candidate:
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

#: mail boilerplate that wraps a listing link but describes the mail, not the
#: flat ("Here are 2 new properties that meet your search criteria.")
_BOILERPLATE_RE = re.compile(
    r"new propert|search criteria|suchabo|suchauftrag|treffer für|critères de recherche",
    re.I,
)


def _headline(labels: list[str]) -> str:
    """The best of a listing's link texts.

    A listing is linked two or three times per mail: once wrapping the photo
    (empty text), once as the headline, once as a "Details" button. Prefer the
    longest label that is neither a button nor mail boilerplate; fall back to
    boilerplate only when nothing better exists (it still beats an empty title).
    """
    usable = [
        label
        for label in labels
        if len(label) >= 8
        and not _CTA_RE.match(label)
        and not _BOILERPLATE_RE.search(label)
    ]
    # No real headline is better than a fake one: the caller falls back to the
    # first line of the block text, which at least describes the flat.
    return max(usable, key=len)[:200] if usable else ""


def _title(text: str) -> str:
    """First meaningful line; alert mails lead with the headline."""
    for line in text.splitlines():
        line = line.strip()
        if _BOILERPLATE_RE.search(line):
            continue
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
            # Overshot: everything above is bigger still. If even the anchor
            # itself is oversized there is no usable block at all — returning
            # the oversized node would let one mega-block's first price answer
            # for every listing linked from it.
            return fallback
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
