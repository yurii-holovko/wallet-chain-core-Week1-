# exchange/client.py


class ExchangeClient:
    """
    Wrapper around ccxt for Binance testnet.
    Handles rate limiting, error handling, and response normalization.
    """

    def __init__(self, config: dict):
        """
        Initialize with config dict containing apiKey, secret, sandbox flag.
        Must validate connection on init (fetch server time or status).
        """
        ...

    def fetch_order_book(
        self, symbol: str, limit: int = 20  # "ETH/USDT"  # Number of price levels
    ) -> dict:
        """
        Fetch L2 order book snapshot.

        Returns normalized dict:
        {
            'symbol': 'ETH/USDT',
            'timestamp': 1706000000000,
            'bids': [(price, qty), ...],  # Sorted best→worst
            'asks': [(price, qty), ...],  # Sorted best→worst
            'best_bid': (price, qty),
            'best_ask': (price, qty),
            'mid_price': Decimal,
            'spread_bps': Decimal,
        }
        """
        ...

    def fetch_balance(self) -> dict[str, dict]:
        """
        Fetch account balances.

        Returns:
        {
            'ETH': {
                'free': Decimal('10.5'),
                'locked': Decimal('0'),
                'total': Decimal('10.5'),
            },
            'USDT': {
                'free': Decimal('20000'),
                'locked': Decimal('500'),
                'total': Decimal('20500'),
            },
            ...
        }

        Must filter out zero-balance assets.
        """
        ...

    def create_limit_ioc_order(
        self,
        symbol: str,  # "ETH/USDT"
        side: str,  # "buy" or "sell"
        amount: float,  # Quantity of base asset
        price: float,  # Limit price
    ) -> dict:
        """
        Place a LIMIT IOC (Immediate Or Cancel) order.

        Returns normalized order result:
        {
            'id': str,
            'symbol': str,
            'side': str,
            'type': 'limit',
            'time_in_force': 'IOC',
            'amount_requested': Decimal,
            'amount_filled': Decimal,
            'avg_fill_price': Decimal,
            'fee': Decimal,
            'fee_asset': str,
            'status': str,  # 'filled', 'partially_filled', 'expired'
            'timestamp': int,
        }

        Must handle: partial fills, rejection, and exchange errors.
        """
        ...

    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
    ) -> dict:
        """
        Place a market order. Same return format as create_limit_ioc_order.
        Use sparingly — LIMIT IOC is preferred for arb.
        """
        ...

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an open order. Returns order status after cancel."""
        ...

    def fetch_order_status(self, order_id: str, symbol: str) -> dict:
        """Check current status of an order."""
        ...

    def get_trading_fees(self, symbol: str) -> dict:
        """
        Returns fee structure:
        {'maker': Decimal('0.001'), 'taker': Decimal('0.001')}
        """
        ...
