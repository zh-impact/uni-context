"""Smoke test for UnictxError base class.

Per-module specifics (ItemNotFound, ModelNotFound, etc.) get their own
tests in their owning module's test directory.
"""

from unictx.errors import UnictxError


def test_unictx_error_is_exception_subclass():
    assert issubclass(UnictxError, Exception)


def test_unictx_error_carries_message():
    err = UnictxError("boom")
    assert str(err) == "boom"
