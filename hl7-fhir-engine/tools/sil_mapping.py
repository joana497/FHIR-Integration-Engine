"""
sil_mapping.py - column-by-column mapping of the SIL tables to FHIR R4 (hypothesis H2).

One entry per column of sil.dpedidos, sil.pedidos, sil.dexames, sil.exames
(the SIL DDL supplied for this work) and sil.resultados (a documented
addition for result values). tools/build_mapping_table.py turns it into
docs/h2/sil_fhir_mapping.csv, and python-fhir-engine/tests/
test_mapping_table.py checks it against the schema and against the
columns the adapter actually reads (adapters/sil.py COLUMNS_READ).

Status
  mapped     the value appears in the FHIR output
  fallback   used only when the preferred column is empty
  rule       decides a value (status, priority, category, filtering) without being copied
  key        identity, join or ordering only
  not mapped see 'reason'

Reasons for 'not mapped'
  duplicate    the same fact is taken from another column
  workflow     internal process state (notified, collected, locked ...)
  scheduling   appointment booking; would map to Appointment (out of scope)
  billing      invoicing, payer, cost centre; would map to Claim/Coverage (out of scope)
  encounter    episode/visit context; would map to Encounter (not produced)
  audit/IT     application users, files, print and audit dates
  unknown      semantics could not be established from the name or the SPMS documents
  excluded     deliberately not propagated
  candidate    has a clear FHIR target; left for future work (see note)

Column meanings marked "(inferred)" are deduced from the Portuguese column
name, without documentation of the SIL schema. Meanings marked "(SPMS)"
come from SPMS, "Exames sem Papel - Especificação Técnica - Integrações
SONHO-LIS/RIS", v1.0, 21.04.2022, view MCDT_BDNR.
"""

SR, PT, DR, IS = "ServiceRequest", "Patient", "DiagnosticReport", "ImagingStudy"
OT, OR = "Observation (ordered test)", "Observation (result)"

