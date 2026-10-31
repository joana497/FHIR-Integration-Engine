"""
main.py - HL7 FHIR Engine (Python / FastAPI)

Reads legacy sources through adapters (adapters/), maps the canonical
records to FHIR R4 resources (fhir_mappers.py) and returns them.

This service only reads and transforms. It never writes to HAPI: every
write goes through the Node middleware, where it is authenticated,
validated against the terminology server and audited (fail-closed).
"""
from __future__ import annotations

from typing import Optional

import structlog
from fastapi import FastAPI, HTTPException, Query, Request

import fhir_mappers as M
from adapters import DATABASE_ADAPTERS, MESSAGE_ADAPTERS
from adapters.oru_r01 import HL7ParseError, OruError
from concept_map import load as load_concept_map

structlog.configure(processors=[
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.stdlib.add_log_level,
    structlog.processors.JSONRenderer(),
])
log = structlog.get_logger()

app = FastAPI(
    title="Legacy → HL7 FHIR R4 Engine",
    description="Converts legacy laboratory records (SIL, LIS-B, HL7 v2 ORU^R01) to FHIR R4 resources",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


def _adapter(source: str):
    a = DATABASE_ADAPTERS.get(source)
    if a is None:
        raise HTTPException(status_code=404, detail=f"Unknown source '{source}'")
    return a


def _order(source: str, order_id: str):
    o = _adapter(source).get_order(order_id)
    if o is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found in source '{source}'")
    return o


def _searchset(resources: list) -> dict:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in resources],
    }


# ─── System ──────────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
def health():
    """Readiness: every database source reachable and the ConceptMap loaded."""
    sources = {name: a.health() for name, a in DATABASE_ADAPTERS.items()}
    try:
        cm = load_concept_map()
        cm_info = {"ok": True, "url": cm.url, "version": cm.version, "mappings": cm.size()}
    except Exception as exc:  # noqa: BLE001
        cm_info = {"ok": False, "error": str(exc)}
    ok = all(s["ok"] for s in sources.values()) and cm_info["ok"]
    return {"status": "ok" if ok else "degraded", "service": "python-fhir-engine",
            "sources": sources, "conceptmap": cm_info}


@app.get("/sources", tags=["System"])
def sources():
    return {
        "database": [{"name": n, "description": a.description} for n, a in DATABASE_ADAPTERS.items()],
        "message": [{"name": n, "description": a.description} for n, a in MESSAGE_ADAPTERS.items()],
    }


@app.get("/sources/{source}/orders", tags=["System"])
def list_orders(source: str, limit: int = Query(50, ge=1, le=5000), offset: int = Query(0, ge=0)):
    ids = _adapter(source).list_orders(limit, offset)
    return {"source": source, "count": len(ids), "orders": ids}


@app.get("/conceptmap", tags=["Terminology"])
def conceptmap():
    """The FHIR ConceptMap the engine applies (for inspection, upload or validation)."""
    import json
    from concept_map import DEFAULT_PATH
    return json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))


# ─── Bundles ─────────────────────────────────────────────────────────────────
@app.get("/fhir/bundle/{source}/order/{order_id}", tags=["Bundles"])
def order_bundle(source: str, order_id: str):
    """Complete transaction Bundle for one order of any database source."""
    return M.build_bundle(M.build_order_resources(_order(source, order_id)))


@app.get("/fhir/bundle/order/{numpedido}", tags=["Bundles"])
def get_order_bundle(numpedido: str):
    """SIL shortcut, kept for compatibility: same as /fhir/bundle/sil/order/{numpedido}."""
    return order_bundle("sil", numpedido)


@app.post("/v2/oru/transform", tags=["HL7 v2"])
async def oru_transform(request: Request):
    """HL7 v2.5.1 ORU^R01 (ER7 text body) -> FHIR transaction Bundle.

    Stateless: nothing is stored here. The middleware calls this endpoint,
    then validates, persists and audits the Bundle, and sends the HL7 ACK.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    adapter = MESSAGE_ADAPTERS["oru"]
    try:
        orders = adapter.parse(raw)
        control_id = adapter.control_id(raw)
    except (HL7ParseError, OruError) as exc:
        log.warning("oru_rejected", error=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))
    resources = [r for o in orders for r in M.build_order_resources(o)]
    # Deduplicate (several orders of one message share the same Patient)
    seen, unique = set(), []
    for r in resources:
        k = (r["resourceType"], r["id"])
        if k not in seen:
            seen.add(k)
            unique.append(r)
    log.info("oru_transformed", control_id=control_id, orders=len(orders), resources=len(unique))
    return {"control_id": control_id, "orders": [o.identifier.value for o in orders],
            "bundle": M.build_bundle(unique)}


# ─── FHIR read endpoints (per source; default SIL) ───────────────────────────
def _page_orders(source: str, limit: int, offset: int):
    a = _adapter(source)
    return [o for o in (a.get_order(i) for i in a.list_orders(limit, offset)) if o is not None]


@app.get("/fhir/Patient", tags=["FHIR Resources"])
def list_patients(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), source: str = "sil"):
    a = _adapter(source)
    if hasattr(a, "list_patients"):
        pats = a.list_patients(limit, offset)
    else:
        pats = {M.patient_id(o.patient): o.patient for o in _page_orders(source, limit, offset)}.values()
    return _searchset([M.map_patient(p, source) for p in pats])


@app.get("/fhir/ServiceRequest", tags=["FHIR Resources"])
def list_service_requests(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), source: str = "sil"):
    cm = load_concept_map()
    return _searchset([M.map_service_request(o, cm) for o in _page_orders(source, limit, offset)])


@app.get("/fhir/ServiceRequest/{order_id}", tags=["FHIR Resources"])
def get_service_request(order_id: str, source: str = "sil"):
    return M.map_service_request(_order(source, order_id), load_concept_map())


def _resources_of(rtype: str, source: str, limit: int, offset: int) -> list:
    out = []
    for o in _page_orders(source, limit, offset):
        out.extend(r for r in M.build_order_resources(o) if r["resourceType"] == rtype)
    return out


@app.get("/fhir/ImagingStudy", tags=["FHIR Resources"])
def list_imaging_studies(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), source: str = "sil"):
    """Paged by order: ImagingStudy resources of `limit` orders (non-imaging orders contribute none)."""
    return _searchset(_resources_of("ImagingStudy", source, limit, offset))


@app.get("/fhir/DiagnosticReport", tags=["FHIR Resources"])
def list_diagnostic_reports(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                            source: str = "sil"):
    return _searchset(_resources_of("DiagnosticReport", source, limit, offset))


@app.get("/fhir/Observation", tags=["FHIR Resources"])
def list_observations(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), source: str = "sil"):
    """Paged by order: every Observation of `limit` orders."""
    return _searchset(_resources_of("Observation", source, limit, offset))


@app.get("/fhir/{rtype}/by-order/{order_id}", tags=["FHIR Resources"])
def resources_of_order(rtype: str, order_id: str, source: str = "sil"):
    """All resources of one type produced for one order (e.g. /fhir/Observation/by-order/PED-2024-00002)."""
    res = [r for r in M.build_order_resources(_order(source, order_id)) if r["resourceType"] == rtype]
    if not res:
        raise HTTPException(status_code=404, detail=f"No {rtype} produced for order '{order_id}'")
    return _searchset(res)
