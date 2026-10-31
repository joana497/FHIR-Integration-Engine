"""
systems.py - every URI the engine writes into FHIR resources.

Standard terminologies use their official URIs. Everything local to a
source system, and the identifier namespaces, live under example.org:
the demonstration does not pretend to own a hospital or SPMS domain.
A real deployment would replace BASE with URIs assigned by the
institution (or by SPMS for national code systems).
"""

BASE = "http://example.org/fhir"

# ── Standard terminologies ──────────────────────────────────────────────────
LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
UCUM = "http://unitsofmeasure.org"
DICOM = "http://dicom.nema.org/resources/ontology/DCM"
V2_0074 = "http://terminology.hl7.org/CodeSystem/v2-0074"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
V3_INTERPRETATION = "http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation"

# ── National code systems (placeholder URIs, see module docstring) ──────────
PT_SNS_MCDT = f"{BASE}/CodeSystem/pt-sns-mcdt"     # CPAL "Código SNS" (ACSS price table)
PT_CONV_MCDT = f"{BASE}/CodeSystem/pt-conv-mcdt"   # area + CPAL "Código Conv." (SIL cod_mcdt, e.g. A697.1)

# ── Identifier namespaces ───────────────────────────────────────────────────
SNS_NUMBER = f"{BASE}/sid/sns-number"
# Requisition identifier in the national prescription database (BDNR), SIL
# pedidos.id_requisicao; SPMS view MCDT_BDNR: "ID_REQUISICAO - Identificador da Requisição".
BDNR_REQUISITION = f"{BASE}/sid/pt-bdnr-requisition"


def cs(source: str, name: str) -> str:
    """Local code system of one source, e.g. cs('sil', 'parameter')."""
    return f"{BASE}/CodeSystem/{source}-{name}"


def sid(source: str, name: str) -> str:
    """Identifier namespace of one source, e.g. sid('sil', 'order')."""
    return f"{BASE}/sid/{source}-{name}"


def meta_source(source: str) -> str:
    """Value for Resource.meta.source."""
    return f"{BASE}/source/{source}"


# ConceptMap canonical URL used by the engine at runtime.
LAB_CONCEPTMAP_URL = f"{BASE}/ConceptMap/lab-catalogue"
