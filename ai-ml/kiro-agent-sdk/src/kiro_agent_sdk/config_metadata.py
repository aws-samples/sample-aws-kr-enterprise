"""Non-executing annotations shared by explicit JSON configuration readers."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


def without_schema_annotation(value: dict[str, Any]) -> dict[str, Any]:
    if "$schema" not in value:
        return value
    annotation = value["$schema"]
    try:
        if (type(annotation) is not str or not annotation or len(annotation) > 2048 or
                any(ord(char) <= 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in annotation)):
            raise ValueError()
        # $schema is an annotation, never a source to fetch or execute.
        # Both URI references and relative local schema paths are accepted.
        parsed = urlsplit(annotation)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError()
        _ = parsed.port
    except (ValueError, UnicodeError):
        raise ValueError("Settings schema annotation must be a bounded URI reference") from None
    return {key: item for key, item in value.items() if key != "$schema"}