# (column, status, FHIR target, rule / transformation, reason, note)
DPEDIDOS = [
    ("numpedido", "mapped", f"{SR}.identifier; {SR}.id", "identifier system .../sid/sil-order; id = UUID v5 of system + value", "", "Also prefixes the identifiers of the order's Observations; joins pedidos and dexames."),
    ("numprocesso", "not mapped", "", "", "candidate", "Hospital record number (inferred). Target: a second Patient.identifier (type MR) once its meaning is confirmed."),
    ("episodio", "not mapped", "", "", "encounter", "Episode number (SPMS: NUM_EPISODIO). Target: Encounter.identifier."),
    ("modulo", "fallback", f"{SR}.code.text", "used when designamodulo is empty", "", "Module of the episode (SPMS: COD_MODULO)."),
    ("designamodulo", "mapped", f"{SR}.code.text", "copied", "", ""),
    ("especialid", "not mapped", "", "", "candidate", "Requesting specialty code (inferred). Target: PractitionerRole.specialty of the requester."),
    ("designaespecial", "not mapped", "", "", "duplicate", "Specialty name; the examination's own designaespecial (dexames) is used for the report."),
    ("idser", "fallback", f"{SR}.performer.display", "used when servico is empty", "", ""),
    ("servico", "mapped", f"{SR}.performer.display", "copied", "", ""),
    ("datapedido", "mapped", f"{SR}.authoredOn", "DATE -> FHIR date; also the last fallback of effectiveDateTime", "", ""),
    ("posto", "not mapped", "", "", "duplicate", "Workstation; the examination's posto (dexames) is used."),
    ("dociddoente", "mapped", f"{PT}.identifier; {PT}.id", "identifier system .../sid/sil-patient; id = UUID v5 of system + value", "", "Patient identifier in SIL. It is not asserted to be the SNS number."),
    ("nomedoente", "mapped", f"{PT}.name (text, given, family)", "full string -> name.text; first token -> given, last token -> family", "", "Heuristic: compound surnames are not split; name.text keeps the original."),
    ("datanascimento", "mapped", f"{PT}.birthDate", "DATE -> FHIR date", "", ""),
    ("sexo", "mapped", f"{PT}.gender", "M -> male, F -> female, otherwise unknown", "", ""),
    ("modalidade", "rule", f"{SR}.category; {SR}.code (LOINC)", "SIL modality -> canonical category -> SNOMED CT category (108252007 / 363679005) and order-level LOINC", "", "The order-level LOINC codes are inherited and checked by the H7 run."),
    ("numexame", "not mapped", "", "", "duplicate", "Link to the examination; the adapter follows dexames.numpedido instead."),
    ("verificado", "rule", f"{SR}.status", "fechado = 1 -> completed; else verificado = 1 -> active; else draft", "", ""),
    ("avisado", "not mapped", "", "", "workflow", "Patient notified (inferred)."),
    ("marcado", "not mapped", "", "", "scheduling", "Booked (inferred)."),
    ("recolhido", "not mapped", "", "", "workflow", "Specimen collected (inferred); a Specimen resource is not produced."),
    ("fechado", "rule", f"{SR}.status", "see verificado", "", ""),
    ("segura", "not mapped", "", "", "unknown", ""),
    ("utiliza", "not mapped", "", "", "audit/IT", "Application user (inferred)."),
    ("utilizamarca", "not mapped", "", "", "audit/IT", "User who booked (inferred)."),
    ("utilizarecolha", "not mapped", "", "", "audit/IT", "User who collected (inferred)."),
    ("tipoanalise", "not mapped", "", "", "unknown", ""),
    ("medico", "mapped", f"{SR}.requester.display; {IS}.referrer.display", "copied as display text (no Practitioner resource)", "", ""),
    ("serv", "not mapped", "", "", "unknown", "Internal service code (inferred)."),
    ("dataverificacao", "not mapped", "", "", "workflow", ""),
    ("datamarcado", "not mapped", "", "", "scheduling", ""),
    ("datamarcacao", "not mapped", "", "", "scheduling", ""),
    ("horamarcacao", "not mapped", "", "", "scheduling", ""),
    ("datarecolha", "not mapped", "", "", "candidate", "Collection date (inferred). Target: Specimen.collection.collectedDateTime."),
    ("datachegada", "not mapped", "", "", "workflow", "Arrival date (inferred)."),
    ("datachamada", "not mapped", "", "", "workflow", "Call date (inferred)."),
    ("dataexame", "mapped", f"{SR}.occurrenceDateTime", "DATE -> FHIR date; also a fallback of effectiveDateTime", "", ""),
    ("urgente", "rule", f"{SR}.priority", "> 0 -> urgent, otherwise routine", "", ""),
    ("observacoes", "mapped", f"{SR}.note.text", "copied", "", ""),
    ("infclinica", "mapped", f"{SR}.reasonCode.text", "copied", "", "Free-text clinical information; not coded."),
    ("contraindic", "mapped", f"{SR}.note.text", "prefixed 'Contraindication: '", "", ""),
    ("sugmarcacao", "not mapped", "", "", "scheduling", "Booking suggestion (inferred)."),
    ("pedidomae", "not mapped", "", "", "candidate", "Parent order (inferred). Target: ServiceRequest.basedOn, once its meaning is confirmed."),
    ("postoagenda", "not mapped", "", "", "scheduling", ""),
    ("ref_entrada", "not mapped", "", "", "unknown", ""),
    ("bloqagenda", "not mapped", "", "", "scheduling", ""),
    ("triagem", "not mapped", "", "", "encounter", "Triage (inferred)."),
]

