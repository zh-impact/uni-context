"""Tests for items/errors.py — error hierarchy.

Verifies the three items-module errors inherit from UnictxError (so CLI
catches them collectively), carry their attribute, and format their
message as expected.
"""

import pytest

from unictx.errors import UnictxError
from unictx.items.errors import (
    ExternalizedContentMissing,
    ItemNotFound,
    ItemValidationError,
)


@pytest.mark.parametrize(
    "cls",
    [ItemNotFound, ExternalizedContentMissing, ItemValidationError],
)
def test_error_is_unictx_error_subclass(cls):
    assert issubclass(cls, UnictxError)
    assert issubclass(cls, Exception)


def test_item_not_found_carries_item_id():
    err = ItemNotFound("abc-123")
    assert err.item_id == "abc-123"
    assert str(err) == "item not found: abc-123"


def test_externalized_content_missing_carries_uri():
    err = ExternalizedContentMissing("file:///var/blob/xyz")
    assert err.uri == "file:///var/blob/xyz"
    assert str(err) == "externalized content missing: file:///var/blob/xyz"


def test_item_validation_error_carries_reason():
    err = ItemValidationError("user scope requires owner")
    assert err.reason == "user scope requires owner"
    assert str(err) == "user scope requires owner"


def test_errors_raise_and_catch_via_unictx_error():
    """CLI's `except UnictxError` must catch any of the three."""
    with pytest.raises(UnictxError):
        raise ItemNotFound("x")
    with pytest.raises(UnictxError):
        raise ExternalizedContentMissing("u")
    with pytest.raises(UnictxError):
        raise ItemValidationError("r")
