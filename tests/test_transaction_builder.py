from dataclasses import dataclass

import pytest

from chain.transaction_builder import TransactionBuilder
from core.base_types import Address, TokenAmount, TransactionReceipt


@dataclass
class _Signed:
    raw_transaction: bytes


class _FakeWallet:
    def __init__(self, address: str):
        self.address = address

    def sign_transaction(self, tx: dict):
        return _Signed(b"\x01\x02")


class _FakeClient:
    def __init__(self):
        self._nonce = 7
        self._gas = 21000

    def estimate_gas(self, tx):
        return self._gas

    def get_gas_price(self):
        class _Gas:
            priority_fee_low = 1
            priority_fee_medium = 2
            priority_fee_high = 3

            def get_max_fee(self, priority):
                return 10

        return _Gas()

    def get_nonce(self, address):
        return self._nonce

    def send_transaction(self, signed_tx):
        return "0x123"

    def wait_for_receipt(self, tx_hash, timeout=120):
        return TransactionReceipt(
            tx_hash=tx_hash,
            block_number=1,
            status=True,
            gas_used=21000,
            effective_gas_price=1,
            logs=[],
        )


def test_builder_requires_fields():
    builder = TransactionBuilder(
        _FakeClient(),
        _FakeWallet("0x000000000000000000000000000000000000dead"),
    )
    with pytest.raises(ValueError, match="to address is required"):
        builder.build()


def test_builder_builds_and_sends():
    client = _FakeClient()
    wallet = _FakeWallet("0x000000000000000000000000000000000000dead")
    tx_hash = (
        TransactionBuilder(client, wallet)
        .to(Address.from_string("0x000000000000000000000000000000000000dead"))
        .value(TokenAmount.from_human("0.1", 18, "ETH"))
        .data(b"")
        .with_gas_estimate()
        .with_gas_price("medium")
        .send()
    )
    assert tx_hash == "0x123"


def test_builder_gas_estimate_buffer():
    client = _FakeClient()
    wallet = _FakeWallet("0x000000000000000000000000000000000000dead")
    builder = (
        TransactionBuilder(client, wallet)
        .to(Address.from_string("0x000000000000000000000000000000000000dead"))
        .value(TokenAmount.from_human("0.1", 18, "ETH"))
        .data(b"")
        .with_gas_estimate(buffer=1.5)
    )
    with pytest.raises(ValueError, match="max_fee_per_gas is required"):
        builder.build()

    tx = builder.with_gas_price("medium").build()
    assert tx.gas_limit == 31500
