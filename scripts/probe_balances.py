"""Quick balance probe: USDC.e + native USDC + MATIC.

Usage::

    python -m scripts.probe_balances [address]

If ``address`` is omitted, it uses ``WALLET_ADDRESS`` from .env.

Prints nothing sensitive; only public chain data.
"""
from __future__ import annotations

import sys
from decimal import Decimal

from app.config.settings import settings


USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # bridged (Polymarket)
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # CCTP native

ABI = [
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


def main() -> int:
    from web3 import Web3

    addr = sys.argv[1] if len(sys.argv) > 1 else settings.wallet_address
    if not addr:
        print("No address.")
        return 1

    rpc = settings.polygon_rpc_url or "https://polygon.drpc.org"
    print(f"RPC     : {rpc}")
    print(f"Address : {addr}")

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 12}))
    if not w3.is_connected():
        print("RPC not reachable.")
        return 1

    checksum = Web3.to_checksum_address(addr)

    matic_wei = w3.eth.get_balance(checksum)
    print(f"MATIC   : {Decimal(matic_wei) / Decimal(10**18):.6f}  (for gas)")

    for label, contract_addr in (("USDC.e (Polymarket)", USDC_E), ("USDC native", USDC_NATIVE)):
        try:
            c = w3.eth.contract(address=Web3.to_checksum_address(contract_addr), abi=ABI)
            raw = c.functions.balanceOf(checksum).call()
            dec = c.functions.decimals().call()
            print(f"{label:22s}: {Decimal(raw) / (Decimal(10) ** dec)}")
        except Exception as exc:  # noqa: BLE001
            print(f"{label:22s}: error: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
