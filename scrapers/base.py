from __future__ import annotations

from typing import List, Type

from core.models import Show

_REGISTRY: list = []


class Scraper:
    """Base class. One subclass per site.

    A subclass must:
      - set a unique `name` (used as the `source` on every Show)
      - implement `fetch()` returning a list of Show objects
    Add the new module to scrapers/__init__.py so it registers itself.
    """

    name: str = "base"

    def fetch(self) -> List[Show]:
        raise NotImplementedError


def register(cls: Type[Scraper]) -> Type[Scraper]:
    _REGISTRY.append(cls)
    return cls


def all_scrapers() -> List[Scraper]:
    return [cls() for cls in _REGISTRY]
