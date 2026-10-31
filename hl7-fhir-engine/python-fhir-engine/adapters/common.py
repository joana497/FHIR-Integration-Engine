"""common.py - small parsing helpers shared by more than one adapter."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional


def dec(s) -> Optional[Decimal]:
    try:
        return Decimal(str(s).strip())
    except (InvalidOperation, TypeError, AttributeError):
        return None


def parse_ref_range(rr: Optional[str]):
    """'13.0-17.0' -> (13.0, 17.0); '<5' -> (None, 5); '>40' -> (40, None); otherwise (None, None)."""
    if not rr:
        return None, None
    s = rr.replace(" ", "")
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)", s)
    if m:
        return dec(m.group(1)), dec(m.group(2))
    m = re.fullmatch(r"<=?(-?\d+(?:\.\d+)?)", s)
    if m:
        return None, dec(m.group(1))
    m = re.fullmatch(r">=?(-?\d+(?:\.\d+)?)", s)
    if m:
        return dec(m.group(1)), None
    return None, None
