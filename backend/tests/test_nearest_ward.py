"""Reports from outside the covered area go to the nearest ward instead of being refused."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests import _stubs  # noqa: F401

_stubs.install()

from app.api.v1 import personas  # noqa: E402
from app.core.errors import BadRequest  # noqa: E402


def _loc(inside: bool, km: float = 0.0, ward=True):
    w = SimpleNamespace(id="w-12", name="Kasba Peth") if ward else None
    return SimpleNamespace(inside=inside, ward=w, distance_km=km, note="no wards")


def test_outside_is_filed_to_nearest_ward():
    loc = _loc(False, 7.5)
    personas._nearest_ward_or_refuse(loc)  # does not raise
    out = personas._filed_to(loc)
    assert out["wardId"] == "w-12"
    assert out["outsideArea"] is True
    assert "7.5 km" in out["filedNote"] and "Kasba Peth" in out["filedNote"]


def test_inside_has_no_note():
    out = personas._filed_to(_loc(True))
    assert out["outsideArea"] is False and out["filedNote"] is None


def test_no_wards_at_all_still_refuses():
    with pytest.raises(BadRequest):
        personas._nearest_ward_or_refuse(_loc(False, ward=False))