PEDIDOS = [
    ("numpedido", "key", "", "join to dpedidos", "", ""),
    ("ordem", "key", f"{SR}.orderDetail (order)", "orders the lines", "", ""),
    ("idser", "not mapped", "", "", "duplicate", "Same service as the order header."),
    ("modalidade", "not mapped", "", "", "duplicate", "The header modality is used."),
    ("codprotocolo", "mapped", f"{SR}.orderDetail.coding (.../CodeSystem/sil-protocol)", "copied", "", "Local code, kept for provenance; no ConceptMap target by design."),
    ("designacaoprotocolo", "mapped", f"{SR}.orderDetail.coding.display", "copied", "", ""),
    ("codprop", "not mapped", "", "", "unknown", ""),
    ("verificado", "not mapped", "", "", "workflow", "Line-level verification."),
    ("dataverificacao", "not mapped", "", "", "workflow", ""),
    ("pedidofilho", "not mapped", "", "", "unknown", "Child order (inferred)."),
    ("cod_pedido", "not mapped", "", "", "unknown", "Request code; in the demonstration data it repeats the analysis code."),
    ("des_pedido", "mapped", f"{SR}.orderDetail.text", "copied", "", ""),
    ("notas_linhas", "not mapped", "", "", "candidate", "Line notes (e.g. fasting). Target: ServiceRequest.note, prefixed with the line number."),
    ("cancelar", "rule", f"{SR}.orderDetail; {SR}.note", "a cancelled line (cancelar <> 0) is left out of orderDetail and reported in a note", "", ""),
    ("datamsg", "not mapped", "", "", "audit/IT", "Message date (inferred)."),
    ("numactomed", "not mapped", "", "", "unknown", "Possibly the institution's act number (SPMS: NUM_ACTO); not confirmed."),
    ("id_bdnr", "not mapped", "", "", "candidate", "Identifier of the MCDT in the BDNR (SPMS). Target: an identifier per line, which needs one ServiceRequest per MCDT."),
    ("id_requisicao", "mapped", f"{SR}.requisition", "system .../sid/pt-bdnr-requisition; only when all active lines share one value", "", "Requisition identifier in the BDNR (SPMS: ID_REQUISICAO)."),
    ("cod_mcdt", "mapped", f"{SR}.orderDetail.coding (.../CodeSystem/pt-conv-mcdt) + SNOMED CT", "kept as stored (area + convention code, e.g. A697.1); SNOMED CT added from the ConceptMap", "", "SPMS: COD_MCDT = convention area + code of the convention price table."),
    ("pin_agendamento", "not mapped", "", "", "excluded", "Booking PIN of the requisition (SPMS): an access credential, not clinical data."),
]

