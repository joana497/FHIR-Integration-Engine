"""Mapper rules that the seed data do not exercise (no database needed)."""
from decimal import Decimal

import fhir_mappers as M
import systems as S
import canonical as C
from canonical import Coding, ExamRec, Identifier, OrderRec, PatientRec, ResultRec


def _order(*results: ResultRec) -> OrderRec:
    test = C.TestRec(key="T:1", codings=[Coding(S.cs("sil", "analysis"), "T")], results=list(results))
    exam = ExamRec(key="E:S:G:1", identifier=Identifier(S.sid("sil", "report"), "E"), category="LAB", tests=[test])
    return OrderRec(source="sil", identifier=Identifier(S.sid("sil", "order"), "P"),
                    patient=PatientRec(identifiers=[Identifier(S.sid("sil", "patient"), "1")]), exams=[exam])


def _observations(o: OrderRec) -> dict:
    return {r["identifier"][0]["value"].rsplit(":", 1)[-1]: r for r in M.build_order_resources(o)
            if r["resourceType"] == "Observation" and r["identifier"][0]["system"].endswith("sil-result")}


def test_reference_text_is_kept_for_every_kind_of_result():
    o = _order(
        ResultRec(key="1", codings=[Coding(S.cs("sil", "parameter"), "X")], kind="coded",
                  value_coding=Coding(S.cs("sil", "result-value"), "NEG", "Negativo"), ref_text="Negativo"),
        ResultRec(key="2", codings=[Coding(S.cs("sil", "parameter"), "Y")], kind="string", value_text="Texto",
                  ref_text="Ausente"),
        ResultRec(key="3", codings=[Coding(S.cs("sil", "parameter"), "Z")], kind="quantity", value_num=None,
                  ref_text="pendente"),
        ResultRec(key="4", codings=[Coding(S.cs("sil", "parameter"), "W")], kind="quantity",
                  value_num=Decimal("4.20"), unit_local=Coding(S.cs("sil", "unit"), "mg/dL"),
                  ref_low=Decimal("1"), ref_high=Decimal("5"), ref_text="1-5"),
    )
    obs = _observations(o)
    assert obs["1"]["referenceRange"] == [{"text": "Negativo"}]
    assert obs["2"]["referenceRange"] == [{"text": "Ausente"}]
    assert obs["3"]["referenceRange"] == [{"text": "pendente"}] and "valueQuantity" not in obs["3"]
    rr = obs["4"]["referenceRange"][0]
    assert rr["low"]["value"] == 1 and rr["high"]["value"] == 5 and rr["text"] == "1-5"
    assert obs["4"]["valueQuantity"]["value"] == 4.2


def test_no_reference_range_without_reference_data():
    o = _order(ResultRec(key="1", codings=[Coding(S.cs("sil", "parameter"), "X")], kind="string", value_text="T"))
    assert "referenceRange" not in _observations(o)["1"]


def test_naive_times_take_the_source_time_zone(monkeypatch):
    """Naive timestamps are local time of the source (default Europe/Lisbon: WET/WEST)."""
    from datetime import date, datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(M, "SOURCE_TZ", ZoneInfo("Europe/Lisbon"))
    assert M._fmt_date(datetime(2024, 1, 15, 10, 30)) == "2024-01-15T10:30:00+00:00"      # winter
    assert M._fmt_date(datetime(2024, 7, 15, 10, 30)) == "2024-07-15T10:30:00+01:00"      # summer
    assert M._fmt_date(date(2024, 7, 15)) == "2024-07-15"                                  # a date stays a date
    assert M._fmt_instant(date(2024, 7, 15)) == "2024-07-15T00:00:00+01:00"                # midnight local
    assert M._fmt_instant("2024-01-15") == "2024-01-15T00:00:00+00:00"
    aware = datetime(2024, 7, 15, 10, 30, tzinfo=timezone(timedelta(hours=-1)))
    assert M._fmt_instant(aware) == "2024-07-15T10:30:00-01:00"                            # an offset is kept
    monkeypatch.setattr(M, "SOURCE_TZ", ZoneInfo("Atlantic/Azores"))
    assert M._fmt_date(datetime(2024, 1, 15, 10, 30)) == "2024-01-15T10:30:00-01:00"


def test_order_level_loinc_codes_are_the_verified_ones():
    """The six imaging codes rejected by tx.fhir.org in the first run must not come back."""
    rejected = {"24629-1", "24627-5", "24628-3", "24636-6", "24631-7", "24634-1"}
    assert not rejected & set(M.MODALITY_LOINC.values())
