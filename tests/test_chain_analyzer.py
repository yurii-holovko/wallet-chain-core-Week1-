from eth_abi import encode

from chain import analyzer


def test_decode_function_transfer():
    to_addr = "0x000000000000000000000000000000000000dEaD"
    amount = 123
    selector = analyzer._selector_hash("transfer(address,uint256)")
    data = encode(["address", "uint256"], [to_addr, amount])
    calldata = selector + data.hex()
    decoded = analyzer._decode_function(calldata)
    assert decoded is not None
    assert decoded.name.startswith("transfer(")
    assert decoded.args[0][0] == "to"
    assert decoded.args[1][0] == "amount"


def test_extract_transfers_with_token_cache():
    logs = [
        {
            "address": "0x000000000000000000000000000000000000dEaD",
            "topics": [
                f"0x{analyzer.TRANSFER_TOPIC}",
                "0x" + ("00" * 12) + "000000000000000000000000000000000000beef",
                "0x" + ("00" * 12) + "000000000000000000000000000000000000dead",
            ],
            "data": "0x01",
        }
    ]

    class _Cache:
        def get(self, token):
            return ("TOK", 0)

    transfers = analyzer._extract_transfers(logs, _Cache())
    assert transfers[0]["value"] == "1 TOK"


def test_decode_revert_reason():
    data = "0x08c379a0" + encode(["string"], ["boom"]).hex()
    assert analyzer._decode_revert_reason(data) == "boom"


def test_invalid_hash_detection():
    assert analyzer._is_valid_hash("0x123") is False
