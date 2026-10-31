from datetime import date, datetime, timedelta, timezone

import pytest

from hl7v2 import Encoding, HL7ParseError, parse, ts, unescape


def test_msh_numbering_and_components():
    m = parse("MSH|^~\\&|LISC|LAB-C^^EXAMPLE|FHIR|EX|20240101120000||ORU^R01^ORU_R01|42|P|2.5.1\rPID|1||9^^^SNS^NNPRT||Silva^Ana")
    msh = m.first("MSH")
    assert msh.raw(3) == "LISC" and msh.comp(4, 1) == "LAB-C" and msh.comp(9, 2) == "R01" and msh.raw(10) == "42"
    pid = m.first("PID")
    assert pid.comp(3, 4) == "SNS" and pid.comp(5, 2) == "Ana"


def test_escapes():
    e = Encoding()
    assert unescape(r"a\F\b\S\c\T\d\R\e\E\f", e) == "a|b^c&d~e\\f"
    assert unescape(r"x\.br\y", e) == "x\ny"


def test_timestamps():
    assert ts("20240212") == date(2024, 2, 12)
    assert ts("202402121030") == datetime(2024, 2, 12, 10, 30)
    assert ts("20240212103000+0100") == datetime(2024, 2, 12, 10, 30, tzinfo=timezone(timedelta(hours=1)))
    assert ts("") is None and ts("junk") is None


def test_rejects_non_hl7():
    with pytest.raises(HL7ParseError):
        parse("hello")
