"""Named, scripted cases: for the stage, and as regression checks against the DB.

The random demo shows the system working. These show it working *on purpose*:
each one sets up a specific situation the judges ask about, lets the real
pipeline (intake, trust, dedup, needs, CP-SAT, re-plan, gate, executor, mesh)
handle it, and then checks the outcome against what should have happened.
Nothing in a scenario touches allocation directly; every step goes through the
same door a person, a crew, a camera or the mesh would use.

    GET  /api/v1/demo/scenarios              the list
    POST /api/v1/demo/scenarios/{name}/run   run one; progress appears as beats
    GET  /api/v1/demo/scenarios/last         steps and checks of the last run

Scenarios
---------
  flood_cascade        several reports of one flooded road in a minute:
                       one incident, not five; a pump goes once
  block_on_approach    a crew reports the road it is driving is closed:
                       that unit, and only that unit, is re-routed, once
  unit_breakdown       a committed unit breaks down mid-route:
                       its job is covered by another unit on the next plan
  officer_redirect     an officer cancels a unit and sends it elsewhere:
                       the next three re-plans do not undo it
  officer_hold         an officer stands a unit down and holds it:
                       the planner does not task it while held
  camera_fire_mesh     a camera sees fire while its ward has no internet;
                       the packet arrives over the mesh: one incident, and a
                       dispatch goes back into the mesh outbox
  mesh_blackout_sos    three people report over the mesh, one packet heard by
                       two gateways: two reports, one duplicate, zero spoofs
  multi_hazard_surge   fire, collapse and stranded people across wards at
                       once: the shortfall is recorded, not hidden
  ghaziabad_monsoon    a monsoon night around IPEC, Ghaziabad: local units
                       (not Pune's) are tasked
  pccoe_pawana_night   the PCMC zone round PCCOE (migration 029): Pawana over
                       its banks at Ravet and Chinchwad, an MIDC fire with gas,
                       an underpass under water at Akurdi, a wall down in
                       Nigdi: PCMC units (PC-*) are tasked, not Pune's
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.core.config import settings
from app.core.logging import get_logger
from app.db import session as db
from app.demo import runner
from app.incidents import intake
from app.mesh import envelope, service as mesh
from app.ops import operations as ops
from app.world import events as ev
from app.world.clock import WALL

log = get_logger(__name__)
_rng = random.Random(20260926)


@dataclass(slots=True)
class Run:
    name: str
    started: float = field(default_factory=time.time)
    steps: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    running: bool = True
    error: str | None = None
    ctx: dict[str, Any] = field(default_factory=dict)

    def step(self, text: str, **detail: Any) -> None:
        self.steps.append({"t": round(time.time() - self.started, 1), "text": text, **detail})
        runner.state.beat("scenario", f"[{self.name}] {text}", **{
            k: v for k, v in detail.items() if k in ("incidentId", "resourceId", "wardId")
        })

    def check(self, what: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": what, "ok": bool(ok), "detail": detail})
        self.step(("PASS " if ok else "FAIL ") + what + (f": {detail}" if detail else ""))

    def as_dict(self) -> dict:
        return {
            "name": self.name, "running": self.running, "error": self.error,
            "steps": self.steps, "checks": self.checks,
            "passed": sum(1 for c in self.checks if c["ok"]),
            "failed": sum(1 for c in self.checks if not c["ok"]),
        }


LAST: Run | None = None


# ------------------------------------------------------------------ helpers ---
async def _ward(prefer: str | None = None) -> dict:
    row = await db.fetchrow(
        """
        select id, name, extensions.ST_X(centroid::extensions.geometry) lng,
               extensions.ST_Y(centroid::extensions.geometry) lat
          from wards
         where city_id = 'pune' and ($1::text is null or name ilike '%' || $1 || '%')
         order by random() limit 1
        """,
        prefer,
    )
    if row is None:
        row = await db.fetchrow(
            "select id, name, extensions.ST_X(centroid::extensions.geometry) lng, "
            "extensions.ST_Y(centroid::extensions.geometry) lat from wards "
            "where city_id='pune' order by random() limit 1"
        )
    return dict(row)


async def _report(run: Run, ward: dict, category: str, note: str, *,
                  spread: float = 0.0004, device: str | None = None,
                  source: str = "app") -> intake.IntakeResult:
    lng = ward["lng"] + _rng.uniform(-spread, spread)
    lat = ward["lat"] + _rng.uniform(-spread, spread)
    res = await intake.receive(
        ward_id=ward["id"], category=category, location=(lng, lat), note=note,
        source=source, reporter_name="Scenario",
        device_id=device or f"scn-{uuid.uuid4().hex[:6]}", city_id="pune", clock=WALL,
    )
    outcome = ("new incident" if res.created_incident
               else "merged" if res.linked else "held")
    run.step(f"Report in {ward['name']}: {category.replace('_', ' ')} — {outcome} "
             f"(trust {res.trust.score:.0%})", incidentId=res.incident_id, wardId=ward["id"])
    return res


async def _replan(run: Run, why: str) -> None:
    await runner.force_replan()
    plan = runner.state.last_plan or {}
    run.step(f"Re-plan ({why}): {plan.get('headline', 'no plan')}")


async def _ticks(n: int) -> None:
    """Let the world move: the tick loop if it is running, else wall time."""
    await asyncio.sleep(n * runner.TICK_SECONDS)


async def _unit_on(incident_id: str | None) -> dict | None:
    if not incident_id:
        return None
    row = await db.fetchrow(
        """
        select r.id, r.label, a.id::text assignment_id, a.status::text status
          from assignments a join resources r on r.id = a.resource_id
         where a.incident_id = $1::uuid and a.sim_run_id is null
           and a.status::text in ('proposed','approved','en_route','on_site')
         order by a.created_at desc limit 1
        """,
        incident_id,
    )
    return dict(row) if row else None


async def _busiest_unit() -> dict | None:
    row = await db.fetchrow(
        """
        select r.id, r.label, a.incident_id::text incident_id, i.title,
               a.id::text assignment_id, a.progress
          from assignments a
          join resources r on r.id = a.resource_id
          join incidents i on i.id = a.incident_id
         where a.sim_run_id is null and a.status::text in ('en_route','proposed')
           and a.route is not null and a.progress < 0.6
         order by i.severity desc, a.created_at desc limit 1
        """
    )
    return dict(row) if row else None


async def _ensure_work(run: Run) -> dict | None:
    """Make sure at least one unit is driving somewhere, then return it."""
    unit = await _busiest_unit()
    if unit:
        return unit
    ward = await _ward()
    await _report(run, ward, "person_stranded", "Family on a roof, water rising")
    await _report(run, ward, "person_stranded", "People stuck on the terrace, need a boat")
    await _replan(run, "work to do")
    await _ticks(2)
    return await _busiest_unit()


def _mesh_text(type_: str, body: dict, human: str = "") -> str:
    return envelope.encode(type_, body, key=settings.mesh_hmac_key, human=human)


# ----------------------------------------------------------------- scenarios --
async def flood_cascade(run: Run) -> None:
    ward = await _ward("Baner")
    first = await _report(run, ward, "flooded_road", "Road under knee-deep water")
    for note in ("Water over the road near the signal", "Cannot drive through, flooded",
                 "Flooded road, cars stuck", "Knee deep water on the main road"):
        await _report(run, ward, "flooded_road", note)
    n = await db.fetchval(
        "select count(*) from incidents where ward_id = $1 and category = 'flooded_road' "
        "and status <> 'resolved' and created_at > now() - interval '2 minutes'",
        ward["id"],
    )
    run.check("five reports of one road became at most two incidents", (n or 0) <= 2,
              f"{n} incident(s)")
    await _replan(run, "flooding")
    units = await db.fetchval(
        "select count(*) from assignments where incident_id = $1::uuid and "
        "status::text in ('proposed','approved','en_route','on_site')",
        first.incident_id,
    ) if first.incident_id else 0
    run.check("the flooded road is covered without duplicate dispatch",
              0 < (units or 0) <= 2, f"{units} unit(s) on it")


async def block_on_approach(run: Run) -> None:
    unit = await _ensure_work(run)
    if unit is None:
        run.check("a unit is driving", False, "no unit en route to test with")
        return
    run.step(f"{unit['label']} is on its way to {unit['title']}", resourceId=unit["id"])
    before = await db.fetchval(
        "select count(*) from events where kind = 'assignment.rerouted' and subject_id = $1",
        unit["id"],
    )
    point = await db.fetchrow(
        """
        select extensions.ST_X(p::extensions.geometry) lng, extensions.ST_Y(p::extensions.geometry) lat
          from (select extensions.ST_LineInterpolatePoint(route,
                         least(0.9, progress + (1 - progress) * 0.5)) p
                  from assignments where id = $1::uuid) x
        """,
        unit["assignment_id"],
    )
    block_id = await db.fetchval(
        """
        insert into road_blocks (city_id, location, reason, reported_by)
        values ('pune', extensions.ST_SetSRID(extensions.ST_MakePoint($1,$2),4326)::extensions.geography,
                'Tree down across the road (scenario)', $3)
        returning id::text
        """,
        point["lng"], point["lat"], unit["label"],
    )
    await ev.append(clock=WALL, kind=ev.Kind.ROAD_BLOCKED, actor="field:scenario",
                    subject_type="road_block", subject_id=block_id,
                    payload={"reason": "Tree down across the road", "resource_id": unit["id"]})
    run.step(f"{unit['label']} reports a tree down on the road ahead", resourceId=unit["id"])
    await _replan(run, "road closed")
    await _replan(run, "again, to prove it does not flicker")
    after = await db.fetchval(
        "select count(*) from events where kind = 'assignment.rerouted' and subject_id = $1",
        unit["id"],
    )
    run.check("the unit was re-routed at most once for one block",
              (after or 0) - (before or 0) <= 1, f"{(after or 0) - (before or 0)} re-route(s)")
    still = await db.fetchval(
        "select incident_id::text from assignments where resource_id = $1 and "
        "status::text in ('proposed','approved','en_route','on_site') "
        "order by created_at desc limit 1",
        unit["id"],
    )
    run.check("it kept its job (re-routed, not re-tasked)", still == unit["incident_id"])


async def unit_breakdown(run: Run) -> None:
    unit = await _ensure_work(run)
    if unit is None:
        run.check("a unit is driving", False)
        return
    await db.execute(
        "update resources set status = 'offline', updated_at = now() where id = $1", unit["id"]
    )
    await db.execute(
        "update assignments set status = 'cancelled' where id = $1::uuid", unit["assignment_id"]
    )
    run.step(f"{unit['label']} breaks down on the way to {unit['title']}",
             resourceId=unit["id"])
    await _replan(run, "unit lost")
    cover = await _unit_on(unit["incident_id"])
    run.check("another unit took the job", bool(cover and cover["id"] != unit["id"]),
              cover["label"] if cover else "nobody")
    await db.execute(
        "update resources set status = 'available', updated_at = now() where id = $1",
        unit["id"],
    )
    run.step(f"{unit['label']} repaired and back in service")


async def officer_redirect(run: Run) -> None:
    unit = await _ensure_work(run)
    if unit is None:
        run.check("a unit is driving", False)
        return
    target = await db.fetchrow(
        """
        select i.id::text, i.title from incidents i
         where i.status <> 'resolved' and i.sim_run_id is null and i.id <> $1::uuid
         order by i.severity desc, i.created_at desc limit 1
        """,
        unit["incident_id"],
    )
    if target is None:
        ward = await _ward()
        res = await _report(run, ward, "structural_damage", "Wall collapsed onto the lane")
        target = {"id": res.incident_id, "title": "the new collapse"}
    result = await ops.propose_cancel(
        resource_id=unit["id"], reason="Scenario: the Commissioner wants it elsewhere",
        instead={"kind": "redirect", "incident_id": target["id"],
                 "incident_title": target["title"]},
        actor="Scenario officer",
    )
    run.step(f"Officer cancels {unit['label']} and sends it to {target['title']}: "
             f"{result['summary']}", resourceId=unit["id"])
    for k in range(3):
        await _replan(run, f"pressure test {k + 1}")
    now_on = await db.fetchval(
        "select incident_id::text from assignments where resource_id = $1 and "
        "status::text in ('proposed','approved','en_route','on_site') "
        "order by created_at desc limit 1",
        unit["id"],
    )
    run.check("three re-plans later it is still where the officer sent it",
              now_on == target["id"])
    back = await db.fetchval(
        "select count(*) from assignments where resource_id = $1 and incident_id = $2::uuid "
        "and created_at > now() - interval '5 minutes' "
        "and status::text in ('proposed','approved','en_route','on_site')",
        unit["id"], unit["incident_id"],
    )
    run.check("it was not sent back to the job it was taken off", not back)


async def officer_hold(run: Run) -> None:
    unit = await _ensure_work(run)
    if unit is None:
        run.check("a unit is driving", False)
        return
    await ops.propose_cancel(
        resource_id=unit["id"], reason="Scenario: crew has been on shift 14 hours",
        instead={"kind": "hold", "minutes": 20}, actor="Scenario officer",
    )
    run.step(f"Officer stands {unit['label']} down and holds it for 20 minutes",
             resourceId=unit["id"])
    ward = await _ward()
    await _report(run, ward, "person_stranded", "Elderly man stranded, needs help")
    await _replan(run, "new demand while held")
    tasked = await db.fetchval(
        "select count(*) from assignments where resource_id = $1 and "
        "created_at > now() - interval '1 minute' and "
        "status::text in ('proposed','approved','en_route','on_site')",
        unit["id"],
    )
    run.check("the held unit was not tasked", not tasked)


async def camera_fire_mesh(run: Run) -> None:
    ward = await _ward("Kasba")
    body = {"id": f"cam_07-fire-{uuid.uuid4().hex[:6]}", "n": "cam_07", "k": "fire",
            "c": 0.81, "f": "4/5", "v": 1, "x": "smoke and flames from a shop awning",
            "la": ward["lat"] + 0.0003, "lo": ward["lng"] - 0.0002, "t": int(time.time())}
    text = _mesh_text("S", body, human="[FIRE] camera cam_07 81%")
    run.step(f"Camera cam_07 in {ward['name']} detects fire; no internet there, "
             "packet goes into the mesh", wardId=ward["id"])
    await asyncio.sleep(1.5)
    res = (await mesh.receive([text], gateway_id="scenario-gateway"))[0]
    run.step(f"Gateway forwards it: {res.get('outcome')}")
    run.check("the mesh packet became a report", res.get("outcome") in ("report", "linked"),
              str(res))
    await _replan(run, "fire")
    await mesh.sync_outbox()
    out = await db.fetchval(
        "select count(*) from mesh_outbox where created_at > now() - interval '2 minutes'"
    )
    run.check("something was queued back into the mesh", (out or 0) > 0,
              f"{out} outbound message(s)")


async def mesh_blackout_sos(run: Run) -> None:
    ward = await _ward()
    texts = []
    for k, (cat, note) in enumerate([
        ("person_stranded", "Two children and grandmother on first floor, water rising"),
        ("flooded_road", "Main road under water, no vehicles can pass"),
    ]):
        body = {"id": f"sos-{uuid.uuid4().hex[:8]}", "n": f"phone-{k}", "k": cat,
                "la": ward["lat"] + _rng.uniform(-0.002, 0.002),
                "lo": ward["lng"] + _rng.uniform(-0.002, 0.002),
                "x": note, "t": int(time.time())}
        texts.append(_mesh_text("R", body, human=f"[SOS] {note[:40]}"))
    run.step(f"{ward['name']} has no signal; two people report through bitchat",
             wardId=ward["id"])
    first = await mesh.receive(texts, gateway_id="gw-a")
    dup = await mesh.receive(texts[:1], gateway_id="gw-b")
    run.check("both reports arrived", all(r.get("outcome") in ("report", "linked", "held")
                                          for r in first), str([r.get("outcome") for r in first]))
    run.check("the same packet via a second gateway is a duplicate",
              dup[0].get("outcome") == "duplicate", str(dup[0]))
    if settings.mesh_hmac_key:
        forged = texts[0].replace('"n":"phone-0"', '"n":"phone-X"')
        bad = await mesh.receive([forged], gateway_id="gw-c")
        run.check("a tampered packet is refused", not bad[0].get("ok"), str(bad[0]))
    else:
        run.step("MESH_HMAC_KEY is not set, so signatures are not checked "
                 "(packets are scored as unsigned)")


async def multi_hazard_surge(run: Run) -> None:
    for prefer, cat, note in [
        ("Shivaji", "fire", "Godown on fire, thick smoke"),
        ("Yerwada", "structural_damage", "Building wall collapsed"),
        ("Hadapsar", "person_stranded", "Twenty people cut off by water"),
        ("Kothrud", "person_stranded", "People on roofs, water rising"),
        ("Aundh", "flooded_road", "Underpass fully flooded"),
    ]:
        ward = await _ward(prefer)
        await _report(run, ward, cat, note)
        await _report(run, ward, cat, note + " (second caller)")
    await _replan(run, "surge")
    plan = runner.state.last_plan or {}
    run.check("the plan ran", bool(plan), plan.get("headline", ""))
    run.step(f"Uncovered demands recorded: {len(plan.get('uncovered') or [])}. "
             "Ask the Copilot for options to see mutual aid priced.")


async def ghaziabad_monsoon(run: Run) -> None:
    """A monsoon night around IPEC, Sahibabad: Hindon over its banks at
    Karhera, underpasses under water, a wall down in Khoda, a factory fire in
    the industrial area. Every report goes through intake like any other, and
    the plan should task Ghaziabad units, not Pune's."""
    for prefer, cat, note, n in [
        ("Karhera", "person_stranded", "Hindon water entering houses, families on rooftops", 3),
        ("Arthala", "person_stranded", "Low-lying lanes cut off by river water, elderly inside", 2),
        ("Vaishali", "flooded_road", "Vaishali metro underpass fully under water, cars stuck", 2),
        ("Kaushambi", "flooded_road", "Knee-deep water at Kaushambi bus depot road", 1),
        ("Khoda", "structural_damage", "Boundary wall collapsed after rain, people trapped", 2),
        ("IPEC", "fire", "Fire at a factory in Sahibabad Site IV, thick smoke towards IPEC", 2),
        ("Indirapuram", "power_line", "Live wire down in waterlogged street, man injured", 1),
        ("Vasundhara", "fallen_tree", "Tree fell across road in Sector 5, blocking ambulances", 1),
        ("Mohan Nagar", "flooded_road", "Water over the road at Mohan Nagar crossing", 1),
        ("Shalimar", "waterlogging", "Colony waterlogged, drains overflowing into homes", 1),
        ("Karhera", "supply_shortage", "Relief camp running out of drinking water and food", 1),
        ("Karhera", "shelter_full", "Karhera school shelter full, more families arriving", 1),
        ("Kavi Nagar", "heat_casualty", "Elderly woman collapsed in humid heat at relief queue", 1),
    ]:
        ward = await _ward(prefer)
        for k in range(n):
            await _report(run, ward, cat, note if k == 0 else f"{note} (caller {k + 1})")
    await _replan(run, "Ghaziabad monsoon")
    tasked = await db.fetchval(
        """
        select count(*) from assignments a
          join incidents i on i.id = a.incident_id
         where i.ward_id like 'w-gzb-%' and a.resource_id like 'GZB-%'
           and a.status::text not in ('cancelled', 'complete')
        """
    )
    run.check("Ghaziabad units tasked to Ghaziabad incidents", (tasked or 0) > 0,
              f"{tasked or 0} local assignments")


