"""Active strategy selector.

Centralises the choice of strategy so the orchestrator, the Telegram
handlers, and tests all go through a single entry point.  Swap the
line below to change strategies globally.
"""
from app.strategies.prym_strategy import PrymStrategy


def default_strategy() -> PrymStrategy:
    return PrymStrategy()
