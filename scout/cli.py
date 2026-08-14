"""Command line interface.

Every stage of the pipeline is runnable on its own, so you can verify a change
without waiting for a scheduled digest.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import store
from .config import get_config
from .db import connect, init_db, load_criteria, load_settings, save_criteria, save_settings

app = typer.Typer(add_completion=False, help="Appart-Scout: flat hunting between Zurich and Basel.")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    _setup_logging(verbose)
    init_db()


@app.command()
def init() -> None:
    """Create the database and write the default criteria."""
    init_db()
    with connect() as conn:
        criteria = load_criteria(conn)
        save_criteria(conn, criteria)
        save_settings(conn, load_settings(conn))
    config = get_config()
    console.print(f"[green]database ready[/] at {config.db_path}")
    console.print(f"  OpenAI configured: {'yes' if config.has_openai else 'no'}")
    console.print(f"  SMTP configured:   {'yes' if config.has_smtp else 'no'}")


@app.command()
def fetch(
    source: str = typer.Option(..., "--source", "-s", help="flatfox, immoscout, ..."),
    limit: int = typer.Option(10, "--limit", "-n"),
    save: bool = typer.Option(False, "--save", help="persist results instead of only printing"),
) -> None:
    """Fetch one source and print what it produced. The fastest adapter check."""
    from .sources.base import make_client
    from .sources.registry import get_source

    async def go():
        adapter = get_source(source)
        with connect() as conn:
            criteria = load_criteria(conn)
            settings = load_settings(conn)

        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(make_client())
            if adapter.needs_browser:
                from .browser import browser_session

                adapter.session = await stack.enter_async_context(browser_session())
            listings, state = await adapter.fetch(client, criteria, settings, {})

        table = Table(title=f"{adapter.label}: {len(listings)} listings")
        for column in ("price", "rooms", "m2", "where", "amenities", "photos"):
            table.add_column(column)
        for listing in listings[:limit]:
            table.add_row(
                str(listing.price_chf or "—"),
                f"{listing.rooms:g}" if listing.rooms else "—",
                str(listing.living_space_m2 or "—"),
                f"{listing.zipcode or ''} {listing.city}".strip(),
                ",".join(a[:4] for a in listing.amenities[:5]),
                str(len(listing.images)),
            )
        console.print(table)
        console.print(f"cursor state: {json.dumps(state)[:200]}")

        if save and listings:
            from .dedup import merge

            with connect() as conn:
                for item in merge(listings):
                    store.upsert_listing(conn, item)
            console.print(f"[green]saved {len(listings)} listings[/]")

    asyncio.run(go())


@app.command()
def commute(address: str = typer.Argument(..., help='e.g. "5200 Brugg"')) -> None:
    """Print public-transport minutes from an address to both workplaces."""
    from .models import Listing
    from .geo import CommuteService
    from .sources.base import make_client

    async def go():
        parts = address.split(maxsplit=1)
        zipcode = int(parts[0]) if parts[0].isdigit() else None
        city = parts[1] if zipcode and len(parts) > 1 else address
        listing = Listing(
            source="cli", source_id="cli", url="", zipcode=zipcode, city=city
        )
        with connect() as conn:
            criteria = load_criteria(conn)
            async with make_client() as client:
                service = CommuteService(conn, client, criteria)
                station, walk = await service.nearest_station(listing)
                console.print(f"nearest station: [bold]{station or 'not found'}[/] (+{walk}′ walk)")
                legs = await service.commutes(listing)
        for leg, label in (("a", criteria.label_a), ("b", criteria.label_b)):
            result = legs.get(leg)
            target = criteria.workplace_a if leg == "a" else criteria.workplace_b
            console.print(
                f"  {label} → {target}: "
                + (f"[bold]{result.minutes}′[/] ({result.transfers} changes)" if result else "[red]no route[/]")
            )

    asyncio.run(go())


@app.command()
def run(
    email: bool = typer.Option(True, "--email/--no-email"),
    vision: bool = typer.Option(True, "--vision/--no-vision"),
    source: list[str] = typer.Option(None, "--source", "-s", help="limit to these sources"),
) -> None:
    """Run the full pipeline once."""
    from .pipeline import run_once

    stats = asyncio.run(
        run_once(send_email=email, use_vision=vision, only_sources=list(source) if source else None)
    )
    console.print_json(json.dumps(stats, default=str))


@app.command()
def digest(
    dry_run: bool = typer.Option(True, "--dry-run/--send"),
    out: Path = typer.Option(Path("digest.html"), "--out", "-o"),
    size: int = typer.Option(8, "--size"),
) -> None:
    """Render the digest email. Writes it to a file unless --send is given."""
    from .notify.email import EmailSender, render_digest, render_text

    with connect() as conn:
        criteria = load_criteria(conn)
        settings = load_settings(conn)
        items = store.unnotified(conn, "digest", limit=size)
        subject, html = render_digest(items, criteria)

    if dry_run:
        out.write_text(html)
        console.print(f"[green]{subject}[/]\nwrote {out} ({len(items)} listings)")
        return

    EmailSender().send(settings.recipients, subject, html, render_text(items))
    with connect() as conn:
        for item in items:
            store.mark_notified(conn, item["id"], "digest", item["score"])
    console.print(f"[green]sent to {', '.join(settings.recipients)}[/]")


@app.command()
def probe(
    source: str = typer.Argument(..., help="homegate, newhome, comparis, ..."),
    out_dir: Path = typer.Option(Path("probe"), "--out", "-o"),
) -> None:
    """Dump what a portal actually returns, for finishing or fixing an adapter.

    Writes the rendered HTML and any hydration JSON it can find, so you can see
    the real payload shape instead of guessing at it.
    """
    from .browser import browser_session, extract_state
    from .sources.registry import get_source

    async def go():
        adapter = get_source(source)
        with connect() as conn:
            criteria = load_criteria(conn)
        urls = (
            adapter.search_urls(criteria)
            if hasattr(adapter, "search_urls")
            else [f"https://www.{source}.ch/"]
        )
        out_dir.mkdir(parents=True, exist_ok=True)

        async with browser_session() as session:
            for i, url in enumerate(urls[:2]):
                console.print(f"loading {url}")
                try:
                    # Dump blocked responses too - seeing the interstitial is
                    # exactly what you need when diagnosing one.
                    html = await session.load(url, wait_ms=12000, raise_on_block=False)
                except Exception as exc:
                    console.print(f"  [red]{type(exc).__name__}: {exc}[/]")
                    continue
                from .browser import is_blocked

                if is_blocked(html):
                    console.print("  [yellow]served an anti-bot interstitial[/]")
                html_path = out_dir / f"{source}_{i}.html"
                html_path.write_text(html)
                console.print(f"  wrote {html_path} ({len(html)} bytes)")
                state = extract_state(html)
                if state:
                    state_path = out_dir / f"{source}_{i}_state.json"
                    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1))
                    console.print(f"  wrote {state_path} (top keys: {list(state)[:10]})")
                else:
                    console.print("  [yellow]no hydration state found[/]")

    asyncio.run(go())


@app.command()
def top(limit: int = typer.Option(15, "--limit", "-n")) -> None:
    """Print the current ranking."""
    with connect() as conn:
        items = store.ranked(conn, limit=limit)
    table = Table(title=f"top {len(items)}")
    for column in ("score", "price", "rooms", "m2", "where", "A", "B", "why"):
        table.add_column(column)
    for item in items:
        listing = item["listing"]
        table.add_row(
            f"{item['score']:.0f}",
            str(listing.price_chf or "—"),
            f"{listing.rooms:g}" if listing.rooms else "—",
            str(listing.living_space_m2 or "—"),
            f"{listing.zipcode or ''} {listing.city}".strip(),
            str(item.get("commute_a") or "—"),
            str(item.get("commute_b") or "—"),
            (item["reasons"][0] if item["reasons"] else "")[:44],
        )
    console.print(table)


@app.command()
def serve(
    host: str = typer.Option("", "--host"),
    port: int = typer.Option(0, "--port"),
) -> None:
    """Start the web UI and the background scheduler."""
    import uvicorn

    config = get_config()
    uvicorn.run(
        "scout.web.app:app",
        host=host or config.scout_host,
        port=port or config.scout_port,
        log_level="info",
    )


if __name__ == "__main__":
    app()
