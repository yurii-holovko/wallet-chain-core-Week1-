from decimal import Decimal

import pytest

from core.base_types import Address, TokenAmount


def test_address_invalid_raises():
    with pytest.raises(ValueError, match="Invalid Ethereum address"):
        Address("invalid")


def test_address_case_insensitive_equality():
    lower = Address("0x000000000000000000000000000000000000dead")
    upper = Address("0x000000000000000000000000000000000000DEAD")
    assert lower == upper


def test_token_amount_from_human_raw():
    amount = TokenAmount.from_human("1.5", 18)
    assert amount.raw == 1_500_000_000_000_000_000


def test_token_amount_add_mismatched_decimals():
    a = TokenAmount(raw=1, decimals=18)
    b = TokenAmount(raw=1, decimals=6)
    with pytest.raises(ValueError, match="decimals must match"):
        _ = a + b


def test_token_amount_rejects_float_input():
    with pytest.raises(TypeError, match="not float"):
        TokenAmount.from_human(1.5, 18)


def test_token_amount_mul_decimal_no_float():
    amount = TokenAmount.from_human(Decimal("2"), 18)
    result = amount * Decimal("2")
    assert result.raw == 4 * 10**18
