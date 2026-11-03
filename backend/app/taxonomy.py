"""The taxonomy cache.

Which hazards exist, which incident categories, which resource kinds, what each
kind can *do*, and how tightly a category should be clustered: all of it is
rows, not code. This module reads those rows once at startup, holds them in
memory, and hands them out.

Why this file exists at all: the thing that stops a disaster platform running a
second city or a sixth hazard is almost never the algorithms. It is the closed
type somewhere in the middle that has to be widened, migrated and redeployed
before anything new can flow through. Keeping that list in one refreshable cache
means adding cyclone is an INSERT and one adapter file, and adding Chennai is a
row in `cities` plus its own geography.

Nothing here validates on the hot path unless asked. Lookups are dict hits.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Mapping

from app.core.logging import get_logger
from app.db import session as db
from app.schemas.domain import (
    Agency,
    Capability,
    City,
    HazardTypeRef,
    IncidentCategoryRef,
    LifelineKindRef,
    ResourceKindRef,
    Taxonomy,
)

log = get_logger(__name__)

DEFAULT_CITY = "pune"


class UnknownTaxonomyValue(LookupError):
    """Raised when a value is not in the reference tables.

    Deliberately loud. A typo'd hazard id should fail at the edge with a list of
    what is valid, not silently score zero wards and look like a quiet day.
    """

    def __init__(self, kind: str, value: str, valid: list[str]) -> None:
        self.kind, self.value, self.valid = kind, value, valid
        super().__init__(
            f"Unknown {kind} {value!r}. Known {kind}s: {', '.join(sorted(valid)) or 'none'}."
        )


@dataclass(slots=True)
class TaxonomyCache:
    cities: dict[str, City] = field(default_factory=dict)
    hazards: dict[str, HazardTypeRef] = field(default_factory=dict)
    capabilities: dict[str, Capability] = field(default_factory=dict)
    resource_kinds: dict[str, ResourceKindRef] = field(default_factory=dict)
    lifeline_kinds: dict[str, LifelineKindRef] = field(default_factory=dict)
    categories: dict[str, IncidentCategoryRef] = field(default_factory=dict)
    agencies: dict[str, Agency] = field(default_factory=dict)
    loaded: bool = False

    # ------------------------------------------------------------ lookups --
    def hazard(self, hazard_id: str) -> HazardTypeRef:
        try:
            return self.hazards[hazard_id]
        except KeyError:
            raise UnknownTaxonomyValue("hazard", hazard_id, list(self.hazards)) from None

    def category(self, category_id: str) -> IncidentCategoryRef:
        try:
            return self.categories[category_id]
        except KeyError:
            raise UnknownTaxonomyValue(
                "incident category", category_id, list(self.categories)
            ) from None

    def city(self, city_id: str) -> City:
        try:
            return self.cities[city_id]
        except KeyError:
            raise UnknownTaxonomyValue("city", city_id, list(self.cities)) from None

    # ------------------------------------------------------- capabilities --
    def kinds_providing(self, capability_id: str) -> list[str]:
        """Resource kinds that can serve a demand for this capability.

        This is the replacement for `unit.kind != demand.kind` in the solver.
        A demand asks for water rescue; boats, rescue teams and fire engines can
        all answer it, at different effectiveness. Adding a new kind of vehicle
        is a row, and the solver picks it up without being touched.
        """
        return [
            kind_id
            for kind_id, kind in self.resource_kinds.items()
            if capability_id in kind.capabilities
        ]

    def kind_can(self, kind_id: str, capability_id: str) -> bool:
        kind = self.resource_kinds.get(kind_id)
        return bool(kind and capability_id in kind.capabilities)

    def effectiveness(self, kind_id: str, capability_id: str) -> float:
        """How well this kind serves this capability, in (0, 1]. 0 if it cannot.

        Used to break ties in the solver: a fire engine can do water rescue, but
        if a boat is available and similarly close, the boat should win.
        """
        return _EFFECTIVENESS.get((kind_id, capability_id), 0.0)

    def agencies_providing(self, capability_id: str, city_id: str = DEFAULT_CITY) -> list[Agency]:
        return [
            a
            for a in self.agencies.values()
            if a.city_id == city_id and a.active and capability_id in a.capabilities
        ]

    # --------------------------------------------------------- whole thing --
    def snapshot(self, city_id: str = DEFAULT_CITY) -> Taxonomy:
        """What the frontend fetches at boot so it can stop hard-coding unions."""
        return Taxonomy(
            city=self.city(city_id),
            hazards=sorted(self.hazards.values(), key=lambda h: h.sort_order),
            capabilities=list(self.capabilities.values()),
            resource_kinds=list(self.resource_kinds.values()),
            lifeline_kinds=list(self.lifeline_kinds.values()),
            incident_categories=list(self.categories.values()),
            agencies=[a for a in self.agencies.values() if a.city_id == city_id],
        )


cache = TaxonomyCache()
_EFFECTIVENESS: dict[tuple[str, str], float] = {}


async def _optional(sql: str) -> list:
    """A reference query whose table may not exist yet.

    For rows that are additive — a deployment without them behaves as it did
    before the migration that adds them. Anything load-bearing must *not* use
    this: a missing `incident_categories` should fail loudly at startup, and
    does.
    """
    try:
        return await db.fetch(sql)
    except Exception as exc:  # noqa: BLE001 - an absent table is not an outage
        log.warning("taxonomy_optional_table_missing", sql=sql[:60], error=str(exc)[:120])
        return []


async def load(*, force: bool = False) -> TaxonomyCache:
    """Read every reference table into memory. Idempotent.

    All nine queries go out at once. They were sequential, which on a link to a
    database in another region meant nine round trips end to end: about nine
    seconds of startup from Pune to ap-northeast-2, every reload. They do not
    depend on each other, so there was never a reason to wait.

    The pool is sized above nine, so this does not queue behind itself.
    """
    if cache.loaded and not force:
        return cache

    (
        cities,
        hazards,
        caps,
        kind_caps,
        kinds,
        lifeline_kinds,
        needs_rows,
        keyword_rows,
        cats,
        agency_caps,
        agencies,
    ) = await asyncio.gather(
        db.fetch(
            """select id, name, country, timezone, languages,
                      admin_unit_singular, admin_unit_plural from cities"""
        ),
        db.fetch("select id, display_name, description, sort_order from hazard_types"),
        db.fetch("select id, label, description from capabilities"),
        db.fetch(
            "select kind_id, capability_id, effectiveness from resource_kind_capabilities"
        ),
        db.fetch("select id, display_name, default_capacity from resource_kinds"),
        db.fetch("select id, display_name, shelters_people from lifeline_kinds"),
        db.fetch(
            "select category_id, capability_id, qty_per_incident from incident_category_needs"
        ),
        # The words that recognise a report, beside the categories they
        # recognise.
        #
        # Through `_optional` because a deployment that has not run migration
        # 019 has no such table, and this gather has no `return_exceptions`: an
        # undefined-table error here would take `load()` down, and `load()`
        # runs at startup. A missing keyword table has to degrade to the
        # parser's built-in dictionary, not to a backend that will not boot.
        _optional(
            "select category_id, phrase, weight from incident_category_keywords"
        ),
        db.fetch(
            """select id, display_name, hazard_id, dedup_radius_m, dedup_window_min,
                      base_severity, life_safety from incident_categories"""
        ),
        db.fetch("select agency_id, capability_id from agency_capabilities"),
        db.fetch(
            """select id, city_id, name, short_name, kind, jurisdiction, active
                 from agencies"""
        ),
    )

    cache.cities = {
        r["id"]: City(
            id=r["id"],
            name=r["name"],
            country=r["country"],
            timezone=r["timezone"],
            languages=list(r["languages"] or []),
            admin_unit_singular=r["admin_unit_singular"],
            admin_unit_plural=r["admin_unit_plural"],
        )
        for r in cities
    }

    cache.hazards = {
        r["id"]: HazardTypeRef(
            id=r["id"],
            display_name=r["display_name"],
            description=r["description"],
            sort_order=r["sort_order"],
        )
        for r in hazards
    }

    cache.capabilities = {
        r["id"]: Capability(id=r["id"], label=r["label"], description=r["description"])
        for r in caps
    }

    by_kind: dict[str, list[str]] = {}
    _EFFECTIVENESS.clear()
    for r in kind_caps:
        by_kind.setdefault(r["kind_id"], []).append(r["capability_id"])
        _EFFECTIVENESS[(r["kind_id"], r["capability_id"])] = float(r["effectiveness"])

    cache.resource_kinds = {
        r["id"]: ResourceKindRef(
            id=r["id"],
            display_name=r["display_name"],
            default_capacity=r["default_capacity"],
            capabilities=sorted(by_kind.get(r["id"], [])),
        )
        for r in kinds
    }

    cache.lifeline_kinds = {
        r["id"]: LifelineKindRef(
            id=r["id"],
            display_name=r["display_name"],
            shelters_people=r["shelters_people"],
        )
        for r in lifeline_kinds
    }

    needs: dict[str, dict[str, int]] = {}
    for r in needs_rows:
        needs.setdefault(r["category_id"], {})[r["capability_id"]] = r["qty_per_incident"]

    keywords: dict[str, dict[str, float]] = {}
    for r in keyword_rows:
        keywords.setdefault(r["category_id"], {})[r["phrase"]] = float(r["weight"])

    cache.categories = {
        r["id"]: IncidentCategoryRef(
            id=r["id"],
            display_name=r["display_name"],
            hazard_id=r["hazard_id"],
            dedup_radius_m=r["dedup_radius_m"],
            dedup_window_min=r["dedup_window_min"],
            base_severity=r["base_severity"],
            life_safety=r["life_safety"],
            needs=needs.get(r["id"], {}),
            keywords=keywords.get(r["id"], {}),
        )
        for r in cats
    }

    by_agency: dict[str, list[str]] = {}
    for r in agency_caps:
        by_agency.setdefault(r["agency_id"], []).append(r["capability_id"])

    cache.agencies = {
        r["id"]: Agency(
            id=r["id"],
            city_id=r["city_id"],
            name=r["name"],
            short_name=r["short_name"],
            kind=r["kind"],
            jurisdiction=r["jurisdiction"],
            active=r["active"],
            capabilities=sorted(by_agency.get(r["id"], [])),
        )
        for r in agencies
    }

    cache.loaded = True
    log.info(
        "taxonomy_loaded",
        cities=len(cache.cities),
        hazards=len(cache.hazards),
        capabilities=len(cache.capabilities),
        resource_kinds=len(cache.resource_kinds),
        categories=len(cache.categories),
        agencies=len(cache.agencies),
    )
    return cache


def seed_for_tests(
    *,
    hazards: Mapping[str, str] | None = None,
    kind_capabilities: Mapping[str, Mapping[str, float]] | None = None,
) -> TaxonomyCache:
    """Populate the cache without a database, for unit tests and offline runs."""
    cache.cities = {DEFAULT_CITY: City(id=DEFAULT_CITY, name="Pune")}
    cache.hazards = {
        hid: HazardTypeRef(id=hid, display_name=name)
        for hid, name in (hazards or {"flood": "Flood"}).items()
    }
    kc = kind_capabilities or {}
    _EFFECTIVENESS.clear()
    for kind_id, caps in kc.items():
        for cap_id, eff in caps.items():
            _EFFECTIVENESS[(kind_id, cap_id)] = eff
    cache.capabilities = {
        cap: Capability(id=cap, label=cap.replace("_", " ").title())
        for caps in kc.values()
        for cap in caps
    }
    cache.resource_kinds = {
        kind_id: ResourceKindRef(
            id=kind_id, display_name=kind_id.title(), capabilities=sorted(caps)
        )
        for kind_id, caps in kc.items()
    }
    cache.loaded = True
    return cache
