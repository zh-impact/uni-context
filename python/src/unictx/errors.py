"""Base exception type for all uni-context domain errors.

Module-specific errors (ItemNotFound, ModelNotFound, etc.) live in their
owning module's errors.py and inherit from UnictxError. CLI catches
UnictxError as a catch-all for unified error reporting.
"""


class UnictxError(Exception):
    """Base for all uni-context domain errors.

    Specific errors live in their owning module:
    items/errors.py:ItemNotFound, embed/errors.py:ModelNotFound, etc.
    Catch UnictxError in CLI for unified error reporting.
    """
