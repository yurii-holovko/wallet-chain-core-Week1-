"""Chain-specific exceptions for RPC and transaction failures."""

from __future__ import annotations

from typing import Optional

from core.base_types import TransactionReceipt


class ChainError(Exception):
    """Base class for chain errors."""


class RPCError(ChainError):
    """RPC request failed."""

    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        data: Optional[object] = None,
    ):
        self.code = code
        self.data = data
        super().__init__(message)


class TransactionFailed(ChainError):
    """Transaction reverted."""

    def __init__(self, tx_hash: str, receipt: TransactionReceipt):
        self.tx_hash = tx_hash
        self.receipt = receipt
        super().__init__(f"Transaction {tx_hash} reverted")


class InsufficientFunds(ChainError):
    """Not enough balance for transaction."""


class NonceTooLow(ChainError):
    """Nonce already used."""


class ReplacementUnderpriced(ChainError):
    """Replacement transaction gas too low."""
