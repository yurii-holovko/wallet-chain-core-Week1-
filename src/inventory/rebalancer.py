# inventory/rebalancer.py

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from inventory.tracker import InventoryTracker, Venue

TRANSFER_FEES = {
    "ETH": {
        "withdrawal_fee": Decimal("0.005"),
        "min_withdrawal": Decimal("0.01"),
        "confirmations": 12,
        "estimated_time_min": 15,
    },
    "USDT": {
        "withdrawal_fee": Decimal("1.0"),
        "min_withdrawal": Decimal("10.0"),
        "confirmations": 12,
        "estimated_time_min": 15,
    },
    "USDC": {
        "withdrawal_fee": Decimal("1.0"),
        "min_withdrawal": Decimal("10.0"),
        "confirmations": 12,
        "estimated_time_min": 15,
    },
}

MIN_OPERATING_BALANCE = {
    "ETH": Decimal("0.5"),
    "USDT": Decimal("500"),
    "USDC": Decimal("500"),
}


@dataclass
class TransferPlan:
    """A planned transfer between venues."""

    from_venue: Venue
    to_venue: Venue
    asset: str
    amount: Decimal
    estimated_fee: Decimal  # Withdrawal/gas fee
    estimated_time_min: int  # Minutes to complete

    @property
    def net_amount(self) -> Decimal:
        """Amount received after fees."""
        return self.amount - self.estimated_fee


class RebalancePlanner:
    """
    Generates rebalancing plans when inventory skew exceeds threshold.
    Plans only — does NOT execute transfers.
    """

    def __init__(
        self,
        tracker: InventoryTracker,
        threshold_pct: float = 30.0,  # Rebalance when deviation > 30%
        target_ratio: dict[Venue, float] = None,  # Default: equal split
    ):
        self._tracker = tracker
        self._threshold_pct = threshold_pct
        self._target_ratio = self._normalize_target_ratio(target_ratio)

    def check_all(self) -> list[dict]:
        """
        Check all tracked assets for skew.
        Returns list of assets that need rebalancing.

        Returns:
        [
            {'asset': 'ETH', 'max_deviation_pct': 42.5, 'needs_rebalance': True},
            {'asset': 'USDT', 'max_deviation_pct': 15.2, 'needs_rebalance': False},
        ]
        """
        snapshot = self._tracker.snapshot()
        results: list[dict] = []
        for asset in snapshot["totals"].keys():
            skew = self._tracker.skew(asset)
            needs = skew["max_deviation_pct"] > self._threshold_pct
            results.append(
                {
                    "asset": asset,
                    "max_deviation_pct": skew["max_deviation_pct"],
                    "needs_rebalance": needs,
                }
            )
        return results

    def plan(self, asset: str) -> list[TransferPlan]:
        """
        Generate transfer plan to rebalance a specific asset.

        Rules:
        - Only generate transfers that reduce skew
        - Respect minimum transfer amounts (e.g., Binance min withdrawal)
        - Account for transfer fees in the plan
        - Never plan a transfer that would leave a venue below minimum operating balance

        Returns list of TransferPlan objects.
        Empty list if no rebalance needed.
        """
        asset_key = asset.upper()
        skew = self._tracker.skew(asset_key)
        if skew["max_deviation_pct"] <= self._threshold_pct:
            return []

        fees = TRANSFER_FEES.get(asset_key)
        if not fees:
            return []

        venues = list(self._target_ratio.keys())
        total = skew["total"]
        if total <= 0:
            return []

        current: dict[Venue, Decimal] = {}
        for venue in venues:
            current_amount = (
                skew["venues"].get(venue.value, {}).get("amount", Decimal("0"))
            )
            current[venue] = current_amount

        target_amounts = {
            venue: total * Decimal(str(self._target_ratio[venue])) for venue in venues
        }
        deltas = {venue: current[venue] - target_amounts[venue] for venue in venues}

        from_venue = max(deltas, key=lambda v: deltas[v])
        to_venue = min(deltas, key=lambda v: deltas[v])
        amount = min(deltas[from_venue], -deltas[to_venue])
        if amount <= 0:
            return []

        min_withdrawal = fees["min_withdrawal"]
        withdrawal_fee = fees["withdrawal_fee"]
        min_operating = MIN_OPERATING_BALANCE.get(asset_key, Decimal("0"))
        available = self._tracker.get_available(from_venue, asset_key)
        max_transfer = max(available - min_operating, Decimal("0"))
        amount = min(amount, max_transfer)
        if amount < min_withdrawal:
            return []
        if amount - withdrawal_fee <= 0:
            return []

        return [
            TransferPlan(
                from_venue=from_venue,
                to_venue=to_venue,
                asset=asset_key,
                amount=amount,
                estimated_fee=withdrawal_fee,
                estimated_time_min=int(fees["estimated_time_min"]),
            )
        ]

    def plan_all(self) -> dict[str, list[TransferPlan]]:
        """
        Generate rebalancing plans for ALL skewed assets.
        Returns {asset: [TransferPlan, ...]}
        """
        plans: dict[str, list[TransferPlan]] = {}
        snapshot = self._tracker.snapshot()
        for asset in snapshot["totals"].keys():
            asset_plans = self.plan(asset)
            if asset_plans:
                plans[asset] = asset_plans
        return plans

    def estimate_cost(
        self, plans: list[TransferPlan], price_map: dict[str, Decimal] | None = None
    ) -> dict:
        """
        Estimate total cost of executing rebalance plans.

        Returns:
        {
            'total_transfers': int,
            'total_fees_usd': Decimal,
            'total_time_min': int,  # Max of all transfer times (parallel)
            'assets_affected': list[str],
        }
        """
        price_map = price_map or {}
        total_fees_usd = Decimal("0")
        total_time_min = 0
        assets: set[str] = set()

        for plan in plans:
            assets.add(plan.asset)
            total_time_min = max(total_time_min, plan.estimated_time_min)
            if plan.asset in {"USDT", "USDC"}:
                total_fees_usd += plan.estimated_fee
            elif plan.asset in price_map:
                total_fees_usd += plan.estimated_fee * price_map[plan.asset]

        return {
            "total_transfers": len(plans),
            "total_fees_usd": total_fees_usd,
            "total_time_min": total_time_min,
            "assets_affected": sorted(assets),
        }

    def _normalize_target_ratio(
        self, ratio: dict[Venue, float] | None
    ) -> dict[Venue, float]:
        if ratio is None:
            equal = 1.0 / len(self._tracker._venues)
            return {venue: equal for venue in self._tracker._venues}
        total = sum(ratio.values())
        if total <= 0:
            raise ValueError("target_ratio must sum to > 0")
        return {venue: ratio[venue] / total for venue in ratio}


