# DisruptionOps — Round 1 pitch, technical brief and demo script

Team **Kala Dhua** · KH131 · PS20 — Agentic Disaster Relief & Emergency Resource
Coordinator · Kurukshetra 2.0 Hackfest, MIT ACSC Alandi

Every number in this document was read off the running code, not remembered.
Where a figure is an estimate or a benchmark rather than a live measurement, it
says so.

---

# Part 1 — The pitch

## The 90-second version

> Every city already knows a flood is coming. IMD forecasts it, NDMA publishes
> the guidelines, the ward officer has the plan on their desk. The forecast is
> not what fails.
>
> What fails is the next four hours. Five zones need help at once. There are
> twenty-three units free. Nobody can hold the whole picture, so it is done on
> phone calls — and two boats go to the same street while a third zone gets
> nothing, a truck is sent down a road a crew reported blocked twenty minutes
> ago, and afterwards nobody can say why any of it happened.
>
> DisruptionOps is the coordinator for those four hours. Reports arrive from six
> channels into one door. Every one is scored for trust before it is allowed to
> cost a vehicle. Needs are computed per zone from a taxonomy, not guessed. A
> constraint solver assigns units under real travel times over real roads — and
> re-solves the moment anything changes, weighing the cost of turning a crew
> around before it does.
>
> Two things make it different from a dashboard. First, **no model ever produces
> a number.** Needs come from the taxonomy, allocation from the solver, authority
> from a cited clause. Second, **nothing issues because a model was confident —
> it issues because a delegation clause allows it.** Inside the officer's
> authority it acts on its own and shows the clause. Outside it, it stops and
> asks.
>
> And every step is written to an append-only log with the id of the event that
> caused it, enforced by a database trigger rather than by convention. "Why did
> that boat go there" is answered by walking a chain.

## Who the users are

| User | What they get | Where |
|---|---|---|
| **Resident** | Where to go, which road to avoid, by when. Voice or text, three languages, works with no signal. | `/citizen`, no login |
| **Ward officer / Commissioner** | Risk board, incident queue, allocation planner, the decision gate, the agent trace, a Copilot with 19 tools | command console |
| **Field crew** (fire, ambulance, NDRF, drainage) | Their tasks, turn-by-turn over open roads, accept → on site → close with proof, and now: report a hazard at their own position | crew app |
| **Partner agency** | Request what you do not have, accept or decline, recorded on both sides | inter-agency handoff |

## The impact claim, stated honestly

Against a nearest-first baseline, five random seeds, one third of the fleet, the
same incident stream:

- **10% fewer** demands left uncovered — 63 against 70, of 385
- **−16%** on the worst-case time to commit a unit — 39.8 min against 47.3
- **+51%** on arrival p90, and 163 crews turned around — what re-tasking costs

That last line is on the slide on purpose. The better plan is not free: we pay
51% on the slowest arrivals to leave 10% fewer demands uncovered. That trade is a
product decision, it is measured, and the harness is in the repo so anyone can
re-run it.

And the number that decides what this system is: the same CP-SAT model solved
**once** and never revisited left 70 uncovered — identical to nearest-first. The
optimiser is not the product. Re-running it as the world changes is.

Conditions, because they matter: the harness's `--offline` synthetic city, not
Pune; synthetic arrivals; fixed service times; travel times straight-line at an
urban speed with road blocks applied, identical for every arm. The same table is
on the After-Action screen of the running system, with the conditions under it.

---

# Part 2 — Every model, every formula, and why

## 2.1 The trust score — the heart of the system

This is the piece to spend time on. It is the answer to "how do you stop a
coordinated false-report attack", which is the question this problem statement
invites and most submissions cannot answer.

**Six components, five weighted, one that discounts the rest.**

### 1. Source credibility — weight 0.22

A lookup, not a model. Who sent it is the single most informative cheap signal.

| source | value |
|---|---|
| `field` (a trained crew) | 0.95 |
| `agency` | 0.92 |
| `sensor` / gauge | 0.88 |
| `phone` (call centre) | 0.70 |
| `app` (anonymous resident) | 0.62 |
| `sim` | 0.62 |

### 2. Reporter history — weight 0.18

**Wilson lower bound at 95% confidence**, not a raw success ratio:

```
          c + 1.9208            √( c·w / n + 0.9604 )
r  =  ───────────────────  −  1.96 · ───────────────────
           n + 3.8416                     n + 3.8416
```

where `c` = confirmed, `w` = rejected, `n` = reports actually ruled on.

