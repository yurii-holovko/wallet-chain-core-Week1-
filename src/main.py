"""CLI entrypoint for wallet manager."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.base_types import Address, TokenAmount, TransactionReceipt, TransactionRequest
from core.serializer import CanonicalSerializer
from core.wallet_manager import WalletManager


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Wallet manager CLI")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("address", help="Print wallet address from PRIVATE_KEY")
    subparsers.add_parser("generate", help="Generate a new wallet")

    sign_message = subparsers.add_parser("sign-message", help="Sign a message")
    sign_message.add_argument("message", help="Message to sign")

    sign_typed = subparsers.add_parser(
        "sign-typed-data", help="Sign EIP-712 typed data from JSON files"
    )
    sign_typed.add_argument("--domain", required=True, help="Path to domain JSON file")
    sign_typed.add_argument("--types", required=True, help="Path to types JSON file")
    sign_typed.add_argument("--value", required=True, help="Path to value JSON file")

    sign_tx = subparsers.add_parser(
        "sign-transaction", help="Sign a transaction dict from JSON file"
    )
    sign_tx.add_argument("--tx", required=True, help="Path to transaction JSON file")

    export_keyfile = subparsers.add_parser(
        "keyfile-export", help="Export encrypted keyfile"
    )
    export_keyfile.add_argument("--path", required=True, help="Output keyfile path")
    export_keyfile.add_argument("--password", required=True, help="Keyfile password")

    import_keyfile = subparsers.add_parser(
        "keyfile-import", help="Import encrypted keyfile"
    )
    import_keyfile.add_argument("--path", required=True, help="Input keyfile path")
    import_keyfile.add_argument("--password", required=True, help="Keyfile password")

    serialize = subparsers.add_parser("serialize", help="Canonical serialize JSON file")
    serialize.add_argument("--input", required=True, help="Path to JSON file")

    hash_payload = subparsers.add_parser(
        "hash", help="Keccak256 hash of canonical JSON file"
    )
    hash_payload.add_argument("--input", required=True, help="Path to JSON file")

    verify = subparsers.add_parser(
        "verify-determinism", help="Verify deterministic serialization"
    )
    verify.add_argument("--input", required=True, help="Path to JSON file")
    verify.add_argument(
        "--iterations", type=int, default=100, help="Number of iterations"
    )

    build_tx = subparsers.add_parser(
        "build-transaction", help="Build transaction dict from JSON spec"
    )
    build_tx.add_argument("--input", required=True, help="Path to JSON file")

    receipt_fee = subparsers.add_parser(
        "receipt-fee", help="Compute tx fee from receipt JSON"
    )
    receipt_fee.add_argument("--input", required=True, help="Path to JSON file")

    parser.set_defaults(command="address")
    return parser


def main() -> None:
    """Entrypoint for `make run`."""
    parser = _build_parser()
    args = parser.parse_args()

    try:
        if args.command == "generate":
            wallet = WalletManager.generate()
            print(wallet.address)
            return

        if args.command == "keyfile-import":
            wallet = WalletManager.from_keyfile(args.path, args.password)
            print(wallet.address)
            return

        wallet = WalletManager.from_env()

        if args.command == "sign-message":
            signed = wallet.sign_message(args.message)
            print(signed.signature.hex())
            return

        if args.command == "sign-typed-data":
            domain = _load_json_object(args.domain)
            types = _load_json_object(args.types)
            value = _load_json_object(args.value)
            signed = wallet.sign_typed_data(domain=domain, types=types, value=value)
            print(signed.signature.hex())
            return

        if args.command == "sign-transaction":
            tx = _load_json_object(args.tx)
            _validate_transaction_fields(tx)
            normalized_tx = _normalize_transaction_dict(tx)
            signed = wallet.sign_transaction(normalized_tx)
            print(signed.raw_transaction.hex())
            return

        if args.command == "keyfile-export":
            wallet.to_keyfile(args.path, args.password)
            print(f"Keyfile written to {args.path}")
            return

        if args.command == "serialize":
            payload = _load_json_value(args.input)
            print(CanonicalSerializer.serialize(payload).decode("utf-8"))
            return

        if args.command == "hash":
            payload = _load_json_value(args.input)
            print(CanonicalSerializer.hash(payload).hex())
            return

        if args.command == "verify-determinism":
            payload = _load_json_value(args.input)
            ok = CanonicalSerializer.verify_determinism(
                payload, iterations=args.iterations
            )
            print("ok" if ok else "not deterministic")
            return

        if args.command == "build-transaction":
            spec = _load_json_object(args.input)
            tx_request = _transaction_request_from_spec(spec)
            print(
                json.dumps(
                    tx_request.to_dict(),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return

        if args.command == "receipt-fee":
            receipt_data = _load_json_object(args.input)
            receipt = TransactionReceipt.from_web3(receipt_data)
            fee = receipt.tx_fee
            print(f"{fee.human} {fee.symbol}")
            return

        print(wallet.address)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)


def _load_json_value(path: str) -> object:
    try:
        payload = Path(path).read_text(encoding="utf-8")
        data = json.loads(payload)
    except FileNotFoundError as exc:
        raise ValueError(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}") from exc
    return data


def _load_json_object(path: str) -> dict:
    data = _load_json_value(path)
    if not isinstance(data, dict):
        raise ValueError(f"JSON in {path} must be an object")
    return data


def _parse_hex_data(value: object) -> bytes:
    if value is None:
        return b""
    if not isinstance(value, str):
        raise ValueError("data must be a hex string")
    normalized = value[2:] if value.startswith("0x") else value
    if normalized == "":
        return b""
    try:
        return bytes.fromhex(normalized)
    except ValueError as exc:
        raise ValueError("data must be valid hex") from exc


def _transaction_request_from_spec(spec: dict) -> TransactionRequest:
    if "to" not in spec:
        raise ValueError("transaction spec missing 'to'")
    if "decimals" not in spec:
        raise ValueError("transaction spec missing 'decimals'")

    decimals = _require_int(spec, "decimals")
    symbol = spec.get("symbol")

    if "value_raw" in spec:
        value_raw = _require_int(spec, "value_raw")
        value = TokenAmount(raw=value_raw, decimals=decimals, symbol=symbol)
    elif "value_human" in spec:
        value = TokenAmount.from_human(
            amount=spec["value_human"], decimals=decimals, symbol=symbol
        )
    else:
        raise ValueError("transaction spec missing 'value_raw' or 'value_human'")

    return TransactionRequest(
        to=Address.from_string(spec["to"]),
        value=value,
        data=_parse_hex_data(spec.get("data")),
        nonce=_optional_int(spec.get("nonce")),
        gas_limit=_optional_int(spec.get("gas_limit")),
        max_fee_per_gas=_optional_int(spec.get("max_fee_per_gas")),
        max_priority_fee=_optional_int(spec.get("max_priority_fee")),
        chain_id=_optional_int(spec.get("chain_id")) or 1,
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _to_int(value)


def _require_int(spec: dict, key: str) -> int:
    if key not in spec:
        raise ValueError(f"transaction spec missing '{key}'")
    return _to_int(spec[key])


def _to_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    raise ValueError("Expected integer-like value")


def _normalize_transaction_dict(tx: dict) -> dict:
    normalized = dict(tx)
    if "to" in normalized and normalized["to"] is not None:
        if not isinstance(normalized["to"], str):
            raise ValueError("tx.to must be a hex address string or null")
        normalized["to"] = Address.from_string(normalized["to"]).checksum
    for key in (
        "value",
        "nonce",
        "gas",
        "gasPrice",
        "chainId",
        "maxFeePerGas",
        "maxPriorityFeePerGas",
    ):
        if key in normalized and normalized[key] is not None:
            normalized[key] = _parse_int_field(normalized[key], key)
    return normalized


def _parse_int_field(value: object, field: str) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value)
        except ValueError as exc:
            raise ValueError(
                f"tx.{field} must be an integer or 0x-prefixed hex string"
            ) from exc
    raise ValueError(f"tx.{field} must be an integer or 0x-prefixed hex string")


def _validate_transaction_fields(tx: dict) -> None:
    required = {"nonce", "gas", "value", "data"}
    missing = sorted(key for key in required if key not in tx)
    if missing:
        raise ValueError(f"tx missing required fields: {', '.join(missing)}")

    has_legacy_fee = "gasPrice" in tx
    has_dynamic_fee = "maxFeePerGas" in tx and "maxPriorityFeePerGas" in tx
    if not has_legacy_fee and not has_dynamic_fee:
        raise ValueError(
            "tx must include gasPrice or both maxFeePerGas and maxPriorityFeePerGas"
        )

    if "chainId" not in tx:
        raise ValueError("tx missing required field: chainId")


if __name__ == "__main__":
    main()
