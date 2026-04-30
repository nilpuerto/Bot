"""Tests for the wallet-first Polymarket auth flow.

The contract under test:

* A signer alone (``WALLET_ADDRESS`` + ``WALLET_PRIVATE_KEY``) is enough
  to mark the bot as live-capable.  The three ``POLYMARKET_API_*`` env
  vars are *optional*; when blank, the client must derive them from the
  signer at first use via ``ClobClient.create_or_derive_api_key``.

* When the user pre-fills the three API vars, ``_ensure_clob`` must
  honour them verbatim and skip the derive step (no extra signature).

* Successive ``_ensure_clob`` calls inside the same process must reuse
  the cached client (and, when relevant, the cached derived creds).

These behaviours are mocked end-to-end against ``py_clob_client_v2`` so the
suite stays offline.
"""
from __future__ import annotations

import sys
import types
from typing import Any
import pytest

from app.config.settings import Settings
from app.integrations.polymarket_client import (
    PolymarketClient,
    PolymarketWriteDisabled,
)


# ---------------------------------------------------------------------------
# Helpers — install a fake ``py_clob_client_v2`` so we don't hit Polymarket
# ---------------------------------------------------------------------------


def _install_fake_clob_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``py_clob_client_v2.client`` and ``.clob_types`` with stubs.

    Returns the call-tracking dict so individual tests can assert on it.
    """
    calls: dict[str, Any] = {
        "init_kwargs": None,
        "set_api_creds_with": None,
        "create_or_derive_calls": 0,
        "derive_should_raise": False,
    }

    class _FakeApiCreds:
        def __init__(self, api_key: str, api_secret: str, api_passphrase: str) -> None:
            self.api_key = api_key
            self.api_secret = api_secret
            self.api_passphrase = api_passphrase

        def __repr__(self) -> str:  # noqa: DUNDER
            return f"FakeApiCreds(key={self.api_key!r})"

    class _FakeClobClient:
        def __init__(self, **kwargs: Any) -> None:
            calls["init_kwargs"] = kwargs
            self._creds: Any = None

        def set_api_creds(self, creds: Any) -> None:
            self._creds = creds
            calls["set_api_creds_with"] = creds

        def create_or_derive_api_key(self) -> Any:
            calls["create_or_derive_calls"] += 1
            if calls["derive_should_raise"]:
                raise RuntimeError("simulated derive failure")
            return _FakeApiCreds("derived-key", "derived-secret", "derived-pass")

    fake_client_module = types.ModuleType("py_clob_client_v2.client")
    fake_client_module.ClobClient = _FakeClobClient  # type: ignore[attr-defined]

    fake_types_module = types.ModuleType("py_clob_client_v2.clob_types")
    fake_types_module.ApiCreds = _FakeApiCreds  # type: ignore[attr-defined]

    fake_pkg = types.ModuleType("py_clob_client_v2")
    fake_pkg.client = fake_client_module  # type: ignore[attr-defined]
    fake_pkg.clob_types = fake_types_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "py_clob_client_v2", fake_pkg)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.client", fake_client_module)
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", fake_types_module)

    return calls


def _patch_settings(monkeypatch: pytest.MonkeyPatch, s: Settings) -> None:
    """Inject ``s`` everywhere the client could read settings from.

    ``app/config/__init__.py`` re-exports ``settings`` from
    ``app.config.settings``, so the bare name ``app.config.settings``
    in attribute lookups actually resolves to the Settings *instance*,
    not the submodule.  We grab the real module from ``sys.modules`` to
    avoid that footgun, and patch both binding sites the client touches.
    """
    cfg_module = sys.modules["app.config.settings"]
    poly_module = sys.modules["app.integrations.polymarket_client"]

    monkeypatch.setattr(poly_module, "settings", s)
    monkeypatch.setattr(cfg_module, "settings", s)


def _isolated_settings(**overrides: Any) -> Settings:
    """Construct a Settings instance independent from the real ``.env``.

    We must pin every field touched by these tests because otherwise a
    real ``.env`` (e.g. with a configured FUNDER_ADDRESS for a live
    deployment) would leak into the assertions.
    """
    base: dict[str, Any] = {
        "_env_file": None,
        "POLYMARKET_API_KEY": "",
        "POLYMARKET_API_SECRET": "",
        "POLYMARKET_API_PASSPHRASE": "",
        "POLYMARKET_SIGNATURE_TYPE": 0,
        "POLYMARKET_FUNDER_ADDRESS": "",
        "WALLET_ADDRESS": "",
        "WALLET_PRIVATE_KEY": "",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def wallet_only_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings instance with a signer but no explicit CLOB creds."""
    s = _isolated_settings(
        WALLET_ADDRESS="0x1111111111111111111111111111111111111111",
        WALLET_PRIVATE_KEY="0x" + "ab" * 32,
    )
    _patch_settings(monkeypatch, s)
    return s


