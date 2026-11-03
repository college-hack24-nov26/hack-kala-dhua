"""Classification guards: system-only categories are never a report's reading,
and camera detections map to the category they name."""
from types import SimpleNamespace

from app.incidents import parse
from app.mesh import service as mesh
from app.taxonomy import cache


def _cats():
    out = {}
    for c, life in [("flooded_road", False), ("person_stranded", True), ("structural_damage", True),
                    ("evacuation", True), ("traffic_corridor", False), ("shelter_full", False),
                    ("supply_shortage", False), ("unknown_report", False), ("fire", True),
                    ("waterlogging", False), ("fallen_tree", False)]:
        out[c] = SimpleNamespace(display_name=c.replace("_", " "), life_safety=life, keywords=None,
                                 needs={}, base_severity=3, hazard_id="flood")
    return out


def test_system_only_categories_are_not_reportable(monkeypatch):
    monkeypatch.setattr(cache, "categories", _cats())
    r = parse.reportable()
    assert not set(r) & parse.SYSTEM_ONLY
    assert "person_stranded" in r and "fire" in r


def test_unrecognised_text_is_unclassified_not_guessed(monkeypatch):
    monkeypatch.setattr(cache, "categories", _cats())
    p = parse.parse("wassup")
    assert p.category == parse.UNKNOWN and p.method == "default"


def test_camera_detections_map_to_named_categories():
    assert mesh.SENSOR_CATEGORY["collapse"] == "structural_damage"
    assert mesh.SENSOR_CATEGORY["fall"] == "person_stranded"
    assert "unknown_report" not in mesh.SENSOR_CATEGORY.values()
    for security in ("fight", "violence", "assault", "gathering", "object_left"):
        assert security not in mesh.SENSOR_CATEGORY
