import json

import systems as S
from concept_map import DEFAULT_PATH, load

R4_EQUIVALENCE = {"relatedto", "equivalent", "equal", "wider", "subsumes", "narrower",
                  "specializes", "inexact", "unmatched", "disjoint"}


def test_conceptmap_is_well_formed_r4():
    cm = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
    assert cm["resourceType"] == "ConceptMap" and cm["url"] == S.LAB_CONCEPTMAP_URL and cm["status"]
    for g in cm["group"]:
        assert g["source"].startswith(("http://", "https://")) and g["target"] in {S.LOINC, S.UCUM, S.SNOMED}
        for el in g["element"]:
            assert el["code"] and el["target"]
            for t in el["target"]:
                assert t["code"] and t["equivalence"] in R4_EQUIVALENCE and "CPAL" in t["comment"] or g["target"] == S.UCUM


def test_every_catalogue_component_is_mapped_for_each_source():
    from conftest import ROOT
    cat = json.loads((ROOT / "catalogue" / "catalogue_resolved.json").read_text(encoding="utf-8"))
    cm = load()
    from canonical import Coding
    names = {"sil": "parameter", "lisb": "analyte", "lisc": "observation"}
    for t in cat["tests"]:
        for c in t["components"]:
            for src, cs in names.items():
                if c.get(src):
                    hit = cm.first(Coding(S.cs(src, cs), c[src]), S.LOINC)
                    assert hit and hit.code == c["loinc"], (src, c[src])
