"""Teams of units from a report's words; unit log CSV."""
from app.incidents.intake import needs_from_text
from app.units.service import to_csv


def test_fire_with_people_trapped_needs_a_team():
    n = needs_from_text("Fire in the chemical godown, two workers trapped inside, crowd gathered")
    assert n == {"search_rescue": 1, "medical_transport": 1, "traffic_control": 1}


def test_marathi_and_hindi_words():
    assert "search_rescue" in needs_from_text("इमारतीत लोक अडकले आहेत")
    assert needs_from_text("सड़क पर भीड़ है") == {"traffic_control": 1}
    assert needs_from_text("water on the road") == {}


def test_log_csv_has_header_and_rows():
    out = to_csv([{"at": "2026-10-10T10:00:00+00:00", "type": "event", "kind": "assignment.rerouted",
                   "summary": "off route, redrawn", "lng": 73.8, "lat": 18.6}], "PC-AMB-01")
    lines = out.strip().splitlines()
    assert lines[0].startswith("unit,at,type,kind") and "PC-AMB-01" in lines[1] and "rerouted" in lines[1]


def test_mesh_region_split():
    from app.mesh.service import _channels, region_of
    assert region_of(77.34) == "ncr" and region_of(73.80) == "pune"
    assert region_of(None, "V-NCR-STR4") == "ncr" and region_of(None, "lora:V-PUN-GAS6") == "pune"
    assert region_of(None, "unknown") is None
    ch = {c["name"]: c["pct"] for c in _channels(
        "Gas/heat hazard: human 0%, structural 0%, environment 100% (gas mq2 100%, heat 98%).")}
    assert ch["gas mq2"] == 100 and ch["heat"] == 98 and ch["environment"] == 100


def test_media_decoder_accepts_images_only():
    import base64
    from app.media import _decode
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    assert _decode(base64.b64encode(png).decode())[1] == "image/png"
    assert _decode("data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff" + b"1" * 10).decode())[1] == "image/jpeg"
    assert _decode(base64.b64encode(b"x" * 500_001).decode()) is None


def test_surge_triage_and_rationing_follow_the_level():
    from app.surge import service as s
    s._LEVEL.update({"pune": 0, "ncr": 0})
    assert s.triage_factor("w-pc-07", "search_rescue", 0.12) == 1.0 and s.ration_factor(73.8) == 1.0
    s._LEVEL["pune"] = 1
    assert s.triage_factor("w-pc-07", "search_rescue", 0.12) == 2.0 * 1.3
    assert s.triage_factor("w-pc-07", "supply_delivery", 0.05) == 1.0
    assert s.ration_factor(73.8) == s.RATION_FACTOR and s.ration_factor(77.3) == 1.0   # NCR not strained
    assert s.triage_factor("w-gzb-01", "search_rescue", None) == 1.0
    s._LEVEL["pune"] = 0


def test_ladder_pressure_reasons():
    from app.surge.service import _pressure
    m = {"fleet": {"utilisation": 0.9}, "shelters": {"occupancy_ratio": 0.95, "full_sites": 3},
         "unmet": {"count": 4, "life_safety": 2, "by_capability": {"search_rescue": 2, "mass_transport": 2}},
         "supplies_low_sites": 1}
    assert len(_pressure(m, 1)) == 4 and _pressure(m, 2) and _pressure(m, 3) and _pressure(m, 4)
    calm = {"fleet": {"utilisation": 0.3}, "shelters": {"occupancy_ratio": 0.2, "full_sites": 0},
            "unmet": {"count": 0, "life_safety": 0, "by_capability": {}}, "supplies_low_sites": 0}
    assert not any(_pressure(calm, k) for k in (1, 2, 3, 4))
