"""Consistent ISO timestamp parsing on all supported Python versions."""
from datetime import datetime
import re


def parse_timestamp(value: str) -> datetime:
    value = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", value)
    value = re.sub(r"[zZ]$", "+00:00", value)
    value = re.sub(r"(\d\d:\d\d:\d\d)\.(\d{1,9})(?=Z|[+-]\d\d:\d\d|$)",
                   lambda m: m[1] + "." + (m[2] + "000000")[:6], value)
    return datetime.fromisoformat(value)
