"""Game-specific ROM loaders. Generic SPC input uses VGMTrans's drivers."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType


_DRIVERS = (
    ("ff4", "Final Fantasy IV / Final Fantasy II US"),
    ("som", "Secret of Mana US"),
)


def supported_games() -> list[str]:
    return [game for _, game in _DRIVERS]


def find_driver(rom: bytes) -> ModuleType | None:
    """Match ROM contents; filenames and editable cartridge titles are labels."""
    for name, _ in _DRIVERS:
        driver = import_module(f"{__name__}.{name}")
        if driver.detect(rom):
            return driver
    return None
