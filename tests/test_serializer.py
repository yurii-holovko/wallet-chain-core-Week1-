import pytest

from core.serializer import CanonicalSerializer


def test_nested_objects_sorted_keys():
    obj = {"b": 1, "a": {"d": 4, "c": 3}}
    serialized = CanonicalSerializer.serialize(obj)
    assert serialized == b'{"a":{"c":3,"d":4},"b":1}'


def test_unicode_strings():
    obj = {"emoji": "🚀", "word": "Привіт"}
    serialized = CanonicalSerializer.serialize(obj)
    assert b'"emoji":"\xf0\x9f\x9a\x80"' in serialized
    assert b'"word":"\xd0\x9f\xd1\x80\xd0\xb8\xd0\xb2\xd1\x96\xd1\x82"' in serialized


def test_large_integers():
    obj = {"value": 2**80}
    serialized = CanonicalSerializer.serialize(obj)
    assert serialized == f'{{"value":{2**80}}}'.encode("utf-8")


def test_none_values():
    obj = {"value": None}
    serialized = CanonicalSerializer.serialize(obj)
    assert serialized == b'{"value":null}'


def test_empty_objects_and_arrays():
    assert CanonicalSerializer.serialize({}) == b"{}"
    assert CanonicalSerializer.serialize([]) == b"[]"


def test_floats_are_rejected():
    with pytest.raises(ValueError, match="Floating point"):
        CanonicalSerializer.serialize({"value": 1.23})


def test_verify_determinism_true():
    obj = {"b": [1, 2, 3], "a": {"x": "y"}}
    assert CanonicalSerializer.verify_determinism(obj, iterations=10) is True


def test_verify_determinism_rejects_zero_iterations():
    with pytest.raises(ValueError, match="iterations"):
        CanonicalSerializer.verify_determinism({"a": 1}, iterations=0)
