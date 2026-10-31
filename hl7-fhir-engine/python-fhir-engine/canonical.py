"""
canonical.py - the engine's internal model.

Every source adapter (SIL, the second SQL system, HL7 v2 ORU^R01)
translates its own records into these dataclasses. The FHIR mappers in
fhir_mappers.py read only these classes, so supporting a new legacy
source means writing an adapter, not touching the mappers.

The model is deliberately close to the MCDT lifecycle: a patient, an
order (requisition) with its ordered lines, the examinations performed
under it, and the results of each ordered test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Union

DateLike = Optional[Union[date, datetime]]


@dataclass(frozen=True)
class Coding:
    system: str
    code: str
    display: Optional[str] = None


@dataclass
class Identifier:
    system: str
    value: str


@dataclass
class PatientRec:
    identifiers: list[Identifier]           # first one is the primary key for the FHIR id
    family: Optional[str] = None
    given: list[str] = field(default_factory=list)
    name_text: Optional[str] = None
    gender: str = "unknown"                 # FHIR administrative-gender
    birth_date: DateLike = None


@dataclass
class OrderLineRec:
    codings: list[Coding]
    text: Optional[str] = None


@dataclass
class ResultRec:
    key: str                                 # unique within the order; used for the FHIR id
    codings: list[Coding]                    # local (and possibly standard) codes of the measured item
    text: Optional[str] = None
    status: str = "final"                    # FHIR ObservationStatus
    issued: DateLike = None
    kind: Optional[str] = None               # "quantity" | "coded" | "string" | None (no value yet)
    value_num: Optional[Decimal] = None
    comparator: Optional[str] = None         # <, <=, >=, >
    unit_local: Optional[Coding] = None      # local unit string, in the source's unit code system
    unit_standard: Optional[Coding] = None   # UCUM unit when the source already sends one (HL7 v2 OBX-6)
    value_coding: Optional[Coding] = None    # qualitative value, local or standard
    value_text: Optional[str] = None
    ref_low: Optional[Decimal] = None
    ref_high: Optional[Decimal] = None
    ref_text: Optional[str] = None
    interpretation: Optional[str] = None     # v3 ObservationInterpretation code: N, H, L, HH, LL, A
    performer: Optional[str] = None


@dataclass
class TestRec:
    """One ordered test (an 'exames' line in SIL, an OBR in HL7 v2)."""
    key: str
    codings: list[Coding]
    text: Optional[str] = None
    status: str = "registered"
    results: list[ResultRec] = field(default_factory=list)


@dataclass
class ExamRec:
    """One examination performed under the order (a report)."""
    key: str
    identifier: Identifier
    category: str                            # canonical category: LAB, HM, CH, BLB, MB, SR, XR, CT, MR, US, MG, NM, XA, ECG, PATH
    modality_dicom: Optional[str] = None     # set only for imaging examinations
    codings: list[Coding] = field(default_factory=list)
    description: Optional[str] = None
    status_report: str = "unknown"           # FHIR DiagnosticReportStatus
    status_study: str = "unknown"            # FHIR ImagingStudyStatus
    performed: DateLike = None
    issued: DateLike = None
    performer: Optional[str] = None
    location: Optional[str] = None
    body_site_text: Optional[str] = None
    conclusion: Optional[str] = None
    tests: list[TestRec] = field(default_factory=list)


@dataclass
class OrderRec:
    source: str                              # adapter name: "sil", "lisb", "oru"
    identifier: Identifier
    patient: PatientRec
    requisition: Optional[Identifier] = None  # group identifier shared by the order's lines (e.g. BDNR)
    status: str = "unknown"                  # FHIR RequestStatus
    priority: str = "routine"                # FHIR RequestPriority
    category: str = "LAB"                    # canonical category of the order
    codings: list[Coding] = field(default_factory=list)
    text: Optional[str] = None
    authored_on: DateLike = None
    occurrence: DateLike = None
    requester: Optional[str] = None
    performer: Optional[str] = None
    reason_text: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    lines: list[OrderLineRec] = field(default_factory=list)
    exams: list[ExamRec] = field(default_factory=list)
