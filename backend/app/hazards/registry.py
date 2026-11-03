"""Adapter registry.

Import side effects are avoided: adapters register themselves explicitly in
`load_adapters()`, so the set of live hazards is one readable list rather than
whatever happened to be imported.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.hazards.base import HazardAdapter, Maturity
log = get_logger(__name__)

# Keyed by plain hazard id, so an adapter for a hazard that is not in the
# HazardType constants (a new city's cyclone, say) registers exactly the same.
_registry: dict[str, HazardAdapter] = {}


def register(adapter: HazardAdapter) -> HazardAdapter:
    if adapter.hazard in _registry:
        raise RuntimeError(f"Adapter already registered for {adapter.hazard}")
    _registry[adapter.hazard] = adapter
    log.info(
        "hazard_adapter_registered",
        hazard=str(adapter.hazard),
        maturity=adapter.maturity,
        sources=len(adapter.sources),
    )
    return adapter


def get(hazard: str) -> HazardAdapter:
    try:
        return _registry[hazard]
    except KeyError as exc:
        raise LookupError(f"No adapter registered for hazard {hazard!r}") from exc


def all_adapters() -> list[HazardAdapter]:
    return list(_registry.values())


def live_adapters() -> list[HazardAdapter]:
    return [a for a in _registry.values() if a.maturity is Maturity.LIVE]


def load_adapters() -> None:
    """Called once at startup. Adding a hazard is a line here plus one file,
    and a row in `hazard_types` so the rest of the platform knows it exists."""
    if _registry:
        return
    from app.hazards.air import AirQualityAdapter
    from app.hazards.fire import WildfireAdapter
    from app.hazards.flood import FloodAdapter
    from app.hazards.heat import HeatwaveAdapter
    from app.hazards.seismic import SeismicAdapter

    register(FloodAdapter())
    register(HeatwaveAdapter())
    register(AirQualityAdapter())
    register(WildfireAdapter())
    register(SeismicAdapter())
