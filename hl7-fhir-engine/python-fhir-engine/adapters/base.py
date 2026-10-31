"""
base.py - the contract every source adapter fulfils.

Two kinds of source exist:
  * database sources (SIL, LIS-B): the engine pulls orders on request;
  * message sources (HL7 v2 ORU^R01): the source pushes messages.
Both produce canonical OrderRec objects; nothing downstream of the
adapter knows which kind of source an order came from.
"""
from __future__ import annotations

from typing import Optional, Protocol

from canonical import OrderRec


class DatabaseAdapter(Protocol):
    name: str
    description: str

    def list_orders(self, limit: int, offset: int) -> list[str]: ...
    def get_order(self, order_id: str) -> Optional[OrderRec]: ...
    def health(self) -> dict: ...


class MessageAdapter(Protocol):
    name: str
    description: str

    def parse(self, message: str) -> list[OrderRec]: ...
