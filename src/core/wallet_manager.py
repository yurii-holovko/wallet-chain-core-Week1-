"""Wallet management with secure key handling and signing helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.datastructures import SignedMessage, SignedTransaction
from eth_account.messages import encode_defunct, encode_typed_data
from eth_utils.address import to_checksum_address


def _mask_private_key(private_key: Any) -> str:
    if isinstance(private_key, (bytes, bytearray)):
        raw = private_key.hex()
    else:
        raw = str(private_key)

    if raw.startswith("0x"):
        raw = raw[2:]

    if len(raw) < 10:
        return "<redacted>"

    return f"0x{raw[:6]}...{raw[-4:]}"


def _validate_eip712_types(types: dict) -> None:
    if not isinstance(types, dict) or not types:
        raise TypeError("types must be a non-empty dict")

    for type_name, fields in types.items():
        if not isinstance(type_name, str) or not type_name:
            raise TypeError("types keys must be non-empty strings")
        if not isinstance(fields, list) or not fields:
            raise TypeError("types values must be non-empty lists")
        for field in fields:
            if not isinstance(field, dict):
                raise TypeError("each field must be a dict with name/type")
            if "name" not in field or "type" not in field:
                raise TypeError("each field must include name and type")


class WalletManager:
    """
    Manages wallet operations: key loading, signing, verification.

    Keys can be loaded from:
    - Environment variable
    - Encrypted keyfile (optional stretch goal)

    CRITICAL: Private key must never appear in logs, errors, or string
    representations.
    """

    def __init__(self, private_key: str | bytes) -> None:
        try:
            self._account = Account.from_key(private_key)
        except Exception as exc:  # pragma: no cover - defensive
            masked = _mask_private_key(private_key)
            raise ValueError(f"Invalid private key: {masked}") from exc

    @classmethod
    def from_env(cls, env_var: str = "PRIVATE_KEY") -> "WalletManager":
        """Load private key from environment variable."""
        value = os.environ.get(env_var)
        if not value:
            raise ValueError(f"Environment variable {env_var} is not set")
        return cls(value)

    @classmethod
    def from_keyfile(cls, path: str, password: str) -> "WalletManager":
        """Load from encrypted keyfile."""
        payload = Path(path).read_text(encoding="utf-8")
        data = json.loads(payload)
        try:
            private_key = Account.decrypt(data, password)
        except Exception as exc:
            raise ValueError("Failed to decrypt keyfile") from exc
        return cls(private_key)

    @classmethod
    def generate(cls) -> "WalletManager":
        """Generate a new random wallet and display the key once."""
        account = Account.create()
        print(f"PRIVATE KEY (save securely): {account.key.hex()}")
        return cls(account.key)

    @property
    def address(self) -> str:
        """Returns checksummed address."""
        return to_checksum_address(self._account.address)

    def sign_message(self, message: str) -> SignedMessage:
        """Sign an arbitrary message (with EIP-191 prefix)."""
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        if message == "":
            raise ValueError("message must not be empty")
        signable = encode_defunct(text=message)
        return self._account.sign_message(signable)

    def sign_typed_data(self, domain: dict, types: dict, value: dict) -> SignedMessage:
        """Sign EIP-712 typed data (used by many DeFi protocols)."""
        if not isinstance(domain, dict):
            raise TypeError("domain must be a dict")
        if not isinstance(value, dict):
            raise TypeError("value must be a dict")
        _validate_eip712_types(types)
        signable = encode_typed_data(
            domain_data=domain, message_types=types, message_data=value
        )
        return self._account.sign_message(signable)

    def sign_transaction(self, tx: dict) -> SignedTransaction:
        """Sign a transaction dict."""
        if not isinstance(tx, dict):
            raise TypeError("tx must be a dict")
        if not tx:
            raise ValueError("tx must not be empty")
        return self._account.sign_transaction(tx)

    def to_keyfile(self, path: str, password: str) -> None:
        """Export to encrypted keyfile."""
        data = Account.encrypt(self._account.key, password)
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")

    def __repr__(self) -> str:
        """MUST NOT expose private key."""
        return f"WalletManager(address={self.address})"

    def __str__(self) -> str:
        return self.__repr__()
