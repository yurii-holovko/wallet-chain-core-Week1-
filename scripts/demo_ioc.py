from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "src"
for path in (ROOT, SRC_PATH):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from config import BINANCE_CONFIG  # noqa: E402
from exchange.client import ExchangeClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Demo: place LIMIT IOC on Binance testnet"
    )
    parser.add_argument("--symbol", default="ETH/USDT", help="Trading pair symbol")
    parser.add_argument("--amount", type=float, default=0.01, help="Order size")
    parser.add_argument(
        "--price-multiplier",
        type=float,
        default=0.999,
        help="Multiplier vs best bid (use <1.0 to avoid fills)",
    )
    args = parser.parse_args()

    client = ExchangeClient(BINANCE_CONFIG)
    orderbook = client.fetch_order_book(args.symbol, limit=5)
    best_bid = orderbook.get("best_bid")
    if not best_bid:
        raise SystemExit("Best bid missing; cannot place IOC order")

    base_multiplier = Decimal(str(args.price_multiplier))
    fallbacks = [Decimal("0.99"), Decimal("0.995"), Decimal("0.998"), Decimal("0.999")]
    multipliers = [base_multiplier] + [
        value for value in fallbacks if value != base_multiplier
    ]

    order = None
    last_error: Exception | None = None
    for multiplier in multipliers:
        price = float(best_bid[0] * multiplier)
        try:
            print(f"Placing IOC buy at {price:.6f} (multiplier {multiplier})")
            order = client.create_limit_ioc_order(
                args.symbol, "buy", args.amount, price
            )
            break
        except Exception as exc:
            last_error = exc
            continue

    if order is None:
        raise SystemExit(
            f"Failed to place IOC order. Last error: {last_error}. "
            "Try a multiplier closer to 1.0, e.g. 0.999."
        )

    print("IOC order result:", order)

    order_id = order.get("id")
    if order_id:
        status = client.fetch_order_status(order_id, args.symbol)
        print("Order status:", status)


if __name__ == "__main__":
    main()
