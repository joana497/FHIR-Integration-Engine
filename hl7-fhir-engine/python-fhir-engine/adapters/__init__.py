"""
adapters - registry of source adapters.

Adding a source = writing one module that returns canonical.OrderRec
objects and registering it below (scripts/adapter_line_counts.py
measures exactly that).
"""
from __future__ import annotations

import os

from .oru_r01 import OruR01Adapter
from .sil import SilAdapter

DATABASE_ADAPTERS = {"sil": SilAdapter()}
if os.getenv("LISB_DATABASE_URL"):
    from .lisb import LisbAdapter
    DATABASE_ADAPTERS["lisb"] = LisbAdapter(os.environ["LISB_DATABASE_URL"])

MESSAGE_ADAPTERS = {"oru": OruR01Adapter()}
