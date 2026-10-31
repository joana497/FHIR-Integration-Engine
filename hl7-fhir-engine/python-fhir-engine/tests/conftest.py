import os
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
ROOT = ENGINE.parent
sys.path.insert(0, str(ENGINE))

NEEDS_DB = pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="set DATABASE_URL (and LISB_DATABASE_URL) to run")


def walk(node, path=""):
    """Yield (path, key, value) for every key in a JSON tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield path, k, v
            yield from walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")


def conformance_problems(bundle: dict) -> list:
    """The checks that caught the four historical defects, plus reference integrity."""
    import re
    probs = []
    ids = {f"{e['resource']['resourceType']}/{e['resource']['id']}" for e in bundle["entry"]}
    for path, k, v in walk(bundle):
        if v is None:
            probs.append(f"null at {path}.{k}")
        elif isinstance(v, (list, dict, str)) and len(v) == 0:
            probs.append(f"empty {type(v).__name__} at {path}.{k}")
        if k == "issued" and isinstance(v, str) and not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?([+-]\d\d:\d\d|Z)", v):
            probs.append(f"bad instant {v!r} at {path}")
        # FHIR dateTime/instant: a value with a time must carry a time zone (any element)
        if isinstance(v, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d(\.\d+)?)?", v):
            probs.append(f"time without time zone {v!r} at {path}.{k}")
        if k == "reference" and isinstance(v, str) and v not in ids:
            probs.append(f"dangling reference {v} at {path}")
    for e in bundle["entry"]:
        r = e["resource"]
        if r["resourceType"] == "ImagingStudy":
            for m in r["modality"]:
                if m["code"] not in {"DX", "CR", "CT", "MR", "US", "MG", "NM", "XA"}:
                    probs.append(f"non-DICOM modality {m['code']}")
        if "text" not in r or not r["text"]["div"].startswith('<div xmlns="http://www.w3.org/1999/xhtml">'):
            probs.append(f"no narrative on {r['resourceType']}/{r['id']}")
    return probs
