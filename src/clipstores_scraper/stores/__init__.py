"""Store backends. Each one knows how to dump a performer's full catalog.

Adding a site = drop a new module here implementing StoreScraper and append it
to REGISTRY. Nothing else in the pipeline needs to change.
"""

from __future__ import annotations

from .apclips import APClipsStore
from .base import UA, Logger, StoreScraper, noop
from .clips4sale import Clips4SaleStore
from .goddesssnow import GoddessSnowStore
from .iwantclips import IWantClipsStore
from .loyalfans import LoyalFansStore
from .manyvids import ManyVidsStore
from .modelcentro import ModelCentroStore

REGISTRY: list[StoreScraper] = [
    IWantClipsStore(),
    ManyVidsStore(),
    Clips4SaleStore(),
    LoyalFansStore(),
    GoddessSnowStore(),
    APClipsStore(),
    # ModelCentro/AdultCentro self-hosted sites: one instance per onboarded domain.
    ModelCentroStore("brookelynnebriar.com"),
]


def for_url(url: str) -> StoreScraper | None:
    """The first registered backend that recognizes this store URL."""
    return next((store for store in REGISTRY if store.handles(url)), None)


__all__ = ["REGISTRY", "UA", "Logger", "StoreScraper", "for_url", "noop"]
