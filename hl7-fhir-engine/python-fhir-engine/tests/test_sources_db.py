"""Integration tests against the seeded databases (skipped without DATABASE_URL)."""
import collections

import pytest
from sqlalchemy import text

import systems as S
from conftest import NEEDS_DB, conformance_problems

pytestmark = NEEDS_DB


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def _all_bundles(client):
    for src in [s["name"] for s in client.get("/sources").json()["database"]]:
        for oid in client.get(f"/sources/{src}/orders", params={"limit": 5000}).json()["orders"]:
            yield src, oid, client.get(f"/fhir/bundle/{src}/order/{oid}").json()


def test_every_bundle_is_clean(client):
    n = 0
    for src, oid, b in _all_bundles(client):
        assert conformance_problems(b) == [], (src, oid)
        n += 1
    assert n >= 33


def test_imaging_only_where_imaging(client):
    for src, oid, b in _all_bundles(client):
        types = collections.Counter(e["resource"]["resourceType"] for e in b["entry"])
        sr = next(e["resource"] for e in b["entry"] if e["resource"]["resourceType"] == "ServiceRequest")
        cat = sr["category"][0]["coding"][0]["code"] if "category" in sr else None
        if types["ImagingStudy"]:
            assert cat == "363679005", oid


def test_results_carry_standard_codes(client):
    seen = collections.Counter()
    for src, oid, b in _all_bundles(client):
        for e in b["entry"]:
            r = e["resource"]
            if r["resourceType"] != "Observation" or "hasMember" in r:
                continue
            seen["results"] += 1
            if any(c["system"] == S.LOINC for c in r["code"]["coding"]):
                seen["loinc"] += 1
            if "valueQuantity" in r:
                seen["qty"] += 1
                seen["ucum"] += r["valueQuantity"].get("system") == S.UCUM
            if "valueCodeableConcept" in r:
                seen["coded"] += 1
                seen["snomed"] += any(c["system"] == S.SNOMED for c in r["valueCodeableConcept"]["coding"])
    assert seen["results"] > 0
    assert seen["loinc"] == seen["results"] and seen["ucum"] == seen["qty"] and seen["snomed"] == seen["coded"], seen


def test_bundles_are_deterministic(client):
    a = client.get("/fhir/bundle/sil/order/PED-2024-00002").json()
    b = client.get("/fhir/bundle/sil/order/PED-2024-00002").json()
    assert [e["fullUrl"] for e in a["entry"]] == [e["fullUrl"] for e in b["entry"]]


def test_engine_cannot_write_to_sil():
    """R1: the engine's own connection is refused any write (role + read-only transaction)."""
    from database import SessionLocal
    with SessionLocal() as db:
        with pytest.raises(Exception, match="read-only|permission denied"):
            db.execute(text("UPDATE sil.exames SET verificado = verificado"))
    # Second guard: even with read-only transactions switched off, the role lacks the privilege.
    from database import engine
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SET default_transaction_read_only = off"))
        with pytest.raises(Exception, match="permission denied"):
            conn.execute(text("INSERT INTO sil.dpedidos (numpedido) VALUES ('TEST-ONLY')"))
