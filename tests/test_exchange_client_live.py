from __future__ import annotations

import pytest

from config import BINANCE_CONFIG, get_env
from exchange.client import ExchangeClient


@pytest.mark.integration
def test_fetch_order_book_real_testnet() -> None:
    api_key = get_env("BINANCE_TESTNET_API_KEY")
    secret = get_env("BINANCE_TESTNET_SECRET")
    if not api_key or not secret:
        pytest.skip("Missing BINANCE_TESTNET_API_KEY/SECRET for live testnet test.")
    client = ExchangeClient(BINANCE_CONFIG)
    orderbook = client.fetch_order_book("ETH/USDT", limit=5)
    assert orderbook["bids"]
    assert orderbook["asks"]
