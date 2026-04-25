"""One-shot .env sanity check.  Run it after filling in .env."""
from dotenv import dotenv_values

REQUIRED = [
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_TELEGRAM_IDS",
    "ADMIN_TELEGRAM_IDS",
    "MISTRAL_API_KEY",
    "DATABASE_URL",
]
REQUIRED_FOR_LIVE = [
    "WALLET_ADDRESS",
    "WALLET_PRIVATE_KEY",
    "POLYGON_RPC_URL",
]
OPTIONAL_LIVE = [
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "RELAYER_API_KEY",
    "RELAYER_API_KEY_ADDRESS",
    "POLYMARKET_SIGNATURE_TYPE",
    "POLYMARKET_FUNDER_ADDRESS",
]

v = dotenv_values(".env")

print("REQUIRED (all must be FILLED):")
missing = []
for k in REQUIRED:
    val = v.get(k) or ""
    status = "FILLED" if val else "EMPTY"
    print(f"  {k:28s} {status:7s} ({len(val)} chars)")
    if not val:
        missing.append(k)

print("\nREQUIRED FOR LIVE TRADING (signer + RPC):")
for k in REQUIRED_FOR_LIVE:
    val = v.get(k) or ""
    status = "FILLED" if val else "EMPTY"
    print(f"  {k:28s} {status}")

print("\nOPTIONAL (auto-derived or only for proxy setups):")
for k in OPTIONAL_LIVE:
    val = v.get(k) or ""
    status = "FILLED" if val else "EMPTY"
    print(f"  {k:28s} {status}")

sim = (v.get("SIMULATION_MODE") or "true").lower()
print(f"\nSIMULATION_MODE = {sim}")

dsn = v.get("DATABASE_URL") or ""
if dsn and not dsn.startswith("postgresql+asyncpg://"):
    print(
        "\nWARNING: DATABASE_URL must start with 'postgresql+asyncpg://' "
        f"(currently starts with '{dsn.split('://', 1)[0]}://')."
    )

if missing:
    print(f"\n{len(missing)} required field(s) missing: {', '.join(missing)}")
    raise SystemExit(1)
print("\nAll required fields present.")