**Why Wilson and not c/n:** one correct report out of one is not a perfect
reporter. A raw ratio says 1.0 and the scorer believes it — which is exactly the
hole a fabricated account walks through. Wilson starts everybody near 0.5 and
moves only as evidence accumulates, so a new account cannot buy credibility
cheaply. Reports nobody has ruled on drop out of the denominator entirely, so a
reporter is never punished for being recent.

Keyed on `reporter_key` — `user:<uuid>` when signed in, `device:<id>` otherwise.
The device half is not identity: a random id the app generates for itself, tied
to no name, discarded with site data. It carries one fact, "this phone has
reported before and here is how those turned out".

> **Live in your database right now:** 154 reporters over 418 reports. The
> fabricated burst `device:demo-burst-0001` sits at **0.305**; a consistently
> correct reporter sits at **0.676**; anyone never ruled on sits at exactly
> **0.500**. This is worth showing on screen.

### 3. Location plausibility — weight 0.18

```
base = 0.35 + 0.65 × ward_risk          (ward_risk ∈ [0,1], from the hazard model)
value = base × 0.6   if the category does not match the active hazard
```

With no hazard running it returns a flat 0.5 — no opinion, rather than a guess.
An unknown category is treated as plausible rather than penalised, because a new
category is a configuration change, not a lie.

### 4. Independent corroboration — weight 0.22

```
value = 1 − 1/(1 + 0.9n)        n ≥ 1
value = 0.25                    n = 0
```

**Saturating, deliberately.** The second independent report is worth far more
than the ninth. Counting linearly is precisely what lets a burst look like truth
— thirty reports would score thirty times one report, and a bot farm produces
thirty reports trivially. Here n=1 → 0.47, n=2 → 0.64, n=3 → 0.73, n=10 → 0.90.
The curve is nearly flat past four.

`n` counts *distinct devices and accounts*, not reports. Reports from one source
do not corroborate each other.

### 5. Evidence — weight 0.10

```
no photo            → 0.45
photo, unexamined   → 0.85
photo examined      → clamp(0.65 + 0.30 × agreement, 0.15, 0.95)
```

`agreement ∈ [−1, 1]` comes from the vision model comparing what the picture
shows against what was typed. **It moves in both directions** — a photo of a dry
street attached to "the road is flooded" costs the report, because that is also
what a fabricated report looks like. Capped at 0.95 so a photo can never carry a
report past the gate on its own.

### 6. Anomaly — a discount, not a component

```
anomaly  = min(0.5,  0.12 × (reports_from_this_source_in_15min − 3))   if > 3
         + min(0.35, 0.15 × near_identical_texts_from_this_source)
         + 0.35                                if implied travel > 180 km/h

final = raw × (1 − 0.70 × anomaly)
```

**Why a discount and not a subtraction.** We tried subtracting a fixed amount. A
nine-report burst from one device with duplicated text still landed in "needs
corroboration" instead of being held — the most obvious fabrication pattern
there is, waved through. A multiplicative discount scales with how strong the
rest of the evidence looked, which is the right shape: a confident-looking report
from a suspicious source is exactly the one that should lose the most.

### The routing decision

```
≥ 0.72  → auto_confirmed        may commit a unit on its own
≥ 0.35  → needs_corroboration   visible, but will not spend a vehicle
< 0.35  → quarantined           recorded, held for a human, never clusters
```

**The one asymmetry, and say this out loud:** for a life-safety category
(`person_stranded`, medical), anything above the quarantine floor is confirmed
rather than held. Sending a boat to nobody wastes a boat. Not sending one drowns
somebody. The two mistakes are not comparable and the thresholds should not
pretend they are.

### Why deterministic and not a classifier

There is no labelled dataset of true and false disaster reports for Pune, and
there will not be one before this is deployed. More importantly an officer has to
be able to *audit* a decision that cost a vehicle — `components` and `reasons`
come back with every score, in words. A gradient-boosted model would be a number
nobody can argue with. This runs offline, in microseconds, and explains itself.

---

## 2.2 Report understanding — three tiers, cheapest first

1. **Keyword match**, English / Hindi / Marathi. Handles most reports at zero
   cost and zero latency.
2. **XLM-RoBERTa-large-XNLI** (`joeddav/xlm-roberta-large-xnli`) zero-shot, only
   when the keyword pass is not confident. Chosen because it is zero-shot across
   100 languages — we have no training data, and a model that needs none is the
   only honest option. It classifies into the taxonomy; **it cannot invent a
   category.**
3. **LLM** as the last resort, for prose only.

Speech: **Sarvam AI** (`saarika:v2.5`, `saaras:v2.5` for translation). Chosen
over a general multilingual recogniser specifically because it is trained on the
code-mixed Indian speech people actually use — the general ones drop exactly the
English loan words that carry the location.

