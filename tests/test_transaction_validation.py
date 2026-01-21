import pytest

from main import _normalize_transaction_dict, _validate_transaction_fields


def test_validate_transaction_fields_missing_required():
    tx = {"gas": 21000, "value": 0, "data": "0x"}
    with pytest.raises(ValueError, match="missing required fields"):
        _validate_transaction_fields(tx)


def test_validate_transaction_fields_missing_fee_fields():
    tx = {"nonce": 0, "gas": 21000, "value": 0, "data": "0x", "chainId": 1}
    with pytest.raises(ValueError, match="gasPrice or both maxFeePerGas"):
        _validate_transaction_fields(tx)


def test_validate_transaction_fields_accepts_legacy_fee():
    tx = {
        "nonce": 0,
        "gas": 21000,
        "value": 0,
        "data": "0x",
        "gasPrice": 1_000_000_000,
        "chainId": 1,
    }
    _validate_transaction_fields(tx)


def test_validate_transaction_fields_accepts_dynamic_fee():
    tx = {
        "nonce": 0,
        "gas": 21000,
        "value": 0,
        "data": "0x",
        "maxFeePerGas": 1_000_000_000,
        "maxPriorityFeePerGas": 100_000_000,
        "chainId": 1,
    }
    _validate_transaction_fields(tx)


def test_normalize_transaction_dict_parses_int_fields_and_checksum():
    tx = {
        "to": "0x000000000000000000000000000000000000dead",
        "nonce": "0x1",
        "gas": "21000",
        "value": "0",
        "data": "0x",
        "gasPrice": "0x3b9aca00",
        "chainId": "0x1",
    }
    normalized = _normalize_transaction_dict(tx)
    assert normalized["to"] == "0x000000000000000000000000000000000000dEaD"
    assert normalized["nonce"] == 1
    assert normalized["gas"] == 21000
    assert normalized["value"] == 0
    assert normalized["gasPrice"] == 1_000_000_000
    assert normalized["chainId"] == 1