async def pccoe_pawana_night(run: Run) -> None:
    """Several hazards at once round PCCOE, Akurdi–Nigdi. Every report goes
    through intake like any other; the plan should task PCMC units (PC-*)."""
    zone = await db.fetchval("select count(*) from wards where id like 'w-pc-%'")
    if not zone:
        run.check("PCMC zone present (migration 029)", False, "apply migration 029 first")
        return
    for prefer, cat, note, n in [
        ("Ravet", "person_stranded", "Pawana water entering houses near Ravet bridge, family on terrace", 3),
        ("Chinchwad Gaon", "person_stranded", "River water at Morya Gosavi ghat lanes, elderly couple inside", 2),
        ("Akurdi", "flooded_road", "Akurdi station underpass fully under water, two-wheelers stuck", 2),
        ("Pimpri", "waterlogging", "Pimpri camp market waterlogged, shops flooding", 1),
        ("Chikhli", "fire", "Fire at a chemical unit in MIDC, strong gas smell spreading", 2),
        ("Chikhli", "air_quality", "Smoke and gas smell, people coughing near Sane chowk", 1),
        ("Nigdi", "structural_damage", "Compound wall collapsed on parked cars after rain, someone trapped", 2),
        ("Akurdi", "power_line", "Live wire fallen in waterlogged lane near PCCOE, sparks", 1),
        ("Thergaon", "fallen_tree", "Tree across the road near Aditya Birla hospital, ambulances blocked", 1),
        ("Ravet", "shelter_full", "S B Patil school shelter full, more families arriving", 1),
        ("Ravet", "supply_shortage", "Ravet relief camp short of drinking water", 1),
    ]:
        ward = await _ward(prefer)
        for k in range(n):
            await _report(run, ward, cat, note if k == 0 else f"{note} (caller {k + 1})")
    await _replan(run, "Pawana night round PCCOE")
    tasked = await db.fetchval(
        """
        select count(*) from assignments a
          join incidents i on i.id = a.incident_id
         where i.ward_id like 'w-pc-%' and a.resource_id like 'PC-%'
           and a.status::text not in ('cancelled', 'complete')
        """
    )
    run.check("PCMC units tasked to PCMC incidents", (tasked or 0) > 0, f"{tasked or 0} local assignments")