DEXAMES = [
    ("numexame", "mapped", f"{DR}.identifier; {IS}.identifier", "identifier system .../sid/sil-report; part of the deterministic ids", "", ""),
    ("versao", "key", "", "part of the examination key", "", "Each version produces its own report; choosing the latest version is future work."),
    ("numprocesso", "not mapped", "", "", "duplicate", "Repeats the order header."),
    ("episodio", "not mapped", "", "", "duplicate", ""),
    ("modulo", "not mapped", "", "", "duplicate", ""),
    ("designamodulo", "not mapped", "", "", "duplicate", ""),
    ("especialid", "not mapped", "", "", "duplicate", ""),
    ("designaespecial", "mapped", f"{DR}.code (display, text); {IS}.description; series.description", "copied; 'Microbiologia' also sets the category MB", "", ""),
    ("idser", "key", f"{IS}.location.display (fallback)", "part of the examination key; location when posto is empty", "", ""),
    ("servico", "fallback", f"{DR}.code.text; {IS}.description", "used when designaespecial is empty", "", ""),
    ("datapedido", "fallback", f"{DR}.effectiveDateTime; Observation.effectiveDateTime", "used when dataexame is empty", "", ""),
    ("dataexame", "mapped", f"{DR}.effectiveDateTime; Observation.effectiveDateTime; {IS}.started", "DATE -> FHIR date", "", ""),
    ("dociddoente", "not mapped", "", "", "duplicate", "The patient is taken from the order header."),
    ("nomedoente", "not mapped", "", "", "duplicate", ""),
    ("datanascimento", "not mapped", "", "", "duplicate", ""),
    ("sexo", "not mapped", "", "", "duplicate", ""),
    ("pathrelatorio", "not mapped", "", "", "candidate", "Path of the report file (inferred). Target: DiagnosticReport.presentedForm, if the file is reachable."),
    ("grupo", "mapped", f"{DR}.code.coding (.../CodeSystem/sil-exam-group)", "copied; part of the examination key", "", "Local code; no ConceptMap target by design."),
    ("numpedido", "key", f"{DR}.basedOn; {IS}.basedOn", "joins the examination to its order", "", ""),
    ("verificado", "rule", f"{DR}.status; {IS}.status", "-> registered when no report exists yet", "", ""),
    ("facturado", "not mapped", "", "", "billing", ""),
    ("imagem", "not mapped", "", "", "unknown", "Image available (inferred)."),
    ("relatorio", "rule", f"{DR}.status", "relatorio and fechado -> final; relatorio only -> preliminary", "", ""),
    ("fechado", "rule", f"{DR}.status; {IS}.status", "-> final (report) / available (study)", "", ""),
    ("segura", "not mapped", "", "", "unknown", ""),
    ("utiliza", "not mapped", "", "", "audit/IT", ""),
    ("tipoanalise", "not mapped", "", "", "unknown", ""),
    ("medico", "mapped", f"{DR}.performer.display; Observation.performer.display (fallback)", "copied as display text", "", ""),
    ("serv", "not mapped", "", "", "unknown", ""),
    ("dataverificacao", "fallback", f"{DR}.issued", "used when datarelat is empty", "", ""),
    ("datafacturacao", "not mapped", "", "", "billing", ""),
    ("datarelat", "mapped", f"{DR}.issued", "DATE -> instant (00:00 local time of the source, SOURCE_TIMEZONE, default Europe/Lisbon)", "", "SIL stores a date only; the time of the instant is therefore not meaningful."),
    ("datafecho", "not mapped", "", "", "workflow", "Closure date (inferred)."),
    ("dataimagem", "not mapped", "", "", "unknown", ""),
    ("subsistema", "not mapped", "", "", "billing", "Health subsystem / payer (inferred)."),
    ("proveniencia", "not mapped", "", "", "encounter", "Origin of the patient (inferred)."),
    ("listaestudos", "not mapped", "", "", "unknown", "List of studies (inferred); format unknown."),
    ("ccusto", "not mapped", "", "", "billing", "Cost centre (inferred)."),
    ("listatecnicos", "not mapped", "", "", "candidate", "Technicians (inferred). Target: ImagingStudy.series.performer."),
    ("posto", "mapped", f"{IS}.location.display", "copied", "", ""),
    ("modalidade", "rule", f"{DR}.category (v2-0074); {IS}.modality, series.modality (DICOM); Observation.category", "SIL modality -> canonical category -> v2-0074 section and DICOM modality", "", ""),
    ("dauditado", "not mapped", "", "", "audit/IT", ""),
    ("dexaminado", "not mapped", "", "", "unknown", ""),
    ("numeroprov", "not mapped", "", "", "unknown", ""),
    ("dta_alta", "not mapped", "", "", "encounter", "Discharge date (inferred)."),
    ("nomefich", "not mapped", "", "", "audit/IT", "File name (inferred)."),
    ("dataimpressao", "not mapped", "", "", "audit/IT", "Print date (inferred)."),
    ("horapedido", "not mapped", "", "", "candidate", "Time of the request as text (inferred). Target: combine with datapedido into a dateTime."),
    ("horaexame", "not mapped", "", "", "candidate", "Time of the examination as text (inferred). Target: combine with dataexame into a dateTime."),
    ("idserori", "not mapped", "", "", "unknown", "Originating service (inferred)."),
    ("nori", "not mapped", "", "", "unknown", ""),
    ("bodypart", "mapped", f"{IS}.series.bodySite.display", "copied as display text (not coded)", "", ""),
    ("dta_consulta", "not mapped", "", "", "scheduling", ""),
    ("next_especialidade", "not mapped", "", "", "unknown", ""),
    ("mrelat", "not mapped", "", "", "unknown", ""),
    ("dataversao", "not mapped", "", "", "audit/IT", "Version date (inferred)."),
    ("privado", "not mapped", "", "", "candidate", "Possibly a privacy flag (inferred). If so it must become meta.security (e.g. confidentiality R) before production use."),
    ("req_idser", "not mapped", "", "", "unknown", ""),
    ("aoucexame", "not mapped", "", "", "unknown", ""),
]

EXAMES = [
    ("numexame", "key", "", "join to dexames", "", ""),
    ("versao", "key", "", "join to dexames", "", ""),
    ("ordem", "key", f"{OT}.identifier", "part of the test key", "", ""),
    ("validade", "not mapped", "", "", "unknown", ""),
    ("idser", "key", "", "join to dexames", "", ""),
    ("grupo", "key", "", "join to dexames", "", ""),
    ("codigif", "mapped", f"{OT}.code.coding (.../CodeSystem/pt-sns-mcdt) + SNOMED CT", "only five-digit values; SNOMED CT added from the ConceptMap", "", "Assumption: the IGIF code is the CPAL 'Código SNS' (ACSS table); to be confirmed."),
    ("designacaoigif", "mapped", f"{OT}.code.coding.display", "copied", "", ""),
    ("codigoanalise", "mapped", f"{OT}.code.coding (.../CodeSystem/sil-analysis)", "copied; part of the test key", "", "Local code; no ConceptMap target by design."),
    ("designacaoanalise", "mapped", f"{OT}.code.coding.display; code.text", "copied", "", ""),
    ("codprop", "not mapped", "", "", "unknown", ""),
    ("verificado", "not mapped", "", "", "workflow", "The test status is derived from its results."),
    ("facturado", "not mapped", "", "", "billing", ""),
    ("dataverificacao", "not mapped", "", "", "workflow", ""),
    ("datafacturacao", "not mapped", "", "", "billing", ""),
    ("tipoanestesia", "not mapped", "", "", "unknown", "Anaesthesia type (inferred); would map to Procedure."),
    ("medicoanestesia", "not mapped", "", "", "unknown", "Anaesthetist (inferred)."),
    ("idserori", "not mapped", "", "", "unknown", ""),
    ("id_bdnr", "not mapped", "", "", "candidate", "Identifier of the MCDT in the BDNR (SPMS)."),
    ("id_requisicao", "not mapped", "", "", "duplicate", "The requisition is taken from pedidos."),
    ("cod_mcdt", "mapped", f"{OT}.code.coding (.../CodeSystem/pt-conv-mcdt)", "kept as stored (area + convention code)", "", "SPMS: COD_MCDT."),
    ("pin_agendamento", "not mapped", "", "", "excluded", "Booking PIN (SPMS): an access credential."),
]