Vision: **Qwen2.5-VL**, self-hosted. It answers a fixed JSON contract — water
depth band against a named reference object, people visible, time of day, image
quality, whether it looks staged. It may raise or lower trust. **It may never set
category, severity or dispatch.**

---

## 2.3 Allocation — OR-Tools CP-SAT

**Demand weight:**

```
weight = severity² × (1 + population_at_risk / 10 000)
```

Severity squared so it dominates; population breaks ties *within* a severity
band. A severity-5 incident affecting 200 people still outranks a severity-3
affecting 20 000, which is the correct ethical ordering and is worth being able
to defend.

**Objective:**

```
minimise   Σ  weight × eta × 10 / capability_fit
         + Σ  10 000 × weight × (1 − served)
```

The 10 000 coverage penalty means **coverage is lexicographically first**:
leaving a demand uncovered costs more than any achievable arrival time. Only
once everything reachable is covered does the solver start shaving minutes.

**Constraints:** capability match · unit capacity · one unit per job ·
`MAX_ETA_MINUTES = 45` reachability · travel times over the current road graph,
with roads crews have reported blocked *removed from the graph*.

**Switching cost — the re-planning rule:**

```
multiplier = 1 + (1.6 − 1) × (0.25 + 0.75 × progress)
```

A committed crew costs 1.15× to turn around at the start of its journey and 1.6×
when nearly there. This is the difference between a system that re-plans and one
that thrashes: without it, every new report yanks crews sideways and nothing ever
arrives. A committed unit is only re-tasked when the gain genuinely beats the
disruption.

Falls back to a **greedy heuristic honouring the same switching cost** if CP-SAT
cannot solve in time, so a degraded solve is still a coherent one.

**Travel times:** Mapbox Directions over real roads, OSRM as fallback,
straight-line as the last resort — and the UI says which one it used, because a
straight-line estimate presented as a route is a lie a driver acts on.

---

## 2.4 Forecast — Gamma-Poisson recurrence per zone

Conjugate Bayesian, chosen because it has exactly the property that matters: **it
starts as a prior and becomes the ward's own record**, with a number saying how
far along that journey it is.

```
prior:      Gamma(α₀, β₀),  β₀ = PRIOR_HOURS = 12,  α₀ = city_rate × 12
posterior:  λ = (α₀ + k) / (β₀ + t)        k = incidents seen, t = hours observed
expected:   λ × horizon_hours
P(≥1):      1 − e^(−expected)
evidence:   t / (β₀ + t)        0 = entirely the city prior, 1 = entirely this ward
```

**`evidence` is the honesty column.** A forecast resting on the city-wide prior
rather than on this ward's own history says so, in words, in the rationale. An
officer deciding whether to move a pump at 2 a.m. must be told the difference
between "this ward floods every monsoon" and "we have no idea, so we guessed from
the city average". β₀ = 12 hours means a single unusual night cannot swing it.

Flood forcing: **GloFAS v4 via Open-Meteo** — keyless, no licence we can lose.

### Forecast-driven prepositioning

When a capability is projected short, spare units are *proposed* toward the ward
most likely to need them — through the same delegation gate as a dispatch.

```
confidence = 0.30 + 0.40 × evidence + 0.30 × hit_rate
hit_rate   = (hits + 1) / (resolved + 2)          Laplace-smoothed
```

Laplace smoothing so a capability with no history returns exactly 0.5 — "no
opinion" — rather than looking certain or hopeless on one data point. Every
proposal is recorded and settled later against whether a named unit was actually
tasked in that ward inside the horizon. **A capability that keeps being proposed
and never used drifts down until a human has to agree.** That is the loop
working, not failing.

Three limits held deliberately: it proposes and never moves; it never touches a
committed unit; below a 1.0-unit projected shortfall it says nothing, because an
officer who learns these proposals are noise will stop reading the ones that are
not.

---

## 2.5 The delegation gate — why this is agentic rather than automated

Actions are matched to a delegation clause by **exact `action_key`**, not fuzzy
similarity. (pgvector is in the database; similarity is deliberately *not* the
default. "Close enough to a clause that permits this" is not a sentence anybody
should be able to say about requisitioning NDRF.)

- Inside the cited authority → **auto-issues**, and the clause is displayed.
- Outside it, or below the confidence bar → **officer approval queue**.

Clauses come from real documents: **NDMA Guidelines on Management of Urban
Flooding (2010)** and the **PMC Disaster Management Plan**. `preposition_equipment`
is delegated to the Ward Officer under PMC DMP 2023 cl. 6.1, which is why most
prepositioning proposals auto-issue.