SCENARIOS: dict[str, tuple[str, Callable[[Run], Awaitable[None]]]] = {
    "flood_cascade": ("Many reports of one flooded road become one incident and one dispatch.", flood_cascade),
    "block_on_approach": ("A crew's road is closed ahead of it: re-routed once, job kept.", block_on_approach),
    "unit_breakdown": ("A unit breaks down mid-route: someone else covers its job.", unit_breakdown),
    "officer_redirect": ("An officer cancels a unit and sends it elsewhere: the planner obeys.", officer_redirect),
    "officer_hold": ("An officer holds a unit: the planner leaves it alone.", officer_hold),
    "camera_fire_mesh": ("A camera sees fire in a ward with no internet; it arrives over the mesh.", camera_fire_mesh),
    "mesh_blackout_sos": ("People report over bitchat; duplicates and forgeries are caught.", mesh_blackout_sos),
    "multi_hazard_surge": ("Five hazards at once: shortfalls recorded, not hidden.", multi_hazard_surge),
    "ghaziabad_monsoon": ("Monsoon night around IPEC, Ghaziabad: Hindon flood, underpasses, collapse, fire.", ghaziabad_monsoon),
    "pccoe_pawana_night": ("Round PCCOE: Pawana flood at Ravet and Chinchwad, MIDC fire and gas, underpass, collapse.", pccoe_pawana_night),
}


def catalogue() -> list[dict]:
    return [{"name": k, "description": v[0]} for k, v in SCENARIOS.items()]


async def run(name: str) -> Run:
    global LAST
    if name not in SCENARIOS:
        raise KeyError(name)
    r = Run(name=name)
    LAST = r
    try:
        await SCENARIOS[name][1](r)
    except Exception as exc:  # noqa: BLE001 - report, don't crash the demo
        log.exception("scenario_failed", scenario=name)
        r.error = f"{type(exc).__name__}: {exc}"
        r.step(f"Scenario stopped: {r.error}")
    finally:
        r.running = False
    return r


def start(name: str) -> Run:
    """Run in the background; poll `LAST` for progress."""
    if name not in SCENARIOS:
        raise KeyError(name)
    task_run = Run(name=name)

    async def _go() -> None:
        global LAST
        LAST = task_run
        try:
            await SCENARIOS[name][1](task_run)
        except Exception as exc:  # noqa: BLE001
            log.exception("scenario_failed", scenario=name)
            task_run.error = f"{type(exc).__name__}: {exc}"
            task_run.step(f"Scenario stopped: {task_run.error}")
        finally:
            task_run.running = False

    asyncio.get_running_loop().create_task(_go())
    return task_run