@pytest.fixture
def explicit_creds_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings instance with the three CLOB vars pinned by the user."""
    s = _isolated_settings(
        WALLET_ADDRESS="0x2222222222222222222222222222222222222222",
        WALLET_PRIVATE_KEY="0x" + "cd" * 32,
        POLYMARKET_API_KEY="pinned-key",
        POLYMARKET_API_SECRET="pinned-secret",
        POLYMARKET_API_PASSPHRASE="pinned-pass",
    )
    _patch_settings(monkeypatch, s)
    return s


# ---------------------------------------------------------------------------
# Settings-level contract
# ---------------------------------------------------------------------------


def test_wallet_only_is_enough_for_write() -> None:
    s = _isolated_settings(WALLET_ADDRESS="0xaaaa", WALLET_PRIVATE_KEY="0xbbbb")
    assert s.has_polymarket_write_credentials is True
    assert s.has_explicit_clob_creds is False


def test_missing_signer_blocks_write() -> None:
    s = _isolated_settings()
    assert s.has_polymarket_write_credentials is False


def test_explicit_creds_flag_requires_all_three() -> None:
    base = dict(WALLET_ADDRESS="0xaaaa", WALLET_PRIVATE_KEY="0xbbbb")
    assert _isolated_settings(**base, POLYMARKET_API_KEY="k").has_explicit_clob_creds is False
    assert _isolated_settings(
        **base,
        POLYMARKET_API_KEY="k",
        POLYMARKET_API_SECRET="s",
        POLYMARKET_API_PASSPHRASE="p",
    ).has_explicit_clob_creds is True


def test_funder_defaults_to_signer() -> None:
    s = _isolated_settings(WALLET_ADDRESS="0xfeed", WALLET_PRIVATE_KEY="0xbeef")
    assert s.effective_funder_address == "0xfeed"


def test_funder_override_wins() -> None:
    s = _isolated_settings(
        WALLET_ADDRESS="0xfeed",
        WALLET_PRIVATE_KEY="0xbeef",
        POLYMARKET_FUNDER_ADDRESS="0xc0ffee",
    )
    assert s.effective_funder_address == "0xc0ffee"


# ---------------------------------------------------------------------------
# Client-level contract
# ---------------------------------------------------------------------------


def test_ensure_clob_derives_creds_when_env_blank(
    monkeypatch: pytest.MonkeyPatch, wallet_only_settings: Settings
) -> None:
    calls = _install_fake_clob_client(monkeypatch)

    client = PolymarketClient()._ensure_clob()

    assert client is not None
    assert calls["create_or_derive_calls"] == 1, "must derive when env is blank"
    assert calls["set_api_creds_with"] is not None
    assert calls["set_api_creds_with"].api_key == "derived-key"
    # EOA path: no signature_type / funder kwargs forwarded.
    assert "signature_type" not in calls["init_kwargs"]
    assert "funder" not in calls["init_kwargs"]


def test_ensure_clob_uses_pinned_creds_without_deriving(
    monkeypatch: pytest.MonkeyPatch, explicit_creds_settings: Settings
) -> None:
    calls = _install_fake_clob_client(monkeypatch)

    client = PolymarketClient()._ensure_clob()

    assert client is not None
    assert calls["create_or_derive_calls"] == 0, "must NOT derive when env is pinned"
    assert calls["set_api_creds_with"].api_key == "pinned-key"
    assert calls["set_api_creds_with"].api_secret == "pinned-secret"
    assert calls["set_api_creds_with"].api_passphrase == "pinned-pass"


def test_ensure_clob_caches_derived_creds_across_init(
    monkeypatch: pytest.MonkeyPatch, wallet_only_settings: Settings
) -> None:
    calls = _install_fake_clob_client(monkeypatch)

    poly = PolymarketClient()
    poly._ensure_clob()
    poly._clob = None  # simulate forced re-init within the same process
    poly._ensure_clob()

    assert calls["create_or_derive_calls"] == 1, "second init must reuse cache"


def test_ensure_clob_forwards_signature_type_and_funder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _isolated_settings(
        WALLET_ADDRESS="0x1111111111111111111111111111111111111111",
        WALLET_PRIVATE_KEY="0x" + "ab" * 32,
        POLYMARKET_SIGNATURE_TYPE=2,
        POLYMARKET_FUNDER_ADDRESS="0x9999999999999999999999999999999999999999",
    )
    _patch_settings(monkeypatch, s)
    calls = _install_fake_clob_client(monkeypatch)

    PolymarketClient()._ensure_clob()

    assert calls["init_kwargs"]["signature_type"] == 2
    assert calls["init_kwargs"]["funder"] == "0x9999999999999999999999999999999999999999"


def test_ensure_clob_raises_when_signer_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _isolated_settings()
    _patch_settings(monkeypatch, s)
    _install_fake_clob_client(monkeypatch)

    with pytest.raises(PolymarketWriteDisabled):
        PolymarketClient()._ensure_clob()


def test_ensure_clob_wraps_derive_failure(
    monkeypatch: pytest.MonkeyPatch, wallet_only_settings: Settings
) -> None:
    calls = _install_fake_clob_client(monkeypatch)
    calls["derive_should_raise"] = True

    with pytest.raises(PolymarketWriteDisabled) as ei:
        PolymarketClient()._ensure_clob()
    assert "derive" in str(ei.value).lower()
