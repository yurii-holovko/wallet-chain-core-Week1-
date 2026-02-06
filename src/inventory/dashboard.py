from __future__ import annotations

import argparse
import json
import os
import time
from datetime import timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from config import BINANCE_CONFIG
from exchange.client import ExchangeClient
from inventory.tracker import InventoryTracker, Venue


def _format_decimal(value: Decimal, places: int = 2) -> str:
    quantize_value = Decimal(f"1e-{places}")
    return format(
        value.quantize(quantize_value, rounding=ROUND_HALF_UP), f",.{places}f"
    )


def _load_wallet_balances(path: Path | None) -> dict:
    if path is None:
        return {"USDT": "15000", "ETH": "0"}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "crypto" in data and "address" in data:
        raise SystemExit(
            "Wallet file looks like a keystore; provide balances JSON like "
            '{"USDT": "15000", "ETH": "0"} or {"wallet": {...}}.'
        )
    if isinstance(data, dict) and "wallet" in data and isinstance(data["wallet"], dict):
        return data["wallet"]
    if (
        isinstance(data, dict)
        and "balances" in data
        and isinstance(data["balances"], dict)
    ):
        return data["balances"]
    if isinstance(data, dict):
        return data
    raise SystemExit("Wallet balances file must be a JSON object.")


def _clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _render(
    snapshot: dict,
    tracker: InventoryTracker,
    error: str | None,
    wallet_assets: set[str],
    show_wallet_only: bool,
    show_nonzero_only: bool,
) -> None:
    timestamp = (
        snapshot["timestamp"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    venues = snapshot["venues"]
    assets: set[str] = set()
    for venue_assets in venues.values():
        assets.update(venue_assets.keys())
    if show_wallet_only:
        assets = assets.intersection(wallet_assets)
    if show_nonzero_only:
        assets = {
            asset
            for asset in assets
            if (
                venues.get("binance", {}).get(asset, {}).get("total", Decimal("0"))
                + venues.get("wallet", {}).get(asset, {}).get("total", Decimal("0"))
            )
            > 0
        }

    _clear_screen()
    print("Real-time Inventory Dashboard")
    print("-" * 60)
    print(f"Updated: {timestamp}")
    if error:
        print(f"CEX error: {error}")
    print("-" * 60)
    print(f"{'Asset':<8} {'Binance':>15} {'Wallet':>15} {'Total':>15}")
    print("-" * 60)
    for asset in sorted(assets):
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
    print("-" * 60)
    print("Skew (deviation vs equal split):")
    for asset in sorted(assets):
        skew = tracker.skew(asset)
        if skew["total"] == 0:
            continue
        venues_result = skew["venues"]
        binance_dev = venues_result.get("binance", {}).get("deviation_pct", 0.0)
        wallet_dev = venues_result.get("wallet", {}).get("deviation_pct", 0.0)
        print(f"  {asset}: Binance {binance_dev:+.1f}%, Wallet {wallet_dev:+.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-time inventory dashboard")
    parser.add_argument(
        "--interval", type=float, default=5.0, help="Refresh interval (s)"
    )
    parser.add_argument("--wallet-balances", help="Path to wallet balances JSON")
    parser.add_argument(
        "--wallet-only",
        action="store_true",
        help="Only display assets present in the wallet balances file",
    )
    parser.add_argument(
        "--nonzero-only",
        action="store_true",
        help="Only display assets with non-zero totals",
    )
    args = parser.parse_args()

    client = ExchangeClient(BINANCE_CONFIG)
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    wallet_path = Path(args.wallet_balances) if args.wallet_balances else None

    wallet_balances = _load_wallet_balances(wallet_path)
    wallet_assets = {asset.upper() for asset in wallet_balances.keys()}

    while True:
        error = None
        try:
            cex_balances = client.fetch_balance()
        except Exception as exc:
            error = str(exc)
            cex_balances = {}
        wallet_balances = _load_wallet_balances(wallet_path)
        wallet_assets = {asset.upper() for asset in wallet_balances.keys()}
        tracker.update_from_cex(Venue.BINANCE, cex_balances)
        tracker.update_from_wallet(Venue.WALLET, wallet_balances)
        snapshot = tracker.snapshot()
        _render(
            snapshot,
            tracker,
            error,
            wallet_assets,
            args.wallet_only,
            args.nonzero_only,
        )
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