**The sentence to say to a judge:** *No action issues because a model was
confident. It issues because a clause delegates it.*

---

## 2.6 The audit log

Append-only `events` table. Every row carries `causation_id` — the id of the
event that caused it — written at the time, not inferred from timestamps
afterwards. Two database triggers, `events_no_delete` and `events_no_update`,
refuse DELETE and UPDATE.

Enforced by the database, not by convention. We know the trigger works because it
broke our own reset endpoint and we had to design around it rather than disable
it.

---

## 2.7 What is deterministic, and why that is the headline

| Decision | Produced by |
|---|---|
| What a report is about | taxonomy (model may only choose from it) |
| What a zone needs | `incident_category_needs` table |
| How much to trust a report | six-component arithmetic, shown |
| Which unit goes where | OR-Tools CP-SAT |
| Whether it may issue | a cited delegation clause |
| Prose, summaries, explanation | LLM |

**No model produces a number.** That is the line. Remove every model from this
system and it still coordinates a flood — slightly less well at reading Marathi
free text, and blind to photos. Remove the solver and you have a chatbot.

---

# Part 3 — Mapping to the 100-mark rubric

### Problem Understanding — 20
Lead with the reframe: *the forecast is not what fails, the allocation does.*
Name the four failure modes you are fixing (duplicated effort, slow decisions,
misdirected units, no learning) and show slide 2's map — 23 units, 5 simultaneous
needs. Name the users and what each actually gets. Cite NDMA and the PMC DM Plan
by name to show you read the domain, not just the problem statement.

### Innovation — 20
Three things, in this order:
1. **Authority as the gate, not model confidence** — an exact-clause delegation
   matrix. Almost nobody does this.
2. **Trust scored before clustering**, so a burst cannot manufacture its own
   corroboration — with the saturating curve and the multiplicative anomaly
   discount as the evidence you thought about the attack.
3. **A closed loop that learns from outcomes** — prepositioning proposals scored
   against whether the unit was actually used; reporter reliability moving with
   verdicts.

### Technical Approach — 15
The architecture diagram, then the three things that survive scrutiny: CP-SAT
with switching cost, Gamma-Poisson with an evidence figure, and the append-only
log enforced by a trigger. Say "no model produces a number" here.

### Prototype / Progress — 20
**This is where the marks are. Demo, do not describe.** Follow Part 4. The
deployed URL, three live surfaces, a real re-plan, and the Copilot what-if.

### Feasibility & Impact — 10
The three numbers, the trade-off you are paying for them, and the cost story:
every live feed is keyless or free-tier, a new city is configuration rather than
a procurement round. Say what happens when things fail — offline outbox, OSRM
fallback, greedy fallback, vision service optional.

### Team Coordination — 10
Every member must be able to answer a question about a part they did not write.
Agree in advance who takes trust, who takes the solver, who takes the gate, who
drives the demo. Do not let one person answer everything.

### Rules & Originality — 5
Public repo, open sources, no licensed dependency. Be explicit that the benchmark
harness is in the repo and re-runnable.

---

# Part 4 — The demo script

Eight minutes. Rehearse it twice. **Open every tab before you start.**

**0. Before you walk in.** Backend awake (Render cold-starts ~50s — hit the URL
five minutes early). Demo world started. VLM tunnel up if you are showing photos.
Phone on mobile data for the citizen app.

**1. The problem, on the map (45s).** Command console, risk board. Five zones
lit, the inventory count. *"Twenty-three units free. Five zones need help. This
is the decision the system exists to make."*

**2. A resident reports (60s).** On the phone, on mobile data. Speak it in
Marathi or Hindi. Show what it was read as and the confidence. Show the trust
score and the reason. *"One anonymous report. Unconfirmed. It has not spent a
vehicle — and it should not have."*

**3. Corroboration crosses the threshold (60s).** Incident queue → **"3
neighbours report this"**. Say plainly: *"These are simulated and labelled as
such in the log — each with its own device id, because only independent sources
corroborate."* Watch report count rise, trust rise, status flip to confirmed, and
a unit get committed. **This is the best 60 seconds in the demo.** It shows the
mechanism, not a number.

**4. A crew changes the world (60s).** Crew app: report a hazard at your own
position, or declare a road blocked. Back on the console: the road is out of the
graph, the route re-solves, and the ETA changes. *"A crew's word re-planned the
city in under two seconds."*

**5. The gate (60s).** Decision gate. One auto-issued with its clause displayed,
one waiting. *"That one did not wait because the model was unsure. It waited
because the clause reserves it to a named officer."*

