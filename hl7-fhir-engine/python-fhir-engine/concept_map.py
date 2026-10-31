"""
concept_map.py - runtime translation of local codes through a FHIR ConceptMap.

The engine loads conceptmaps/lab-catalogue.json (built from CPAL by
tools/build_conceptmap.py) and uses it in the mappers to add standard
codings next to the local ones:

  local test/parameter code  -> LOINC        (Observation.code)
  local unit string          -> UCUM         (Observation.valueQuantity)
  local qualitative value    -> SNOMED CT    (Observation.valueCodeableConcept)
  national MCDT code         -> SNOMED CT    (ordered-test Observation.code)

Only targets with equivalence 'equivalent' or 'equal' are applied
automatically; anything weaker would need a human decision.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from canonical import Coding

DEFAULT_PATH = Path(__file__).parent / "conceptmaps" / "lab-catalogue.json"
AUTO_EQUIVALENCES = {"equivalent", "equal"}


class ConceptMapIndex:
    def __init__(self, resource: dict):
        self.url = resource.get("url")
        self.version = resource.get("version")
        self._index: dict[tuple[str, str], list[tuple[str, str, Optional[str]]]] = {}
        for g in resource.get("group", []):
            src, tgt = g.get("source"), g.get("target")
            for el in g.get("element", []):
                for t in el.get("target", []):
                    if t.get("equivalence") in AUTO_EQUIVALENCES:
                        self._index.setdefault((src, el["code"]), []).append((tgt, t["code"], t.get("display")))

    def translate(self, coding: Coding, target_system: Optional[str] = None) -> list[Coding]:
        """Standard codings equivalent to a local coding (empty if unmapped)."""
        out = []
        for tgt, code, display in self._index.get((coding.system, coding.code), []):
            if target_system is None or tgt == target_system:
                out.append(Coding(tgt, code, display))
        return out

    def first(self, coding: Optional[Coding], target_system: str) -> Optional[Coding]:
        if coding is None:
            return None
        hits = self.translate(coding, target_system)
        return hits[0] if hits else None

    def size(self) -> int:
        return len(self._index)


_cached: Optional[ConceptMapIndex] = None


def load(path: Optional[str] = None) -> ConceptMapIndex:
    """Load (once) the ConceptMap named by CONCEPTMAP_PATH or the default file."""
    global _cached
    if _cached is None or path is not None:
        p = Path(path or os.getenv("CONCEPTMAP_PATH") or DEFAULT_PATH)
        _cached = ConceptMapIndex(json.loads(p.read_text(encoding="utf-8")))
    return _cached
