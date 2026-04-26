"""Unit tests for the deterministic match gates."""
from __future__ import annotations

from app.services.match_gates import (
    categories_compatible,
    infer_market_topic,
    normalize_entities,
    passes_entity_gate,
)


# ---- infer_market_topic ----------------------------------------------------


def test_infer_topic_recognises_sports() -> None:
    assert infer_market_topic("Will USA win the 2026 FIFA World Cup?") == "sports"
    assert infer_market_topic("Lakers vs Warriors NBA finals 2026?") == "sports"
    assert infer_market_topic("London Marathon Women's Winner") == "sports"


def test_infer_topic_recognises_crypto() -> None:
    assert infer_market_topic("Will Bitcoin reach $100k by 2026?") == "crypto"
    assert infer_market_topic("ETH ETF approval before June?") == "crypto"


def test_infer_topic_recognises_political() -> None:
    assert (
        infer_market_topic("Will Trump win the 2028 presidential election?")
        == "political"
    )


def test_infer_topic_recognises_economic() -> None:
    assert (
        infer_market_topic("Will the Fed cut rates at the next FOMC meeting?")
        == "economic"
    )


def test_infer_topic_recognises_geopolitical() -> None:
    assert (
        infer_market_topic("Will Russia and Ukraine sign a ceasefire by July?")
        == "geopolitical"
    )


def test_infer_topic_returns_none_for_unclassifiable() -> None:
    assert infer_market_topic("Will the new bakery on Main Street open?") is None


# ---- categories_compatible -------------------------------------------------


def test_categories_compatible_strict_orthogonal() -> None:
    # Sports and crypto are orthogonal — never compatible.
    assert not categories_compatible("sports", "crypto")
    assert not categories_compatible("crypto", "sports")
    assert not categories_compatible("sports", "political")
    assert not categories_compatible("crypto", "political")


def test_categories_compatible_related_pass() -> None:
    # Politics ↔ macro ↔ geopolitical share enough to allow cross-matches.
    assert categories_compatible("political", "geopolitical")
    assert categories_compatible("political", "economic")
    assert categories_compatible("economic", "political")
    assert categories_compatible("geopolitical", "political")


def test_categories_compatible_unknown_is_permissive() -> None:
    # Unknown sides should NOT veto — fail-open by design.
    assert categories_compatible(None, "sports")
    assert categories_compatible("political", None)
    assert categories_compatible(None, None)


def test_categories_compatible_other_is_permissive() -> None:
    # 'other' is the catch-all bucket — allow it to pair widely.
    assert categories_compatible("other", "political")
    assert categories_compatible("other", "economic")


# ---- passes_entity_gate ----------------------------------------------------


def test_entity_gate_blocks_when_entities_present_but_no_hits() -> None:
    assert not passes_entity_gate(
        entity_hits=0,
        has_entities=True,
        jaccard=0.5,
        no_entity_jaccard_min=0.30,
        require_entity_hit=True,
    )


def test_entity_gate_passes_with_one_hit() -> None:
    assert passes_entity_gate(
        entity_hits=1,
        has_entities=True,
        jaccard=0.0,
        no_entity_jaccard_min=0.30,
        require_entity_hit=True,
    )


def test_entity_gate_can_be_disabled() -> None:
    # When the operator turns the gate off (debug only), zero hits no
    # longer reject.
    assert passes_entity_gate(
        entity_hits=0,
        has_entities=True,
        jaccard=0.0,
        no_entity_jaccard_min=0.30,
        require_entity_hit=False,
    )


def test_entity_gate_falls_back_to_jaccard_when_no_entities() -> None:
    # No entities → the Jaccard floor takes over.
    assert not passes_entity_gate(
        entity_hits=0,
        has_entities=False,
        jaccard=0.10,
        no_entity_jaccard_min=0.30,
        require_entity_hit=True,
    )
    assert passes_entity_gate(
        entity_hits=0,
        has_entities=False,
        jaccard=0.40,
        no_entity_jaccard_min=0.30,
        require_entity_hit=True,
    )


def test_normalize_entities_strips_and_lowercases() -> None:
    out = normalize_entities(["Trump", "  Federal Reserve  ", "", None])
    assert "trump" in out
    assert "federal reserve" in out
    assert "" not in out