RESULTADOS = [
    ("idser", "key", "", "join to exames", "", ""),
    ("grupo", "key", "", "join to exames", "", ""),
    ("numexame", "key", "", "join to exames", "", ""),
    ("codigoanalise", "key", "", "join to exames (assigns the result to its test)", "", ""),
    ("versao", "key", "", "join to exames", "", ""),
    ("ordem", "key", "", "join to exames", "", ""),
    ("seq", "key", f"{OR}.identifier; {OR}.id", "part of the result key", "", ""),
    ("codparametro", "mapped", f"{OR}.code.coding (.../CodeSystem/sil-parameter) + LOINC", "LOINC added from the ConceptMap (CPAL)", "", ""),
    ("designacaoparametro", "mapped", f"{OR}.code.coding.display; code.text", "copied", "", ""),
    ("tiporesultado", "rule", f"{OR}.value[x]; {DR}.conclusion", "NM -> valueQuantity; CE -> valueCodeableConcept; TX -> valueString; CO -> DiagnosticReport.conclusion", "", ""),
    ("valornumerico", "mapped", f"{OR}.valueQuantity.value", "NUMERIC -> JSON number (integer when integral)", "", ""),
    ("comparador", "mapped", f"{OR}.valueQuantity.comparator", "copied (<, <=, >=, >)", "", ""),
    ("unidade", "mapped", f"{OR}.valueQuantity.unit; .system/.code (UCUM)", "unit text as written; UCUM code from the ConceptMap", "", ""),
    ("codvalor", "mapped", f"{OR}.valueCodeableConcept.coding (.../CodeSystem/sil-result-value) + SNOMED CT", "SNOMED CT added from the ConceptMap (CPAL value sets)", "", ""),
    ("valortexto", "mapped", f"{OR}.valueCodeableConcept.text / valueString; {DR}.conclusion", "by tiporesultado", "", ""),
    ("refmin", "mapped", f"{OR}.referenceRange.low", "same unit and UCUM code as the value", "", ""),
    ("refmax", "mapped", f"{OR}.referenceRange.high", "same unit and UCUM code as the value", "", ""),
    ("reftexto", "mapped", f"{OR}.referenceRange.text", "copied", "", ""),
    ("flag", "mapped", f"{OR}.interpretation (v3-ObservationInterpretation)", "N, L, H, LL, HH, A, POS, NEG copied as codes; other values dropped", "", ""),
    ("estado", "mapped", f"{OR}.status; {OT}.status", "F -> final, P -> preliminary, C -> corrected; the test takes the least final status", "", ""),
    ("dataresultado", "mapped", f"{OR}.issued", "TIMESTAMP -> instant; a value without time zone is local time of the source (SOURCE_TIMEZONE, default Europe/Lisbon: +00:00 in winter, +01:00 in summer)", "", "The default assumes a mainland institution; set SOURCE_TIMEZONE for the Azores (Atlantic/Azores) or Madeira (Atlantic/Madeira)."),
    ("validadopor", "mapped", f"{OR}.performer.display", "copied", "", ""),
]

TABLES = {"dpedidos": DPEDIDOS, "pedidos": PEDIDOS, "dexames": DEXAMES, "exames": EXAMES, "resultados": RESULTADOS}
SUPPLIED = {"dpedidos", "pedidos", "dexames", "exames"}   # resultados is a demonstration addition
USED = {"mapped", "fallback", "rule", "key"}
