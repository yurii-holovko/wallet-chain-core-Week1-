# inventory/tracker.py

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Venue(str, Enum):
    BINANCE = "binance"
    WALLET = "wallet"  # On-chain wallet (DEX venue)


@dataclass
class Balance:
    venue: Venue
    asset: str
    free: Decimal
    locked: Decimal = Decimal("0")

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


class InventoryTracker:
    """
    Tracks positions across CEX and DEX venues.
    Single source of truth for where your money is.
    """

    def __init__(self, venues: list[Venue]):
        """Initialize tracker for given venues."""
        ...

    def update_from_cex(self, venue: Venue, balances: dict):
        """
        Update balances from ExchangeClient.fetch_balance().
        Replaces previous snapshot for this venue.

        Args:
            venue: Which CEX venue
            balances: {asset: {free, locked, total}} from ExchangeClient
        """
        ...

    def update_from_wallet(self, venue: Venue, balances: dict):
        """
        Update balances from on-chain wallet query.

        Args:
            venue: Wallet venue
            balances: {asset: amount} from chain/ module
        """
        ...

    def snapshot(self) -> dict:
        """
        Full portfolio snapshot at current time.

        Returns:
        {
            'timestamp': datetime,
            'venues': {
                'binance': {'ETH': {'free': ..., 'locked': ..., 'total': ...}, ...},
                'wallet':  {'ETH': {'free': ..., 'locked': ..., 'total': ...}, ...},
            },
            'totals': {
                'ETH':  Decimal('20.0'),
                'USDT': Decimal('40000.0'),
            },
            'total_usd': Decimal('80200.0'),  # requires price feed
        }
        """
        ...

    def get_available(self, venue: Venue, asset: str) -> Decimal:
        """
        How much of `asset` is available to trade at `venue`.
        Returns free balance only (not locked in orders).
        """
        ...

    def can_execute(
        self,
        buy_venue: Venue,
        buy_asset: str,  # What you're spending (e.g., "USDT")
        buy_amount: Decimal,  # How much you're spending
        sell_venue: Venue,
        sell_asset: str,  # What you're selling (e.g., "ETH")
        sell_amount: Decimal,  # How much you're selling
    ) -> dict:
        """
        Pre-flight check: can we execute both legs of an arb?

        Returns:
        {
            'can_execute': bool,
            'buy_venue_available': Decimal,
            'buy_venue_needed': Decimal,
            'sell_venue_available': Decimal,
            'sell_venue_needed': Decimal,
            'reason': str or None,  # Why not, if can_execute=False
        }
        """
        ...

    def record_trade(
        self,
        venue: Venue,
        side: str,  # "buy" or "sell"
        base_asset: str,
        quote_asset: str,
        base_amount: Decimal,
        quote_amount: Decimal,
        fee: Decimal,
        fee_asset: str,
    ):
        """
        Update internal balances after a trade executes.
        Must handle: buy increases base / decreases quote,
                     sell decreases base / increases quote,
                     fee deducted from fee_asset.
        """
        ...

    def skew(self, asset: str) -> dict:
        """
        Calculate distribution skew for an asset across venues.

        Returns:
        {
            'asset': str,
            'total': Decimal,
            'venues': {
                'binance': {'amount': Decimal, 'pct': float, 'deviation_pct': float},
                'wallet':  {'amount': Decimal, 'pct': float, 'deviation_pct': float},
            },
            'max_deviation_pct': float,
            'needs_rebalance': bool,  # True if max_deviation > 30%
        }
        """
        ...
