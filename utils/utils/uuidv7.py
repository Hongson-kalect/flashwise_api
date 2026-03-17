# utils/uuid_utils.py
import uuid
import warnings

def generate_uuid7():
    """
    Try to produce a UUIDv7 (if a supporting library is installed).
    Fallback: uuid.uuid4() (NOT sortable like uuid7).
    To get real uuid7, install a library such as `uuid6` (pip install uuid6)
    or any package that exposes uuid7() function.
    """
    # Preferred: use a library that supports uuid7
    try:
        # example: the 'uuid6' package exposes uuid7()
        import uuid6  # type: ignore
        return uuid6.uuid7()
    except Exception:
        pass

    try:
        # some packages named 'uuid7' or 'rfc4122' may exist:
        import uuid7  # type: ignore
        if hasattr(uuid7, "uuid7"):
            return uuid7.uuid7()
    except Exception:
        pass

    # Fallback: uuid4 (not uuid7). WARN the developer.
    warnings.warn(
        "No uuid7 implementation found. Falling back to uuid4. "
        "Install a uuid7-capable package (e.g. pip install uuid6) to get uuid7."
    )
    return uuid.uuid4()
