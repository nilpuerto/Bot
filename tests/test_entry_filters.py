"""Unit tests for :mod:`app.services.entry_filters`."""
from __future__ import annotations

import pytest

from app.services.entry_filters import entry_token_gate_fail_reason


def test_rejects_above_default_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.entry_filters.settings.entry_max_price", 0.15)
    monkeypatch.setattr("app.services.entry_filters.settings.entry_min_price", 0.03)
    monkeypatch.setattr("app.services.entry_filters.settings.min_implied_prob", None)
    monkeypatch.setattr("app.services.entry_filters.settings.max_implied_prob", None)

    assert entry_token_gate_fail_reason(0.92) == "above_entry_max"
    assert entry_token_gate_fail_reason(0.12) is None


def test_implied_prob_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.entry_filters.settings.entry_max_price", 0.99)
    monkeypatch.setattr("app.services.entry_filters.settings.entry_min_price", 0.01)
    monkeypatch.setattr(
        "app.services.entry_filters.settings.min_implied_prob", 0.05
    )
    monkeypatch.setattr(
        "app.services.entry_filters.settings.max_implied_prob", 0.85
    )

    assert entry_token_gate_fail_reason(0.02) == "implied_below_min"
    assert entry_token_gate_fail_reason(0.90) == "implied_above_max"
    assert entry_token_gate_fail_reason(0.10) is None


def test_strategy_override_bands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.entry_filters.settings.entry_max_price", 0.15)
    monkeypatch.setattr("app.services.entry_filters.settings.entry_min_price", 0.03)

    assert (
        entry_token_gate_fail_reason(
            0.30,
            entry_min_override=0.05,
            entry_max_override=0.35,
        )
        is None
    )

