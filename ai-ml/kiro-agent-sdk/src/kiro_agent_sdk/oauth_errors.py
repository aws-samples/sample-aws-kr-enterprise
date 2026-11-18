"""Keep credential-bearing transport exceptions out of public OAuth errors."""
from __future__ import annotations

from typing import Any, Optional


async def sanitized_oauth(operation: Any, message: str, error_type: Any = ValueError, *,
                          safe_errors: Optional[dict[str, type]] = None) -> Any:
    try:
        return await operation
    except StopAsyncIteration:
        raise
    except Exception as error:
        # Only exact, caller-declared constant validation messages may survive.
        # Reconstruct the exception outside except; never retain its frames.
        if safe_errors is not None:
            text = str(error)
            selected = safe_errors.get(text)
            if selected is type(error):
                message, error_type = text, selected
            text = ""
    finally:
        operation = None
    # Raising outside except removes __context__, unlike `from None`.
    raise error_type(message)