**6. The Copilot (90s).** Ask "what are my options for the next three hours" →
three strategies, each priced by the solver. Then "what if I move Pump 3 to
Baner" → it re-solves on a copy and shows the hole it leaves in Kothrud. Then
"apply the second one" → through the gate, not around it.

**7. Why (45s).** Agent trace: pick the assignment and walk the causal chain back
to the report that caused it. *"Append-only, enforced by a database trigger. Not
a convention we promise to keep."*

**8. Close (20s).** The three numbers, and the URL.

## If something breaks

Say what happened and keep going — a team that debugs calmly in front of judges
reads as competent, not unprepared. Every failure path is designed: the citizen
app opens offline from the outbox, routing degrades to OSRM then straight-line
and says which, the solver degrades to greedy, and the vision service being down
costs a report its photo bonus and nothing else.

---

# Part 5 — Questions you will be asked

**"How do you stop a coordinated false-report attack?"**
Four ways at once: trust is scored *before* clustering so a burst cannot
manufacture corroboration; corroboration saturates so thirty reports are worth
barely more than four; corroboration counts distinct devices, not reports; and
the anomaly discount is multiplicative, so a burst from one device with
duplicated text is held, not merely downgraded. Then show
`device:demo-burst-0001` at 0.305.

**"Is this just a wrapper around an LLM?"**
No model produces a number. Walk the table in §2.7. Offer: remove every model and
it still coordinates a flood.

**"What if the model is confidently wrong?"**
It cannot dispatch. The gate is authority, not confidence. And a model cannot
invent a category — it may only choose from the taxonomy.

**"Why not LangGraph / a framework?"**
The orchestrator is deterministic on purpose: fixed order, one transaction, every
step writing an event with its cause. A framework would add a scheduler we would
then have to explain to an auditor. *(Note: `langgraph` and `langchain` are in
requirements.txt but imported nowhere — remove them before submission so nobody
finds them and asks.)*

**"Does it work for a second city / another disaster?"**
Taxonomy is rows, not enums — hazards, categories, capabilities, resource kinds.
A new hazard or a new city is configuration. Row-level security scopes staff
reads by city.

**"Where did the benchmark numbers come from?"**
Five random seeds, one third of the fleet, the same incident stream, against a
nearest-first baseline. Harness is in the repo.

**"What does not work yet?"**
Answer honestly and briefly — see Part 6. Judges reward a team that knows its own
gaps far more than one that claims none.

---

# Part 6 — What is still open

Ranked by what a judge is most likely to notice.

1. **Ward boundary drawing.** `Configuration.tsx:719` still sends
   `boundary: null`, so a new ward has no polygon. Visible if anyone opens
   configuration.
2. **`langgraph` / `langchain` in `requirements.txt`, imported nowhere.** Remove
   them. This is a five-minute change that closes an originality question.
3. **No frontend build has been run against the last three rounds of changes.**
   Run `npm run build` before you deploy again, not during the demo.
4. **19 of 420 reports never clustered into an incident.** Expected behaviour
   (clustering declined them) but nobody has looked at *why* those nineteen.
5. **Supabase leaked-password protection is off.** One toggle in Auth settings.
6. **Render cold start ~50s on free tier.** Not fixable today; just warm it up
   before you present.
7. **No automated tests on the intake path.** The `/citizen/report` outage that
   lasted three hours would have been caught by one.

---

# Part 7 — The one-paragraph answer to "what did you build"

> A coordinator for the first four hours of an urban disaster. Reports arrive
> from six channels into one ingest path, are scored for trust on six auditable
> components before they are allowed to cost a vehicle, and are clustered so ten
> calls about one street become one incident. Needs per zone come from a
> taxonomy; allocation comes from an OR-Tools CP-SAT solver minimising
> population-weighted arrival time under capability, capacity and reachability
> constraints, over real travel times with blocked roads removed from the graph.
> It re-solves whenever anything changes, weighing a switching cost before
> turning a committed crew around. Nothing issues because a model was confident —
> it issues because a delegation clause from the NDMA guidelines or the PMC plan
> permits it, matched exactly, and the clause is shown. A Gamma-Poisson
> recurrence per ward proposes moving equipment before it is needed, and is
> scored afterwards against whether the unit was actually used. Every step is
> written to an append-only log carrying the id of the event that caused it,
> enforced by a database trigger. Three interfaces read one shared state: a
> resident's PWA that works offline in three languages, a command console with a
> 19-tool Copilot, and a crew app where a blocked road re-plans the city in
> seconds.
