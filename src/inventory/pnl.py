from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from inventory.tracker import Venue


@dataclass
class TradeLeg:
    """Single execution leg."""

    id: str
    timestamp: datetime
    venue: Venue
    symbol: str  # "ETH/USDT"
    side: str  # "buy" or "sell"
    amount: Decimal  # Base asset qty
    price: Decimal  # Execution price
    fee: Decimal
    fee_asset: str


@dataclass
class ArbRecord:
    """Complete arb trade with both legs."""

    id: str
    timestamp: datetime
    buy_leg: TradeLeg
    sell_leg: TradeLeg
    gas_cost_usd: Decimal = Decimal("0")

    @property
    def gross_pnl(self) -> Decimal:
        """Price difference revenue."""
        ...

    @property
    def total_fees(self) -> Decimal:
        """All fees: both legs + gas."""
        ...

    @property
    def net_pnl(self) -> Decimal:
        """Gross - fees."""
        ...

    @property
    def net_pnl_bps(self) -> Decimal:
        """Net PnL in basis points of notional."""
        ...

    @property
    def notional(self) -> Decimal:
        """Trade size in quote currency."""
        ...


class PnLEngine:
    """
    Tracks all arb trades and produces PnL reports.
    """

    def __init__(self):
        self.trades: list[ArbRecord] = []

    def record(self, trade: ArbRecord):
        """Record a completed arb trade."""
        ...

    def summary(self) -> dict:
        """
        Aggregate PnL summary.

        Returns:
        {
            'total_trades': int,
            'total_pnl_usd': Decimal,
            'total_fees_usd': Decimal,
            'avg_pnl_per_trade': Decimal,
            'avg_pnl_bps': Decimal,
            'win_rate': float,           # % of trades with positive PnL
            'best_trade_pnl': Decimal,
            'worst_trade_pnl': Decimal,
            'total_notional': Decimal,
            'sharpe_estimate': float,    # PnL / stddev(PnL) — rough estimate
            'pnl_by_hour': dict,         # {hour: total_pnl}
        }
        """
        ...

    def recent(self, n: int = 10) -> list[dict]:
        """
        Last N trades as summary dicts.
        For display in CLI dashboard.
        """
        ...

    def export_csv(self, filepath: str):
        """Export all trades to CSV for analysis."""
        ...
