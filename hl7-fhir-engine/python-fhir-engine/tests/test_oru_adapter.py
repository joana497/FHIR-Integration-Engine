import glob
import os

import pytest

import systems as S
from adapters.oru_r01 import OruError, OruR01Adapter
from conftest import ROOT, conformance_problems
from fhir_mappers import build_bundle, build_order_resources

SAMPLES = sorted(glob.glob(str(ROOT / "samples" / "oru" / "*.hl7")))


@pytest.mark.parametrize("path", SAMPLES, ids=os.path.basename)
def test_samples_map_cleanly(path):
    orders = OruR01Adapter().parse(open(path, encoding="utf-8").read())
    assert orders
    res = [r for o in orders for r in build_order_resources(o)]
    assert conformance_problems(build_bundle(res)) == []


def test_every_result_gets_loinc_and_units_get_ucum():
    for path in SAMPLES:
        for o in OruR01Adapter().parse(open(path, encoding="utf-8").read()):
            for r in build_order_resources(o):
                if r["resourceType"] == "Observation" and "hasMember" not in r:
                    assert any(c["system"] == S.LOINC for c in r["code"]["coding"]), r["code"]
                    if "valueQuantity" in r:
                        assert r["valueQuantity"].get("system") == S.UCUM
                    if "valueCodeableConcept" in r:
                        assert any(c["system"] == S.SNOMED for c in r["valueCodeableConcept"]["coding"])


@pytest.mark.parametrize("msg,err", [
    ("MSH|^~\\&|A|B|C|D|20240101||ADT^A01|1|P|2.5.1\rPID|1||1", "unsupported message type"),
    ("MSH|^~\\&|A|B|C|D|20240101||ORU^R01|1|P|2.5.1\rPID|1||1", "without PID or OBR"),
    ("MSH|^~\\&|A|B|C|D|20240101||ORU^R01|1|P|2.5.1\rPID|1||1\rORC|RE|P1\rOBR|1|P1||T^t^L\rOBX|1|NM|X^x^L||abc", "not a number"),
    ("MSH|^~\\&|A|B|C|D|20240101||ORU^R01|1|P|2.5.1\rPID|1||1\rORC|RE|P1\rOBR|1|P1||T^t^L\rOBX|1|ZZ|X^x^L||1", "not supported"),
])
def test_rejections(msg, err):
    with pytest.raises(OruError, match=err):
        OruR01Adapter().parse(msg)
