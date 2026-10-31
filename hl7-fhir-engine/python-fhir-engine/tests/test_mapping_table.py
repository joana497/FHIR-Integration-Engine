"""The H2 mapping table (tools/sil_mapping.py) must describe the schema and the adapter exactly."""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "tools" / "sil_mapping.py").exists():  # e.g. inside the engine's container
    pytest.skip("tools/ not available", allow_module_level=True)
sys.path.insert(0, str(ROOT / "tools"))

from sil_mapping import TABLES, USED  # noqa: E402

from adapters.sil import COLUMNS_READ  # noqa: E402

STATUSES = USED | {"not mapped"}
REASONS = {"duplicate", "workflow", "scheduling", "billing", "encounter", "audit/IT", "unknown", "excluded", "candidate"}


def _schema():
    ddl = (ROOT / "postgres" / "01_schema.sql").read_text(encoding="utf-8")
    out = {}
    for name, body in re.findall(r"CREATE TABLE sil\.(\w+) \((.*?)\n\);", ddl, re.S):
        out[name] = [m.group(1) for line in body.splitlines()
                     if (m := re.match(r"\s*([a-z_]+)\s+[A-Z]", line))]
    return out


def test_every_schema_column_appears_exactly_once():
    schema = _schema()
    assert set(schema) == set(TABLES)
    for table, cols in schema.items():
        listed = [e[0] for e in TABLES[table]]
        assert sorted(listed) == sorted(cols), table
        assert len(listed) == len(set(listed)), table


def test_used_columns_are_exactly_the_columns_the_adapter_reads():
    for table, entries in TABLES.items():
        used = {e[0] for e in entries if e[1] in USED}
        assert used == COLUMNS_READ[table], (table, used ^ COLUMNS_READ[table])


def test_vocabulary():
    for table, entries in TABLES.items():
        for col, status, target, rule, reason, _note in entries:
            assert status in STATUSES, (table, col, status)
            if status == "not mapped":
                assert reason in REASONS, (table, col, reason)
                assert not target, (table, col)
            else:
                assert not reason, (table, col)
                assert rule, (table, col)
