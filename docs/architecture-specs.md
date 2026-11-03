# DisruptionOps — two architecture diagrams to draw

Two versions of the same system. Draw the simple one for the deck and the
detailed one for the documentation or a backup slide. They must agree — the
detailed one is the simple one with the boxes opened up, in the same order,
reading in the same direction.

Palette for both: navy `#11253C` · slate `#3D5468` · muted `#6E8194` ·
terracotta `#B23A24` · teal `#2E7D8F` · panel `#F3F6F9` · borders `#9FAFBE`.

---

# ARCHITECTURE 1 — THE SIMPLE ONE

Five boxes in a ring, one arrow each, and one arrow that closes the loop.
If somebody remembers one picture from the deck, this is the one.

## Layout

Draw it as a **horizontal chain of five boxes**, left to right, with a single
return arrow sweeping back underneath from the last box to the second.

```
   ┌────────┐   ┌────────────┐   ┌──────────┐   ┌──────────┐   ┌────────┐
   │ REPORT │──▶│ UNDERSTAND │──▶│  DECIDE  │──▶│ ALLOCATE │──▶│  ACT   │
   └────────┘   └────────────┘   └──────────┘   └──────────┘   └────────┘
                      ▲                                            │
                      └────────────────────────────────────────────┘
                         anything changes  →  solve again
```

## What goes in each box

| Box | One line under the title |
|---|---|
| **REPORT** | Someone tells us something. Five channels, one door. |
| **UNDERSTAND** | What is it, is it true, and is it already open? |
| **DECIDE** | Is this action delegated to us, or does an officer sign it? |
| **ALLOCATE** | Which unit, over which roads, at what cost to move it. |
| **ACT** | The crew goes. The resident is told where to walk. |

## The return arrow

Draw it **thicker than the others and in terracotta `#B23A24`**. Label it:

> anything changes → solve again

This arrow is the entire product. Every competitor draws boxes one to five.
Nobody draws the arrow back.

## The one thing underneath

A single bar running the full width below all five boxes, navy fill, white text:

> **Every step writes down what caused it. The database refuses to let that be edited.**

## Drawing notes

- Five boxes, all the same size. No box is more important than another.
- Arrows between boxes: thin, muted `#6E8194`.
- Return arrow: thick, terracotta, curved under the chain, arrowhead landing on
  the left edge of UNDERSTAND.
- Nothing else. No icons, no sub-bullets, no logos. The simple diagram earns its
  keep by being the one thing on the slide.

---

# ARCHITECTURE 2 — THE DETAILED ONE

The same five stages, opened up. Six horizontal bands stacked top to bottom,
with a vertical audit column down the right edge touching every band.

Read down. One band per row, band name on the left, components in a row of
cards to its right.

```
 ┌──────────────────────────────────────────────────────────────┬────────┐
 │ 1  INTAKE            [5 source cards] → intake.receive()      │        │
 ├──────────────────────────────────────────────────────────────┤        │
 │ 2  UNDERSTAND THE REPORT   [4 cards]                          │ AUDIT  │
 ├──────────────────────────────────────────────────────────────┤  LOG   │
 │ 3  UNDERSTAND THE ZONE     [3 cards]                          │        │
 ├──────────────────────────────────────────────────────────────┤ append │
 │ 4  DECIDE                  [2 cards]                          │  only  │
 ├──────────────────────────────────────────────────────────────┤        │
 │ 5  ALLOCATE                [5 cards]                          │        │
 ├══════════════════════════════════════════════════════════════┤        │
 │ ▲  RE-PLAN   ← new report (band 1) · unit status (band 6)     │        │
 ├──────────────────────────────────────────────────────────────┤        │
 │ 6  ACT                     [4 cards]                          │        │
 └──────────────────────────────────────────────────────────────┴────────┘
   FOUNDATION  Postgres + PostGIS · RLS by city · taxonomy is rows, not enums
```

## Band 1 — INTAKE

Five cards, then all five arrow into one wide box beneath them.

| Card | Sub-label |
|---|---|
| Citizen app | text · voice · photo |
| Field crew | reports from the ground |
| Partner agency | API |
| Sensors & gauges | rivers · rain · weather |
| Simulation / replay | past events, what-if |

Wide box beneath: **`intake.receive()`**

Caption: *Five channels, one door. Nothing downstream knows where a report came from.*

