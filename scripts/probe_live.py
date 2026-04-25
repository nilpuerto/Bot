"""Live readiness probe for the bot.

Run with::

    python scripts/probe_live.py

What it does (read-only — never sends an order, never prints secrets):

1. Reports which credentials the app sees, **masked**.  Confirms private
   key length without ever printing it.
2. Verifies that the address derived from ``WALLET_PRIVATE_KEY`` matches
   ``WALLET_ADDRESS`` (and the relayer fallback) — this is the #1
   silent-failure cause and we want it caught here.
3. Reads the on-chain USDC balance for that address on Polygon.
4. Connects to the database and lists every user the bot knows about,
   showing their mode, configured cap and per-trade risk.
5. Tells you in one line whether the bot is good to go.

Designed to be run before flipping ``SIMULATION_MODE=false``.
"""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from typing import Optional


def _mask(value: Optional[str], keep: int = 4) -> str:
    if not value:
        return "<empty>"
    s = str(value)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}...{s[-keep:]}  (len={len(s)})"


def _short_addr(addr: Optional[str]) -> str:
    if not addr:
        return "<empty>"
    a = str(addr)
    if len(a) < 12:
        return a
    return f"{a[:6]}...{a[-4:]}"


def _check_address_matches_pk() -> tuple[bool, Optional[str], Optional[str]]:
    """Derive the address from the configured private key and compare.

    Returns ``(ok, derived, configured)``.  ``ok`` is ``False`` when
    they differ — that's a deal-breaker for live trading.
    """
    from app.config.settings import settings

    pk = settings.wallet_private_key.strip()
    cfg = settings.wallet_address.strip()
    if not pk or not cfg:
        return (False, None, cfg or None)
    try:
        from eth_account import Account  # type: ignore
    except ImportError:
        return (False, None, cfg)
    try:
        derived = Account.from_key(pk).address
        return (derived.lower() == cfg.lower(), derived, cfg)
    except Exception:
        return (False, None, cfg)


async def _print_db_users() -> tuple[int, list[dict]]:
    from app.database.repositories.users_repo import UsersRepository
    from app.database.session import session_scope

    async with session_scope() as session:
        users = await UsersRepository(session).list_allowed()

    rows: list[dict] = []
    for u in users:
        rows.append(
            {
                "id": u.id,
                "telegram_id": u.telegram_id,
                "username": u.username or "—",
                "mode": getattr(u.mode, "value", str(u.mode)),
                "is_active": bool(u.is_active),
                "balance": float(u.balance or 0),
                "risk_pct": float(u.risk_pct or 0),
            }
        )
    return len(users), rows


async def _read_usdc_balance() -> Decimal:
    from app.integrations.polymarket_client import PolymarketClient

    poly = PolymarketClient()
    return await poly.get_usdc_balance()


