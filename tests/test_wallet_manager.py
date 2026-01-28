import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils.address import to_checksum_address

from core.wallet_manager import WalletManager, _mask_private_key


def test_repr_and_str_do_not_expose_private_key():
    account = Account.create()
    wallet = WalletManager(account.key)
    key_hex = account.key.hex()

    assert key_hex not in repr(wallet)
    assert key_hex not in str(wallet)
    assert "WalletManager(address=" in repr(wallet)


def test_invalid_private_key_is_masked(monkeypatch):
    bad_key = "0x" + ("a" * 12)
    masked = _mask_private_key(bad_key)
    monkeypatch.setenv("PRIVATE_KEY", bad_key)

    with pytest.raises(ValueError) as exc:
        WalletManager.from_env()

    message = str(exc.value)
    assert bad_key not in message
    assert masked in message


def test_sign_message_rejects_empty_message():
    wallet = WalletManager(Account.create().key)
    with pytest.raises(ValueError, match="must not be empty"):
        wallet.sign_message("")


def test_sign_typed_data_rejects_invalid_types_before_crypto(monkeypatch):
    wallet = WalletManager(Account.create().key)

    def fail_encode(*args, **kwargs):
        raise AssertionError("encode_typed_data should not be called")

    monkeypatch.setattr("core.wallet_manager.encode_typed_data", fail_encode)

    with pytest.raises(TypeError, match="types must be a non-empty dict"):
        wallet.sign_typed_data(domain={}, types=[], value={})  # type: ignore[arg-type]


def test_sign_message_recovery_matches_address():
    wallet = WalletManager(Account.create().key)
    message = "hello from tests"

    signed = wallet.sign_message(message)
    recovered = Account.recover_message(
        encode_defunct(text=message), signature=signed.signature
    )

    assert recovered.lower() == wallet.address.lower()


def test_sign_transaction_recovery_matches_address():
    wallet = WalletManager(Account.create().key)
    tx = {
        "nonce": 0,
        "to": to_checksum_address("0x" + "1" * 40),
        "value": 123,
        "gas": 21000,
        "gasPrice": 1,
        "data": b"",
        "chainId": 1,
    }

    signed = wallet.sign_transaction(tx)
    recovered = Account.recover_transaction(signed.raw_transaction)

    assert recovered.lower() == wallet.address.lower()
