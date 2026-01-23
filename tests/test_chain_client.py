import pytest

from chain.client import ChainClient, GasPrice
from chain.errors import InsufficientFunds, NonceTooLow, RPCError
from core.base_types import Address, TokenAmount, TransactionRequest


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_rpc_retries_then_success(monkeypatch):
    calls = {"count": 0}

    def fake_post(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            import requests

            raise requests.Timeout("boom")
        return _Response({"result": "0x1"})

    client = ChainClient(["https://rpc.example"], max_retries=2)
    monkeypatch.setattr(client._session, "post", fake_post)
    monkeypatch.setattr("chain.client.time.sleep", lambda *_: None)

    result = client._rpc_call("eth_blockNumber", [])
    assert result == "0x1"


def test_rpc_error_classification_nonce_too_low(monkeypatch):
    def fake_post(*args, **kwargs):
        return _Response(
            {
                "error": {
                    "message": "nonce too low",
                    "code": -32000,
                }
            }
        )

    client = ChainClient(["https://rpc.example"])
    monkeypatch.setattr(client._session, "post", fake_post)

    with pytest.raises(NonceTooLow):
        client._rpc_call("eth_sendRawTransaction", ["0x00"])


def test_rpc_error_classification_insufficient_funds(monkeypatch):
    def fake_post(*args, **kwargs):
        return _Response(
            {
                "error": {
                    "message": "insufficient funds for gas * price + value",
                    "code": -32000,
                }
            }
        )

    client = ChainClient(["https://rpc.example"])
    monkeypatch.setattr(client._session, "post", fake_post)

    with pytest.raises(InsufficientFunds):
        client._rpc_call("eth_sendRawTransaction", ["0x00"])


def test_rpc_error_payload_exposed(monkeypatch):
    def fake_post(*args, **kwargs):
        return _Response({"error": {"message": "boom", "code": 123, "data": "0xdead"}})

    client = ChainClient(["https://rpc.example"])
    monkeypatch.setattr(client._session, "post", fake_post)

    with pytest.raises(RPCError) as exc:
        client._rpc_call("eth_call", [])

    assert exc.value.code == 123
    assert exc.value.data == "0xdead"


def test_get_gas_price_uses_priority_fee(monkeypatch):
    def fake_post(*args, **kwargs):
        payload = kwargs.get("json")
        method = payload["method"]
        if method == "eth_getBlockByNumber":
            return _Response({"result": {"baseFeePerGas": "0x5"}})
        return _Response({"result": "0x3"})

    client = ChainClient(["https://rpc.example"])
    monkeypatch.setattr(client._session, "post", fake_post)

    gas = client.get_gas_price()
    assert isinstance(gas, GasPrice)
    assert gas.base_fee == 5
    assert gas.priority_fee_medium == 3


def test_call_returns_bytes(monkeypatch):
    def fake_post(*args, **kwargs):
        return _Response({"result": "0x1234"})

    client = ChainClient(["https://rpc.example"])
    monkeypatch.setattr(client._session, "post", fake_post)
    tx = TransactionRequest(
        to=Address.from_string("0x000000000000000000000000000000000000dead"),
        value=TokenAmount(raw=0, decimals=18),
        data=b"",
    )
    assert client.call(tx) == bytes.fromhex("1234")
