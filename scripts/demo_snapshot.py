from __future__ import annotations

import argparse
import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "src"
for path in (ROOT, SRC_PATH):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from config import BINANCE_CONFIG  # noqa: E402
from exchange.client import ExchangeClient  # noqa: E402
from inventory.tracker import InventoryTracker, Venue  # noqa: E402


def _format_decimal(value: Decimal, places: int = 2) -> str:
    quantize_value = Decimal(f"1e-{places}")
    return format(
        value.quantize(quantize_value, rounding=ROUND_HALF_UP), f",.{places}f"
    )


def _load_wallet_balances(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "wallet" in data and isinstance(data["wallet"], dict):
        return data["wallet"]
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Demo: portfolio snapshot")
    parser.add_argument(
        "--wallet-balances",
        default="docs/examples/wallet_balances.json",
        help="Path to wallet balances JSON",
    )
    args = parser.parse_args()

    client = ExchangeClient(BINANCE_CONFIG)
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])

    wallet = _load_wallet_balances(Path(args.wallet_balances))
    tracker.update_from_wallet(Venue.WALLET, wallet)
    tracker.update_from_cex(Venue.BINANCE, client.fetch_balance())

    snapshot = tracker.snapshot()
    venues = snapshot["venues"]
    assets = sorted(
        {*venues.get("binance", {}).keys(), *venues.get("wallet", {}).keys()}
    )

    print("Portfolio Snapshot")
    print("-" * 60)
    print(f"{'Asset':<8} {'Binance':>15} {'Wallet':>15} {'Total':>15}")
    print("-" * 60)
    for asset in assets:
        binance_total = (
            venues.get("binance", {}).get(asset, {}).get("total", Decimal("0"))
        )
        wallet_total = (
            venues.get("wallet", {}).get(asset, {}).get("total", Decimal("0"))
        )
        total = binance_total + wallet_total
        print(
            f"{asset:<8} {_format_decimal(binance_total):>15} "
            f"{_format_decimal(wallet_total):>15} {_format_decimal(total):>15}"
        )


if __name__ == "__main__":
    main()
