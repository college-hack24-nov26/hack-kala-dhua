# Evidence fusion: how Indradhanu decides what is real

> Repeating isn't spam. Agreement between independent sources is proof.

## The problem with scoring reports one by one

Most reporting systems score each report alone, as if it were a single phone call, and then guard against abuse by
penalising sources that report often or repeat themselves. In a disaster this fails twice:

1. **Frightened people call again and again.** A family on a roof sends ten messages in ten minutes. A per-report
   rubric reads that as a spam burst and holds it.
2. **Sensor networks are many eyes behind one key.** A drone swarm or a camera grid legitimately reports several
   places within minutes. The old rubric scored every drone report at about 16 % and quarantined it, so a drone
   that had seen people trapped could not get a single unit sent.

Indradhanu has more than people as witnesses: edge-AI cameras with a vision-language model, a drone swarm, LoRa
field sensors, crews in the field, official notices, and (next) satellite products. The question to ask is not "how
credible is this one message?" It is **"how many independent kinds of evidence agree about this place right now?"**

## The model (`backend/app/incidents/fusion.py`)

Belief is computed **per incident**, from every observation of every kind, in log-odds. Each item adds or subtracts
a weight, the order doesn't matter, and every weight can be shown to an officer.

```
belief = prior(category) + Σ over kinds of evidence [ diminishing Σ over independent witnesses ]
weight(item) = polarity × own confidence × logit(reliability) × decay(age) × proximity(distance)
```

| Concept | Rule | Why |
|---|---|---|
| **Witness** | All observations from one reporter, device, node or fleet are one witness: its strongest observation, plus at most +0.30 for persistence | A scared caller counts once as evidence, and their repeats are flagged as **distress** (urgency), never as spam |
| **Kind of evidence (modality)** | citizen, mesh, camera/VLM, drone, LoRa sensor, field crew, official, satellite. Independent witnesses of one kind add with diminishing weight 1, 1/1.45, 1/1.9, … | Ten independent people add more than one, but not ten times as much |
| **Human cap** | People-only evidence tops out at about 83 %: "likely", enough to dispatch, never "confirmed" | A coordinated burst of fake accounts cannot manufacture certainty, and honest callers are not punished |
| **Independent agreement** | A second kind of evidence (camera, drone, sensor) adds fully on top | "People and an independent sensor agree" is what makes an incident **confirmed** |
| **Contradiction** | Same rule, negative sign: "water has receded", a drone over an empty dry street, an ordinary camera frame of the spot, a crew ruling it false | The system can also become sure something is *not* there and stand down |
| **Own confidence** | VLM / detector confidence, drone report confidence, classification confidence scale the item | A drone that says "0.44, thin report" counts less than one that says 0.9 |
| **Reliability** | Class prior (crew 0.95, official 0.95, drone 0.88, LoRa 0.85, camera 0.80, citizen 0.68, signed mesh 0.66, unsigned 0.52), replaced by the witness's own record once 3+ of its reports have been ruled on by a person or crew (Laplace-smoothed) | Learns which cameras, drones and people are right, and never learns from its own automatic labels (the old view taught itself the drones were "always wrong" because it had quarantined them) |
| **Decay** | Half-life by category (fire 45 min, stranded 90, flooded road 120, structural 240); ignored after 4 half-lives | An old report does not keep a finished incident "confirmed" |
| **Proximity** | Evidence near but not merged into the incident weighs `exp(-d / 200 m)` | A camera 50 m away matters; one 600 m away barely does |

Status: **confirmed** ≥ 85 % · **likely** ≥ 60 % · **uncertain** · **doubtful** < 25 %.

## What belief controls: verify first, then dispatch

| Belief vs floor | Planner does | Floor |
|---|---|---|
| At or above | Covers every need of the incident (CP-SAT, as before) | 30 % for life-safety categories (stranded, fire, collapse), 55 % otherwise |
| Below | Plans **one field-assessment need only**: the nearest assessor, and the agent graph's aerial node sends a **drone to look** | |

The asymmetry is deliberate: sending a boat to nobody wastes a boat; not sending one drowns somebody. A single
frightened caller about people in water is dispatched at once (about 53 %). A single anonymous report of a blocked
drain is checked first.

This closes the loop by itself:

```
report -> belief uncertain -> drone / camera / assessor looks -> their observation is evidence
       -> belief crosses the floor -> full dispatch          (or: contradiction -> doubtful -> stand down)
```

Every change of at least 5 points is written to the event log as `incident.belief_changed`, with each witness and
its weight. When the dispatch floor is crossed, the planner re-plans at once.

## Where it runs

| Step | Code |
|---|---|
| Every report: belief recomputed inside intake (savepoint, so a failure never loses a report) | `incidents/intake.py::_recompute_incident` |
| New evidence events (`report.linked`, `incident.opened`, `field.drone_situation`, `field.drone_need`, crew `field.*`, `incident.resolved`): re-fuse, one incident at a time, bounded queue | `agents/event_router.py::_evidence_hook` → `fusion.schedule` |
| Verify-first gate in the planner | `agents/replan.py::_open_demands` |
| Explain it | `GET /api/v1/incidents/{id}/belief`, and the incident page card "1b · What our edge devices see here" (belief, each kind of evidence, reasons, distress, next step) |
| Per-report score (still shown per report) | `incidents/trust.py`: repeats are no longer an anomaly; only physically impossible movement (a spoofed location) still discounts a report |

## Worked examples (`backend/tests/test_fusion.py`)

| Situation | Belief | Outcome |
|---|---|---|
| One caller: people stranded | ~53 %, uncertain | dispatched (life safety) |
| Same caller ten times | +<8 points; flagged as distress | still one witness |
| 40 people, no sensor | ~83 %, likely | dispatched, "no independent sensor has seen it yet" |
| Citizen + VLM camera + drone | > 85 %, confirmed | dispatched |
| Drone alone, its own confidence 0.44 | above the life-safety floor | dispatched |
| Citizen says flooded, drone sees a dry empty street | doubtful | verify / stand down |
| Unsigned mesh report, inside the drone map | below the floor | "send a drone to look" |

## Limits, stated plainly

* Reliability priors are set by hand until outcomes exist. They are tunable constants at the top of `fusion.py`, not
  learned yet.
* "Independent" means a different reporter key or a different kind of sensor. Two cameras looking at the same
  smoke from the same pole are not truly independent. Diminishing weights limit the damage; they don't remove it.
* Proximity uses straight-line distance, not line of sight.
* Drone evidence comes from the live swarm link in memory: one API instance holds the link.
* Satellite products are a modality in the model, but no satellite feed is wired yet (see the SAR note).