## Band 2 — UNDERSTAND THE REPORT

Four cards, chained left to right with thin arrows.

| Card | Sub-label |
|---|---|
| Parser | keyword → XLM-R → LLM last |
| Trust scorer | 6 components, deterministic |
| Duplicate clusterer | ten calls, one incident |
| Vision · Qwen2.5-VL | a photo corroborates only |

Caption: *The category can only come from the taxonomy. The model cannot invent one.*

## Band 3 — UNDERSTAND THE ZONE

Three cards.

| Card | Sub-label |
|---|---|
| Needs assessment | category → capability × quantity |
| Severity & priority | corroboration lifts it, capped at 5 |
| Ward state | exposed · required · met · shortfall |

Caption: *What each zone needs, and how much of it is actually covered.*

## Band 4 — DECIDE

Two cards, the second wider than the first.

| Card | Sub-label |
|---|---|
| Policy / delegation matrix | NDMA SOP · PMC DM plan · exact match on action key |
| Decision gate | auto-issue **with the clause shown**, or officer queue |

Caption: *No action issues because a model was confident. It issues because a clause delegates it.*

## Band 5 — ALLOCATE

Five cards.

| Card | Sub-label |
|---|---|
| Travel-time matrix | Mapbox → OSRM fallback |
| OR-Tools CP-SAT | capability · capacity · ETA |
| Re-plan | with switching cost |
| Duplicate-effort | two agencies, one incident |
| Prepositioning | forecast moves spare units |

Caption: *Capability-matched, never nearest-available: a boat beats a fire engine for water rescue at the same ETA.*

## The RE-PLAN strip

Between band 5 and band 6. Full width, terracotta tint, terracotta bold text,
with a left-pointing or upward triangle at the start:

> **▲ RE-PLAN** — a new urgent report (band 1), or a unit status change
> (band 6), sends the solver round again.

Draw this strip **visually heavier than the band separators**. It is the loop,
and on a stack of six neat bands the loop is the thing a reader will otherwise
miss.

## Band 6 — ACT

Four cards.

| Card | Sub-label |
|---|---|
| Citizen PWA | where to go · 3 languages · offline |
| Command console | 16 screens + Copilot, 19 tools |
| Field crew app | accept → on site → close, with proof |
| Inter-agency handoff | request · accept · recorded both sides |

Caption: *Three interfaces, three different jobs.*

## The audit column

A tall navy panel down the **right edge**, running the full height of all six
bands, with a small arrow from each band into it.

Heading: **AUDIT LOG**

Body, three short paragraphs:
- Every step writes an event carrying the id of the event that caused it.
- A database trigger refuses DELETE and UPDATE.
- So "why did that boat go there?" is answered by walking a chain, not by guessing.

## The foundation line

One line across the very bottom, small, bold, slate:

> **FOUNDATION**  Supabase Postgres + PostGIS · row-level security scopes staff
> reads by city · taxonomy is rows, not enums — a new hazard or a new city is
> configuration, not a release.

---

# Drawing rules that apply to both

**Read one way.** The simple diagram reads left to right, the detailed one top
to bottom. Never mix directions inside one picture.

**Band tints, very pale.** Intake `#EAF1F7` · Understand `#EAF2EC` · Zone
`#FAF3E6` · Decide `#F7EAEA` · Allocate `#EFEBF5` · Act `#EAF2EC`. Cards on top
of them stay white with a `#9FAFBE` hairline border.

**One accent per picture.** Terracotta is reserved for the loop — the return
arrow in the simple one, the RE-PLAN strip in the detailed one. If terracotta
appears anywhere else, the loop stops being the thing the eye finds.

**Sub-labels never smaller than about 60% of the card title.** The old version of
this diagram failed for exactly this reason: the structure was right and the
text was 8px, so on a projector it was a coloured pattern with no information in
it.

**No icons in the detailed one.** Six bands and twenty-three cards is already a
lot to look at; icons would add decoration to something that needs the opposite.
Icons belong in the simple diagram, if anywhere.

---

# If you only have time for one

Draw the simple one. Five boxes and a red arrow back, at a size people can read
from the back of a room, beats a detailed diagram nobody can parse in the twenty
seconds it will be on screen.

The detailed one is worth having ready as a backup slide for the question
*"but how does it actually work?"* — which is a question you want, because the
answer is good.
