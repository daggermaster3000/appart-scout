"""Digest email over plain SMTP.

SMTP with an app password rather than a transactional-email API: no signup, no
key to rotate, no monthly free-tier cliff, and it keeps working on a Pi behind a
home router.
"""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import get_config
from ..models import Criteria

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["chf"] = lambda v: f"{v:,}".replace(",", "'") if v else "—"
    env.filters["rooms"] = lambda v: f"{v:g}" if v else "—"
    return env


def render_digest(
    items: list[dict[str, Any]],
    criteria: Criteria,
    subject_prefix: str = "New flats",
) -> tuple[str, str]:
    """Return (subject, html)."""
    template = _env().get_template("digest.html.j2")
    html = template.render(
        items=items,
        criteria=criteria,
        generated_at=datetime.now().strftime("%d.%m.%Y %H:%M"),
    )
    if items:
        best = items[0]["score"]
        subject = f"{subject_prefix}: {len(items)} match{'es' if len(items) != 1 else ''} (best {best:.0f}/100)"
    else:
        subject = f"{subject_prefix}: nothing new"
    return subject, html


def render_text(items: list[dict[str, Any]], criteria: Criteria) -> str:
    """Plain-text alternative, for mail clients that refuse HTML.

    Takes `criteria` so the commute lines can name the two of you, the same way
    the HTML version does — "Ada 34' / Bo 41'" rather than an unlabelled pair of
    numbers whose order you have to remember.
    """
    if not items:
        return "No new listings matched your criteria."
    lines = []
    for i, item in enumerate(items, 1):
        listing = item["listing"]
        lines.append(
            f"{i}. [{item['score']:.0f}/100] {listing.city} — "
            f"CHF {listing.price_chf or '?'} — "
            f"{listing.rooms or '?'} rooms — {listing.living_space_m2 or '?'} m2"
        )
        commute = _commute_line(item, criteria)
        if commute:
            lines.append(f"   {commute}")
        for line in _vision_lines(item.get("vision")):
            lines.append(f"   {line}")
        for reason in item.get("reasons", [])[:2]:
            lines.append(f"   - {reason}")
        for source in item.get("sources", []):
            lines.append(f"   {source['source']}: {source['url']}")
        lines.append("")
    return "\n".join(lines)


def _commute_line(item: dict[str, Any], criteria: Criteria) -> str:
    legs = [
        f"{label} {item[key]}'"
        for key, label in (
            ("commute_a", criteria.label_a),
            ("commute_b", criteria.label_b),
        )
        if item.get(key) is not None
    ]
    return "commute: " + " / ".join(legs) if legs else ""


def _vision_lines(vision: dict[str, Any] | None) -> list[str]:
    """The photo evaluation, flattened for a plain-text mail."""
    if not vision:
        return []
    lines = []
    if vision.get("verdict"):
        lines.append(f"photos: {vision['verdict']}")
    details = [
        f"{name}: {vision[key]}"
        for key, name in (
            ("condition", "condition"),
            ("brightness", "light"),
            ("kitchen", "kitchen"),
            ("bathroom", "bathroom"),
            ("renovation_era", "renovated"),
        )
        if vision.get(key) and vision[key] not in ("unclear", "not shown")
    ]
    if details:
        lines.append("  " + " · ".join(details))
    for flag in vision.get("red_flags") or []:
        lines.append(f"  ! {flag}")
    return lines


class EmailSender:
    def __init__(self, config=None) -> None:
        self.config = config or get_config()

    @property
    def available(self) -> bool:
        return self.config.has_smtp

    def send(self, recipients: list[str], subject: str, html: str, text: str = "") -> None:
        if not self.available:
            raise RuntimeError(
                "SMTP is not configured; set SMTP_HOST, SMTP_USER and SMTP_PASSWORD in .env"
            )
        if not recipients:
            raise ValueError("no recipients configured")

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.config.mail_from
        message["To"] = ", ".join(recipients)
        message.set_content(text or "This digest is best viewed as HTML.")
        message.add_alternative(html, subtype="html")

        if self.config.smtp_starttls:
            with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.config.smtp_user, self.config.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP_SSL(self.config.smtp_host, self.config.smtp_port, timeout=30) as smtp:
                smtp.login(self.config.smtp_user, self.config.smtp_password)
                smtp.send_message(message)
        log.info("sent %r to %s", subject, ", ".join(recipients))
