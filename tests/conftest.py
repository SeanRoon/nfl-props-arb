import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def nfl_event() -> dict:
    """One real Polymarket US NFL game event, trimmed to our target markets."""
    return _load("pm_nfl_event.json")


@pytest.fixture
def pm_book() -> dict:
    """A real order book response."""
    return _load("pm_book.json")
