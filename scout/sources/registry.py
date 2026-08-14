"""Source registry.

Adapters are registered lazily by module path so that an adapter which fails to
import (a missing optional dependency, a syntax error after a hasty fix on the
Pi) degrades to "this one source is unavailable" rather than taking the whole
service down.
"""

from __future__ import annotations

import importlib
import logging

from .base import Source

log = logging.getLogger(__name__)

#: name -> "module:ClassName"
SOURCES: dict[str, str] = {
    "flatfox": "scout.sources.flatfox:FlatfoxSource",
    "homegate": "scout.sources.homegate:HomegateSource",
    "immoscout": "scout.sources.immoscout:ImmoScout24Source",
    "newhome": "scout.sources.newhome:NewhomeSource",
    "comparis": "scout.sources.comparis:ComparisSource",
}


def get_source(name: str) -> Source:
    try:
        path = SOURCES[name]
    except KeyError:
        raise KeyError(f"unknown source {name!r}; known: {', '.join(SOURCES)}") from None
    module_name, class_name = path.split(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()


def load_sources(names: list[str]) -> list[Source]:
    """Instantiate the named sources, skipping (and logging) any that fail."""
    loaded: list[Source] = []
    for name in names:
        try:
            loaded.append(get_source(name))
        except Exception:
            log.exception("source %s failed to load; skipping", name)
    return loaded
