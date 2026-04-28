"""Polymarket client — Gamma REST (read) + CLOB (write) + USDC balance.

Read side (public, no credentials):
  * Gamma API            https://gamma-api.polymarket.com
  * Data API (leaderboard, trades by user)
                         https://data-api.polymarket.com

Write side (requires funded Polygon wallet):
  * ``py-clob-client``  → signs and posts orders.
  * Auth model is **wallet-first**: the only mandatory secret is the
    ``WALLET_PRIVATE_KEY`` of the signer.  The CLOB API key + secret +
    passphrase used by L2 endpoints are auto-derived (or created) on
    first use by signing an L1 request — same flow as the official
    Polymarket SDKs.  The user can still pre-set them via env vars to
    pin a long-lived key, but it is no longer mandatory.

The client is safe by default:
  * If no signer is configured, write operations raise
    ``PolymarketWriteDisabled``.
  * ``get_usdc_balance`` reads the on-chain balance via web3 with a minimal
    ERC-20 ABI — no external dependency on custodial endpoints.  When
    Polymarket runs the user behind a Gnosis Safe proxy (browser-wallet
    flow), the bot reads the *funder* address (``POLYMARKET_FUNDER_ADDRESS``)
    instead of the EOA, since that is where the USDC actually sits.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

import httpx

from app.config.settings import settings
from app.utils.logger import get_logger


logger = get_logger(__name__)


GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
# USDC.e on Polygon (Polymarket settles in USDC.e)
POLYGON_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ERC20_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
]


class PolymarketWriteDisabled(RuntimeError):
    """Raised when a trade is attempted without the required credentials."""


@dataclass
class MarketSnapshot:
    id: str
    slug: Optional[str]
    question: str
    outcomes: list[str]
    outcome_prices: list[float]
    volume_24h: float
    liquidity: float
    best_yes_price: Optional[float]
    best_no_price: Optional[float]
    end_date: Optional[str] = None
    # CLOB token ids — required to place real orders / read the order book.
    # Polymarket encodes these as ERC-1155 token ids (big decimal strings).
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None

    def token_id_for_side(self, side: str) -> Optional[str]:
        return self.yes_token_id if side.lower() == "yes" else self.no_token_id


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    """CLOB order book for a single token id.

    Both sides are ordered best-first (bids: highest price first,
    asks: lowest price first).
    """

    token_id: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass
class LeaderboardEntry:
    wallet_address: str
    label: Optional[str]
    pnl_usd: Optional[float]
    volume_usd: Optional[float]
    roi: Optional[float]


@dataclass
class UserTrade:
    market_id: str
    market_slug: Optional[str]
    side: str  # 'yes' or 'no'
    price: Optional[float]
    size_usd: float
    tx_hash: Optional[str]
    timestamp: Optional[int]  # unix seconds


class PolymarketClient:
    """High-level facade combining all Polymarket interactions."""

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout
        self._http: Optional[httpx.AsyncClient] = None
        self._clob: Any = None  # lazy — py-clob-client
        # Cache for credentials derived from the signer.  Once we hit
        # ``create_or_derive_api_creds`` we keep the result in memory so
        # subsequent re-inits (e.g. after a process restart inside the
        # same Python session) don't burn an extra signature.
        self._derived_creds: Any = None

    # ---- lifecycle -------------------------------------------------------

    async def __aenter__(self) -> "PolymarketClient":
        self._http = httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": "PrymSignals/1.0"},
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    # ---- Gamma REST (markets) -------------------------------------------

    async def search_markets(self, query: str, limit: int = 10) -> list[MarketSnapshot]:
        """Full-text search for active markets matching ``query``."""
        assert self._http is not None
        params = {"search": query, "limit": limit, "active": "true", "closed": "false"}
        try:
            resp = await self._http.get(f"{GAMMA_BASE}/markets", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("gamma_search_error", error=str(exc))
            return []
        return [_parse_market(m) for m in resp.json() if isinstance(m, dict)]

    async def list_active_markets(
        self,
        *,
        limit: int = 200,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[MarketSnapshot]:
        """List active markets ranked by ``order`` (default: 24-h volume).

        This is the *seed* for the bot's market universe — a best-effort
        snapshot of every market currently tradeable, refreshed
        periodically by ``MarketUniverseService``.  Unlike
        :meth:`search_markets`, no text query is sent — we get the raw
        catalogue and filter / rank in Python.

        Gamma caps a single request at 100 items, so for limits above
        that we paginate transparently with ``offset``.
        """
        assert self._http is not None
        page_size = 100
        if limit <= 0:
            return []
        out: list[MarketSnapshot] = []
        offset = 0
        seen_ids: set[str] = set()
        while len(out) < limit:
            page_limit = min(page_size, limit - len(out))
            params = {
                "limit": page_limit,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": order,
                "ascending": "true" if ascending else "false",
            }
            try:
                resp = await self._http.get(f"{GAMMA_BASE}/markets", params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(
                    "gamma_list_active_error",
                    error=str(exc),
                    offset=offset,
                )
                break
            payload = resp.json()
            if not isinstance(payload, list) or not payload:
                break
            new_in_page = 0
            for raw in payload:
                if not isinstance(raw, dict):
                    continue
                market = _parse_market(raw)
                if not market.id or market.id in seen_ids:
                    continue
                seen_ids.add(market.id)
                out.append(market)
                new_in_page += 1
            if new_in_page == 0 or len(payload) < page_limit:
                break
            offset += page_limit
        return out

    async def get_market(self, market_id: str) -> Optional[MarketSnapshot]:
        assert self._http is not None
        try:
            resp = await self._http.get(f"{GAMMA_BASE}/markets/{market_id}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("gamma_get_market_error", error=str(exc), market_id=market_id)
            return None
        data = resp.json()
        if not data or not isinstance(data, dict):
            return None
        return _parse_market(data)

    # ---- Data API (leaderboard & user trades) ---------------------------

    async def get_leaderboard(self, limit: int = 50) -> list[LeaderboardEntry]:
        """Best-effort fetch of top traders.  Polymarket's data API surface
        changes periodically; we handle multiple response shapes defensively."""
        assert self._http is not None
        params = {"limit": limit, "period": "month"}
        for path in ("/leaderboard", "/traders/leaderboard"):
            try:
                resp = await self._http.get(f"{DATA_BASE}{path}", params=params)
                if resp.status_code == 200:
                    return _parse_leaderboard(resp.json())
            except httpx.HTTPError as exc:
                logger.warning("leaderboard_error", path=path, error=str(exc))
        return []

    async def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Fetch the CLOB order book for ``token_id`` (no credentials needed)."""
        assert self._http is not None
        if not token_id:
            return None
        try:
            resp = await self._http.get(
                f"{CLOB_BASE}/book", params={"token_id": token_id}
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("clob_book_error", error=str(exc), token_id=token_id)
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        return _parse_order_book(token_id, data)

    async def get_trades_for_wallet(
        self, wallet_address: str, limit: int = 50
    ) -> list[UserTrade]:
        assert self._http is not None
        params = {"user": wallet_address, "limit": limit}
        try:
            resp = await self._http.get(f"{DATA_BASE}/trades", params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "user_trades_error", wallet=wallet_address, error=str(exc)
            )
            return []
        return _parse_user_trades(resp.json())

    # ---- Balance & writes -----------------------------------------------

    async def get_usdc_balance(self, address: Optional[str] = None) -> Decimal:
        """Read on-chain USDC.e balance via web3 (synchronous under to_thread).

        ``address`` defaults to the *funder* (the address that actually
        holds collateral on Polymarket — see settings) so the number
        matches what the Polymarket UI shows.  Pure-EOA setups have
        ``funder == wallet_address`` and behave exactly as before.
        """
        addr = address or settings.effective_funder_address
        if not addr:
            return Decimal("0")
        return await asyncio.to_thread(_read_usdc_balance, addr)

    def _ensure_clob(self) -> Any:
        """Lazy-init the py-clob-client.

        Auth flow:
          1. Need a signer.  ``WALLET_PRIVATE_KEY`` is the only hard
             requirement; without it we raise ``PolymarketWriteDisabled``.
          2. If the user pinned a CLOB key via env vars (KEY+SECRET+
             PASSPHRASE), use it as-is.
          3. Otherwise spin up an L1-only client first, call
             ``create_or_derive_api_creds`` (signs an EIP-712 message
             with the wallet) to get the L2 creds, cache them, and apply
             them to the same client instance via ``set_api_creds``.
        """
        if self._clob is not None:
            return self._clob
        if not settings.has_polymarket_write_credentials:
            raise PolymarketWriteDisabled(
                "Polymarket signer missing. Required: WALLET_ADDRESS + "
                "WALLET_PRIVATE_KEY (the CLOB API key/secret/passphrase "
                "are derived automatically from the signer)."
            )
        try:
            from py_clob_client.client import ClobClient  # type: ignore
            from py_clob_client.clob_types import ApiCreds  # type: ignore
        except ImportError as exc:
            raise PolymarketWriteDisabled(
                f"py-clob-client not importable: {exc}"
            ) from exc

        sig_type = settings.polymarket_signature_type
        funder = settings.effective_funder_address or None
        # ``signature_type``/``funder`` are only meaningful when != EOA.
        # Passing them as None keeps the legacy EOA path intact for
        # users who do not need a proxy.
        client_kwargs: dict[str, Any] = {
            "host": "https://clob.polymarket.com",
            "key": settings.wallet_private_key,
            "chain_id": 137,  # Polygon
        }
        if sig_type and sig_type != 0:
            client_kwargs["signature_type"] = sig_type
            client_kwargs["funder"] = funder

        client = ClobClient(**client_kwargs)

        creds: Any
        if settings.has_explicit_clob_creds:
            creds = ApiCreds(
                api_key=settings.polymarket_api_key,
                api_secret=settings.polymarket_api_secret,
                api_passphrase=settings.polymarket_api_passphrase,
            )
            client.set_api_creds(creds)
            logger.info("clob_creds_from_env")
        elif self._derived_creds is not None:
            client.set_api_creds(self._derived_creds)
            logger.info("clob_creds_from_cache")
        else:
            try:
                creds = client.create_or_derive_api_creds()
            except Exception as exc:  # noqa: BLE001 — SDK exceptions vary
                raise PolymarketWriteDisabled(
                    f"Could not derive CLOB API creds from signer: {exc}. "
                    "Make sure the wallet matches WALLET_ADDRESS and is "
                    "registered on Polymarket."
                ) from exc
            self._derived_creds = creds
            client.set_api_creds(creds)
            logger.info("clob_creds_derived_from_signer")

        self._clob = client
        return self._clob

    async def place_order(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size_shares: float,
    ) -> dict:
        """Submit a limit order via the CLOB.  Runs the blocking call in a
        thread so it does not stall the asyncio loop."""
        client = self._ensure_clob()

        def _submit() -> dict:
            import inspect
            import json
            import time

            from py_clob_client.clob_types import OrderArgs  # type: ignore

            # #region agent debug log
            _oa_sig = str(inspect.signature(OrderArgs.__init__))
            _oa_fields = [p for p in inspect.signature(OrderArgs.__init__).parameters]
            _has_neg_risk = "neg_risk" in _oa_fields
            _has_tick_size = "tick_size" in _oa_fields
            _debug_pre = {
                "sessionId": "f71d1d", "runId": "run1",
                "hypothesisId": "H-A,H-B,H-C,H-D,H-E",
                "location": "polymarket_client.py:place_order:pre",
                "timestamp": int(time.time() * 1000),
                "message": "order_args_pre_submit",
                "data": {
                    "token_id": token_id,
                    "side": side.upper(),
                    "price": price,
                    "size_shares": size_shares,
                    "orderargs_signature": _oa_sig,
                    "has_neg_risk_field": _has_neg_risk,
                    "has_tick_size_field": _has_tick_size,
                },
            }
            try:
                with open("/root/Bot/debug-f71d1d.log", "a") as _f:
                    _f.write(json.dumps(_debug_pre) + "\n")
            except Exception:
                pass
            logger.info("debug_order_pre_submit",
                        token_id=token_id, price=price, side=side,
                        has_neg_risk_field=_has_neg_risk,
                        has_tick_size_field=_has_tick_size,
                        orderargs_sig=_oa_sig)
            # #endregion

            order_args = OrderArgs(
                token_id=token_id,
                side=side.upper(),
                price=price,
                size=size_shares,
            )

            # #region agent debug log
            _debug_args = {
                "sessionId": "f71d1d", "runId": "run1",
                "hypothesisId": "H-A,H-B",
                "location": "polymarket_client.py:place_order:order_args",
                "timestamp": int(time.time() * 1000),
                "message": "order_args_built",
                "data": {"order_args_repr": repr(order_args)},
            }
            try:
                with open("/root/Bot/debug-f71d1d.log", "a") as _f:
                    _f.write(json.dumps(_debug_args) + "\n")
            except Exception:
                pass
            logger.info("debug_order_args_built", order_args_repr=repr(order_args))
            # #endregion

            try:
                signed = client.create_and_post_order(order_args)
            except Exception as _exc:
                import traceback as _tb
                _exc_str = str(_exc)
                _client_sig_type = getattr(client, "signature_type", None)
                _client_funder = getattr(client, "funder", None)
                _client_mode = {
                    "sig_type": _client_sig_type,
                    "funder": _client_funder,
                    "has_create_order": hasattr(client, "create_order"),
                    "has_create_and_post_order": hasattr(client, "create_and_post_order"),
                }
                # #region agent debug log
                _debug_err = {
                    "sessionId": "f71d1d", "runId": "run2",
                    "hypothesisId": "H-NONCE,H-EXP,H-SAFE",
                    "location": "polymarket_client.py:place_order:exception",
                    "timestamp": int(time.time() * 1000),
                    "message": "order_submit_exception",
                    "data": {
                        "exc_type": type(_exc).__name__,
                        "exc_str": _exc_str,
                        "order_args_repr": repr(order_args),
                        "client_mode": _client_mode,
                        "traceback": _tb.format_exc()[-2000:],
                    },
                }
                try:
                    with open("/root/Bot/debug-f71d1d.log", "a") as _f:
                        _f.write(json.dumps(_debug_err) + "\n")
                except Exception:
                    pass
                logger.info(
                    "debug_order_exception",
                    exc_type=type(_exc).__name__,
                    exc_str=_exc_str,
                    client_sig_type=_client_sig_type,
                    client_funder=_client_funder,
                    nonce=order_args.nonce,
                    expiration=order_args.expiration,
                )
                # #endregion
                if "order_version_mismatch" not in _exc_str:
                    raise

                # Runtime hypothesis H-NONCE: py-clob-client uses whatever
                # nonce/expiration are in OrderArgs. Our OrderArgs currently
                # default to nonce=0/expiration=0. Retry once with a fresh
                # nonce and short GTD-style expiration to test if the version
                # mismatch is really a stale/invalid order envelope.
                _now_s = int(time.time())
                _nonce_ms = int(time.time() * 1000)
                retry_args = OrderArgs(
                    token_id=token_id,
                    side=side.upper(),
                    price=price,
                    size=size_shares,
                    nonce=_nonce_ms,
                    expiration=_now_s + 3600,
                )
                # #region agent debug log
                _debug_retry_nonce = {
                    "sessionId": "f71d1d", "runId": "run3",
                    "hypothesisId": "H-NONCE,H-EXP",
                    "location": "polymarket_client.py:place_order:retry_nonce",
                    "timestamp": int(time.time() * 1000),
                    "message": "order_submit_retry_nonce",
                    "data": {
                        "nonce": retry_args.nonce,
                        "expiration": retry_args.expiration,
                        "retry_args_repr": repr(retry_args),
                    },
                }
                try:
                    with open("/root/Bot/debug-f71d1d.log", "a") as _f:
                        _f.write(json.dumps(_debug_retry_nonce) + "\n")
                except Exception:
                    pass
                logger.info(
                    "debug_order_retry_nonce_attempt",
                    nonce=retry_args.nonce,
                    expiration=retry_args.expiration,
                )
                # #endregion
                try:
                    signed = client.create_and_post_order(retry_args)
                    logger.info(
                        "debug_order_retry_nonce_success",
                        nonce=retry_args.nonce,
                        expiration=retry_args.expiration,
                    )
                except Exception as _retry_exc:
                    # #region agent debug log
                    _debug_retry_fail = {
                        "sessionId": "f71d1d", "runId": "run3",
                        "hypothesisId": "H-NONCE,H-EXP",
                        "location": "polymarket_client.py:place_order:retry_nonce_exception",
                        "timestamp": int(time.time() * 1000),
                        "message": "order_submit_retry_nonce_exception",
                        "data": {
                            "exc_type": type(_retry_exc).__name__,
                            "exc_str": str(_retry_exc),
                        },
                    }
                    try:
                        with open("/root/Bot/debug-f71d1d.log", "a") as _f:
                            _f.write(json.dumps(_debug_retry_fail) + "\n")
                    except Exception:
                        pass
                    logger.info(
                        "debug_order_retry_nonce_failed",
                        nonce=retry_args.nonce,
                        expiration=retry_args.expiration,
                        exc=str(_retry_exc),
                    )
                    # #endregion
                    raise
            return signed if isinstance(signed, dict) else {"raw": str(signed)}

        result = await asyncio.to_thread(_submit)
        logger.info("clob_order_placed", token_id=token_id, side=side, price=price)
        return result

    async def cancel_order(self, order_id: str) -> bool:
        client = self._ensure_clob()

        def _cancel() -> bool:
            try:
                client.cancel(order_id=order_id)
                return True
            except Exception as exc:  # pragma: no cover - library exceptions vary
                logger.error("clob_cancel_error", order_id=order_id, error=str(exc))
                return False

        return await asyncio.to_thread(_cancel)


# ---- parsing helpers --------------------------------------------------------

def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_market(raw: dict) -> MarketSnapshot:
    outcomes = raw.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            import json as _json

            outcomes = _json.loads(outcomes)
        except ValueError:
            outcomes = []
    prices = raw.get("outcomePrices") or []
    if isinstance(prices, str):
        try:
            import json as _json

            prices = _json.loads(prices)
        except ValueError:
            prices = []
    prices_f = [_as_float(p) for p in prices]
    # Normalize YES/NO prices (Polymarket convention: outcomes[0]=YES)
    best_yes = prices_f[0] if prices_f else None
    best_no = prices_f[1] if len(prices_f) > 1 else (1 - best_yes if best_yes is not None else None)

    # --- CLOB token ids ----------------------------------------------------
    # Gamma returns these as a JSON-encoded string: '["<yes>", "<no>"]'.
    # They are the ERC-1155 token ids the CLOB uses for orders.  Without
    # them we can only simulate trades, not submit them.
    token_ids_raw = raw.get("clobTokenIds") or raw.get("tokens") or raw.get("tokenIds")
    if isinstance(token_ids_raw, str):
        try:
            import json as _json

            token_ids_raw = _json.loads(token_ids_raw)
        except ValueError:
            token_ids_raw = []
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    if isinstance(token_ids_raw, list) and token_ids_raw:
        # Support both raw-string lists and {"token_id": "...", "outcome": "Yes"} dicts.
        first, *rest = token_ids_raw
        if isinstance(first, dict):
            for entry in token_ids_raw:
                if not isinstance(entry, dict):
                    continue
                tid = entry.get("token_id") or entry.get("id")
                outcome = str(entry.get("outcome") or "").lower()
                if not tid:
                    continue
                if outcome.startswith("yes") and yes_token_id is None:
                    yes_token_id = str(tid)
                elif outcome.startswith("no") and no_token_id is None:
                    no_token_id = str(tid)
        else:
            yes_token_id = str(first) if first else None
            no_token_id = str(rest[0]) if rest else None

    return MarketSnapshot(
        id=str(raw.get("id") or raw.get("conditionId") or ""),
        slug=raw.get("slug"),
        question=raw.get("question") or raw.get("title") or "",
        outcomes=[str(o) for o in outcomes],
        outcome_prices=prices_f,
        volume_24h=_as_float(raw.get("volume24hr") or raw.get("volume24h") or raw.get("volume")),
        liquidity=_as_float(raw.get("liquidity") or raw.get("liquidityNum")),
        best_yes_price=best_yes,
        best_no_price=best_no,
        end_date=raw.get("endDate"),
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
    )


def _parse_order_book(token_id: str, data: dict) -> OrderBook:
    """Normalise the CLOB book response.

    The ``/book`` endpoint returns::

        {
          "bids": [{"price": "0.12", "size": "120.0"}, ...],
          "asks": [{"price": "0.14", "size": "200.0"}, ...]
        }
    """

    def _levels(rows: Any, ascending: bool) -> list[OrderBookLevel]:
        if not isinstance(rows, list):
            return []
        levels: list[OrderBookLevel] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            price = _as_float(r.get("price"), -1.0)
            size = _as_float(r.get("size"))
            if price < 0 or size <= 0:
                continue
            levels.append(OrderBookLevel(price=price, size=size))
        levels.sort(key=lambda lv: lv.price, reverse=not ascending)
        return levels

    return OrderBook(
        token_id=token_id,
        bids=_levels(data.get("bids"), ascending=False),
        asks=_levels(data.get("asks"), ascending=True),
    )


def _parse_leaderboard(raw: Any) -> list[LeaderboardEntry]:
    rows = raw if isinstance(raw, list) else raw.get("data") or raw.get("traders") or []
    out: list[LeaderboardEntry] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        addr = r.get("proxyWallet") or r.get("wallet") or r.get("address") or r.get("user")
        if not addr:
            continue
        out.append(
            LeaderboardEntry(
                wallet_address=str(addr).lower(),
                label=r.get("name") or r.get("pseudonym") or r.get("label"),
                pnl_usd=_as_float(r.get("pnl") or r.get("profit")),
                volume_usd=_as_float(r.get("volume")),
                roi=_as_float(r.get("roi") or r.get("returnPct")),
            )
        )
    return out


def _parse_user_trades(raw: Any) -> list[UserTrade]:
    rows = raw if isinstance(raw, list) else raw.get("trades") or raw.get("data") or []
    out: list[UserTrade] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        side_raw = str(r.get("side") or r.get("outcome") or "yes").lower()
        side = "yes" if side_raw in ("yes", "buy", "long", "true") else "no"
        size_usd = _as_float(r.get("size") or r.get("usdcSize") or r.get("sizeUsd"))
        price = r.get("price")
        out.append(
            UserTrade(
                market_id=str(
                    r.get("market") or r.get("marketId") or r.get("conditionId") or ""
                ),
                market_slug=r.get("slug"),
                side=side,
                price=_as_float(price) if price is not None else None,
                size_usd=size_usd,
                tx_hash=r.get("transactionHash") or r.get("txHash"),
                timestamp=(
                    int(r["timestamp"])
                    if r.get("timestamp") and str(r["timestamp"]).isdigit()
                    else None
                ),
            )
        )
    return out


# ---- web3 helper ------------------------------------------------------------

def _read_usdc_balance(address: str) -> Decimal:
    """Read USDC.e balance for ``address`` on Polygon.  Blocking."""
    try:
        from web3 import Web3
    except ImportError:
        logger.error("web3_not_installed")
        return Decimal("0")

    rpc = settings.polygon_rpc_url
    if not rpc:
        return Decimal("0")

    try:
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(POLYGON_USDC_ADDRESS),
            abi=ERC20_BALANCE_ABI,
        )
        raw = contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
        decimals = contract.functions.decimals().call()
        return Decimal(raw) / (Decimal(10) ** decimals)
    except Exception as exc:  # pragma: no cover - network dependent
        logger.error("usdc_balance_error", error=str(exc))
        return Decimal("0")