def _format_decimal(value: Decimal, places: int = 2) -> str:
    quantize_value = Decimal(f"1e-{places}")
    return format(
        value.quantize(quantize_value, rounding=ROUND_HALF_UP), f",.{places}f"
    )


def _load_balances(path: Path | None) -> dict:
    if path is None:
        return {
            "binance": {
                "ETH": {"free": "2.0", "locked": "0"},
                "USDT": {"free": "18000", "locked": "0"},
            },
            "wallet": {"ETH": "8.0", "USDT": "12000"},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _build_tracker_from_data(data: dict) -> InventoryTracker:
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    cex_balances = data.get("binance", {})
    wallet_balances = data.get("wallet", {})
    tracker.update_from_cex(Venue.BINANCE, cex_balances)
    tracker.update_from_wallet(Venue.WALLET, wallet_balances)
    return tracker


def _print_check(planner: RebalancePlanner) -> None:
    print("Inventory Skew Report")
    print("═" * 43)
    snapshot = planner._tracker.snapshot()
    for asset in snapshot["totals"].keys():
        skew = planner._tracker.skew(asset)
        print(f"Asset: {asset}")
        for venue, data in skew["venues"].items():
            amount = _format_decimal(data["amount"], 2)
            pct = data["pct"] * 100
            deviation = data["deviation_pct"]
            print(
                f"  {venue.title():<7} {amount} {asset} ({pct:.0f}%)"
                f"   deviation: {deviation:+.0f}%"
            )
        status = (
            "NEEDS REBALANCE"
            if skew["max_deviation_pct"] > planner._threshold_pct
            else "OK"
        )
        print(f"  Status: {status}")
        print("")


def _print_plan(planner: RebalancePlanner, asset: str) -> None:
    plans = planner.plan(asset)
    if not plans:
        print(f"No rebalance needed for {asset.upper()}")
        return
    print(f"Rebalance Plan: {asset.upper()}")
    print("─" * 43)
    for idx, plan in enumerate(plans, start=1):
        print(f"Transfer {idx}:")
        print(f"  From:     {plan.from_venue.value}")
        print(f"  To:       {plan.to_venue.value}")
        print(f"  Amount:   {_format_decimal(plan.amount, 4)} {plan.asset}")
        print(f"  Fee:      {_format_decimal(plan.estimated_fee, 4)} {plan.asset}")
        print(f"  ETA:      ~{plan.estimated_time_min} min")
        print("")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory rebalancer")
    parser.add_argument("--check", action="store_true", help="Show skew report")
    parser.add_argument("--plan", help="Generate plan for asset (e.g., ETH)")
    parser.add_argument("--balances", help="Path to balances JSON")
    args = parser.parse_args()

    data = _load_balances(Path(args.balances) if args.balances else None)
    tracker = _build_tracker_from_data(data)
    planner = RebalancePlanner(tracker)

    if args.check:
        _print_check(planner)
    if args.plan:
        _print_plan(planner, args.plan)


if __name__ == "__main__":
    main()
