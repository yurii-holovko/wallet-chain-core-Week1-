# inventory/rebalancer.py

from dataclasses import dataclass
from decimal import Decimal

from inventory.tracker import InventoryTracker, Venue


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
    ): ...

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
        ...

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
        ...

    def plan_all(self) -> dict[str, list[TransferPlan]]:
        """
        Generate rebalancing plans for ALL skewed assets.
        Returns {asset: [TransferPlan, ...]}
        """
        ...

    def estimate_cost(self, plans: list[TransferPlan]) -> dict:
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
        ...