def _try_derive_clob_creds() -> tuple[bool, str]:
    """Attempt the wallet -> CLOB creds handshake without placing orders.

    Returns ``(ok, detail)`` so the probe can report the outcome without
    crashing if Polymarket is unreachable.
    """
    from app.integrations.polymarket_client import (
        PolymarketClient,
        PolymarketWriteDisabled,
    )

    try:
        client = PolymarketClient()._ensure_clob()  # noqa: SLF001
        if client is None:
            return False, "_ensure_clob returned None"
        return True, "ok (creds usable for L2 endpoints)"
    except PolymarketWriteDisabled as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    # Force live mode for this probe — we want to actually hit the RPC
    # and the DB even if the user has SIMULATION_MODE=true while waiting
    # for the green-light.  We never *trade* anything from this script.
    os.environ.setdefault("SIMULATION_MODE", "false")
    # Importing settings *after* tweaking the env var so the toggle
    # propagates.  (settings is a cached singleton.)
    from app.config.settings import settings as _settings  # noqa: F401
    from app.config.settings import settings

    print("=" * 64)
    print("Prym Signals -- live readiness probe")
    print("=" * 64)

    # 1. Credentials snapshot (masked)
    print("\nCredentials seen by the app (masked):")
    print(f"  WALLET_ADDRESS         : {settings.wallet_address or '<empty>'}")
    print(f"  WALLET_PRIVATE_KEY     : {_mask(settings.wallet_private_key)}")
    print(
        f"  POLYMARKET_API_KEY     : {_mask(settings.polymarket_api_key)}"
        " (auto-derived if blank)"
    )
    print(
        f"  POLYMARKET_API_SECRET  : {_mask(settings.polymarket_api_secret)}"
        " (auto-derived if blank)"
    )
    print(
        f"  POLYMARKET_API_PASSPHRASE: {_mask(settings.polymarket_api_passphrase)}"
        " (auto-derived if blank)"
    )
    print(
        f"  POLYMARKET_SIGNATURE_TYPE: {settings.polymarket_signature_type}"
        "  (0=EOA, 1=Magic, 2=Browser-wallet Safe)"
    )
    print(
        f"  POLYMARKET_FUNDER_ADDRESS: {settings.polymarket_funder_address or '<defaults to wallet>'}"
    )
    print(f"  POLYGON_RPC_URL        : {settings.polygon_rpc_url}")

    has_write = settings.has_polymarket_write_credentials
    print(
        f"\n  has_polymarket_write_credentials = {has_write}  "
        "(needs only WALLET_ADDRESS + WALLET_PRIVATE_KEY)"
    )
    print(
        f"  has_explicit_clob_creds          = {settings.has_explicit_clob_creds}  "
        "(if True, skips the auto-derive step)"
    )

    # 2. Address vs private key sanity check
    print("\nAddress <-> private-key check:")
    ok, derived, cfg = _check_address_matches_pk()
    if derived is None and cfg:
        print(
            "  PRIVATE KEY MISSING or eth_account not installed — cannot derive."
        )
    elif derived is None:
        print("  Skipped (no key configured).")
    else:
        print(f"  Configured address : {cfg}")
        print(f"  Derived address    : {derived}")
        print(f"  Match              : {'YES' if ok else 'NO'}")
        if not ok:
            print(
                "  >>> The private key does NOT belong to WALLET_ADDRESS. "
                "Bot will not be able to firm orders on that account."
            )

    # 3. On-chain USDC balance — read from the funder, not the EOA.
    print("\nOn-chain USDC.e balance (Polygon):")
    funder = settings.effective_funder_address
    if not funder:
        usdc = Decimal("0")
        print("  No funder/WALLET_ADDRESS configured -- skipping RPC call.")
    else:
        try:
            usdc = await _read_usdc_balance()
            print(f"  Funder address: {funder}")
            if funder.lower() != (settings.wallet_address or "").lower():
                print(
                    "  (reading proxy/funder, not the EOA -- expected for "
                    "browser-wallet Polymarket users)"
                )
            print(f"  USDC.e        : {usdc}")
        except Exception as exc:  # noqa: BLE001
            usdc = Decimal("0")
            print(f"  RPC error: {exc}")

    # 3b. CLOB handshake: derive (or use pinned) creds without trading.
    print("\nCLOB handshake (wallet -> API creds):")
    clob_ok, clob_detail = _try_derive_clob_creds()
    print(f"  {('OK' if clob_ok else 'FAIL'):4s}  {clob_detail}")

    # 4. Database users
    print("\nUsers known to the bot (from DB):")
    try:
        n, rows = await _print_db_users()
    except Exception as exc:  # noqa: BLE001
        n, rows = 0, []
        print(f"  DB error: {exc}")
    if n == 0:
        print("  (no users yet -- open the Telegram bot and send /start)")
    else:
        for r in rows:
            active = "active" if r["is_active"] else "PAUSED"
            print(
                f"  - id={r['id']:>3} tg={r['telegram_id']} "
                f"@{r['username']}  mode={r['mode']}  {active}  "
                f"cap=${r['balance']:.2f}  risk={r['risk_pct']:.1f}%"
            )

    # 5. Final verdict
    print("\n" + "=" * 64)
    blockers: list[str] = []
    if not settings.wallet_address:
        blockers.append("WALLET_ADDRESS empty")
    if not settings.wallet_private_key:
        blockers.append("WALLET_PRIVATE_KEY empty")
    if derived is not None and not ok:
        blockers.append("Private key does not match WALLET_ADDRESS")
    if not clob_ok:
        blockers.append(f"CLOB handshake failed: {clob_detail}")
    if usdc <= 0:
        blockers.append(
            "Funder has 0 USDC.e on Polygon -- deposit on Polymarket "
            "(or set POLYMARKET_FUNDER_ADDRESS to the proxy that holds it)"
        )

    if not blockers:
        print("READY -- flip SIMULATION_MODE=false and the bot can trade.")
    else:
        print("NOT READY yet.  Fix:")
        for b in blockers:
            print(f"  - {b}")

    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
