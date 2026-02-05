from decimal import Decimal


class OrderBookAnalyzer:
    """
    Analyze order book snapshots for trading decisions.
    """

    def __init__(self, orderbook: dict):
        """
        Initialize with order book from ExchangeClient.fetch_order_book().
        """
        ...

    def walk_the_book(
        self,
        side: str,  # "buy" (walk asks) or "sell" (walk bids)
        qty: float,  # Amount of base asset
    ) -> dict:
        """
        Simulate filling `qty` against the order book.

        Returns:
        {
            'avg_price': Decimal,
            'total_cost': Decimal,     # In quote currency
            'slippage_bps': Decimal,   # vs best price
            'levels_consumed': int,    # How deep we went
            'fully_filled': bool,
            'fills': [
                {'price': Decimal, 'qty': Decimal, 'cost': Decimal},
                ...
            ]
        }

        If insufficient liquidity, fully_filled=False and fills show what IS available.
        """
        ...

    def depth_at_bps(
        self,
        side: str,  # "bid" or "ask"
        bps: float,  # How deep (e.g., 10 = within 10 bps of best)
    ) -> Decimal:
        """
        Total quantity available within `bps` basis points of best price.
        Measures how much you can trade without moving price beyond threshold.
        """
        ...

    def imbalance(self, levels: int = 10) -> float:
        """
        Order book imbalance ratio.
        Returns [-1.0, +1.0] where:
          +1.0 = all bids (buy pressure)
          -1.0 = all asks (sell pressure)
        """
        ...

    def effective_spread(self, qty: float) -> Decimal:
        """
        Effective spread for a round-trip of size `qty`.
        = (avg_ask_fill - avg_bid_fill) / mid_price * 10000 (bps)

        This is the TRUE cost of immediacy for your trade size.
        Different from quoted spread which only considers best levels.
        """
        ...
