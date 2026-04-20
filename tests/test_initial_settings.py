from __future__ import annotations

from polymarket_ai_agent.config import EDITABLE_SETTINGS_METADATA
from polymarket_ai_agent.initial_settings import INITIAL_SETTINGS_BASELINE


def test_baseline_keyset_matches_editable_metadata() -> None:
    """Every editable field must have a starting value in the baseline and
    vice versa. Drift here causes silent seeding gaps — the API shows the
    field as tunable but the DB never has a row for it, so overrides land
    but effective state keeps reading the pydantic default.
    """
    assert set(INITIAL_SETTINGS_BASELINE.keys()) == set(EDITABLE_SETTINGS_METADATA.keys())
