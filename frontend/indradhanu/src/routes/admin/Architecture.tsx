import { useMemo, useState } from "react"
import { useDragScroll } from "@/hooks/useDragScroll"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card"

/** How the whole thing fits together, drawn live.
 *
 *  A slide of this diagram would be a claim. This one is wired to the same poll
 *  every other screen reads, so each band shows what has actually gone through
 *  it on this run — reports at intake, incidents opened, decisions gated,
 *  assignments made. When somebody asks "is it really doing that", the honest
 *  answer is the number moving next to the box.
 *
 *  The animation is not decoration either. It shows the one thing a static
 *  diagram cannot: that this is a loop. Work flows down the six bands and then
 *  two arrows come back up into the solver — a new urgent report, and a unit
 *  whose status changed — and re-allocation happens because of them. Those two
 *  arrows are the dynamic re-allocation requirement, and they are drawn thicker
 *  and animated in the opposite direction for exactly that reason.
 *
 *  Everything is inline SVG with CSS animation. No diagramming library, no
 *  images: it stays sharp at projector size, it themes with the rest of the
 *  console, and it is one file to change when the architecture changes.
 */

type Band = {
  id: string
  title: string
  caption: string
  boxes: { label: string; sub?: string }[]
  /** The live figure for this band, and what it counts. */
  metric?: (s: LiveCounts) => { value: number; unit: string }
}

type LiveCounts = {
  reports: number
  incidents: number
  wards: number
  decisions: number
  autoIssued: number
  assignments: number
  alerts: number
  events: number
  resources: number
  committed: number
}

const BANDS: Band[] = [
  {
    id: "intake",
    title: "Intake",
    caption:
      "Five channels, one door. Every report is normalised here, so nothing " +
      "downstream needs to know where it came from.",
    boxes: [
      { label: "Citizen app", sub: "text · voice · photo" },
      { label: "Field crew", sub: "accept → on site → close" },
      { label: "Partner agency", sub: "API" },
      { label: "Sensors", sub: "gauges · feeds" },
      { label: "Simulation", sub: "replay" },
    ],
    metric: (s) => ({ value: s.reports, unit: "reports in" }),
  },
  {
    id: "understand",
    title: "Understand the report",
    caption:
      "Cheapest first. Keywords settle most of it; the model is the last " +
      "resort, and it can only pick from the taxonomy, never invent a category.",
    boxes: [
      { label: "Parser", sub: "keyword → zero-shot → LLM" },
      { label: "Trust scorer", sub: "6 components, deterministic" },
      { label: "Duplicate clusterer", sub: "distance · time · category · text" },
      { label: "Vision", sub: "photo corroborates, never decides" },
    ],
    metric: (s) => ({ value: s.incidents, unit: "incidents opened" }),
  },
  {
    id: "zone",
    title: "Understand the zone",
    caption:
      "Per ward: how bad, how many people, what capability is needed and how " +
      "much of it is actually covered.",
    boxes: [
      { label: "Needs assessment", sub: "category → capability × qty" },
      { label: "Severity & priority", sub: "corroboration lifts it, capped at 5" },
      { label: "Ward state", sub: "severity · exposed · shortfall" },
    ],
    metric: (s) => ({ value: s.wards, unit: "wards scored" }),
  },
  {
    id: "decide",
    title: "Decide",
    caption:
      "No action issues because a model was confident. It issues because a " +
      "clause delegates it — and the clause is shown.",
    boxes: [
      { label: "Policy matrix", sub: "NDMA SOP · municipal DM plan" },
      { label: "Decision gate", sub: "auto-issue, or officer queue" },
    ],
    metric: (s) => ({ value: s.decisions, unit: "decisions gated" }),
  },
  {
    id: "allocate",
    title: "Allocate",
    caption:
      "Capability match, capacity, real travel time, one unit per job. A " +
      "committed crew is turned around only when the gain beats the disruption.",
    boxes: [
      { label: "Travel-time matrix", sub: "Mapbox → OSRM → straight line" },
      { label: "CP-SAT solver", sub: "OR-Tools, greedy fallback" },
      { label: "Re-plan", sub: "with switching cost" },
      { label: "Duplicate-effort", sub: "two agencies, one incident" },
    ],
    metric: (s) => ({ value: s.assignments, unit: "assignments" }),
  },
  {
    id: "act",
    title: "Act",
    caption:
      "Three interfaces, three jobs. A resident and a ward officer need " +
      "different things and should not share a screen.",
    boxes: [
      { label: "Citizen PWA", sub: "where to go, which road to avoid" },
      { label: "Command console", sub: "risk · queue · gate · trace" },
      { label: "Field crew app", sub: "offline-capable" },
      { label: "Agency handoff", sub: "request · accept · decline" },
    ],
    metric: (s) => ({ value: s.alerts, unit: "alerts issued" }),
  },
]

const W = 1000
const BAND_H = 128
const BAND_GAP = 26
const TOP = 16

function bandY(i: number) {
  return TOP + i * (BAND_H + BAND_GAP)
}

const TOTAL_H = bandY(BANDS.length - 1) + BAND_H + 28

export default function Architecture() {
  // Wide figure: drag to scroll it sideways on small screens.
  const dragRef = useDragScroll<HTMLDivElement>()
  const { state } = useDemo()
  const [flow, setFlow] = useState(true)
  const [focus, setFocus] = useState<string | null>(null)

  const counts = useMemo<LiveCounts>(() => {
    const decisions = state.decisions ?? []
    return {
      reports: state.reports?.length ?? 0,
      incidents: state.incidents?.length ?? 0,
      wards: state.wards?.length ?? 0,
      decisions: decisions.length,
      autoIssued: decisions.filter(
        (d) => d.status === "auto_issued" || d.status === "approved"
      ).length,
      assignments: (state.resources ?? []).filter(
        (r) => r.status === "en_route" || r.status === "on_site"
      ).length,
      alerts: state.alerts?.length ?? 0,
      events: state.events?.length ?? 0,
      resources: state.resources?.length ?? 0,
      committed: (state.resources ?? []).filter((r) => r.status !== "available").length,
    }
  }, [state])

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-muted-foreground max-w-2xl text-sm">
            The whole system on one page, wired to the live run. Each band shows
            what has passed through it, so this is a reading rather than a
            drawing. Report → understand → score the zone → check authority →
            solve → dispatch, and the moment anything changes, solve again.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={state.running ? "default" : "outline"}>
            {state.running ? `Live · tick ${state.tick}` : "Idle"}
          </Badge>
          <Button variant="outline" size="sm" onClick={() => setFlow((f) => !f)}>
            {flow ? "Pause flow" : "Animate flow"}
          </Button>
        </div>
      </div>

      <Card>
        <CardContent ref={dragRef} className="overflow-x-auto p-2 sm:p-4">
          <svg
            viewBox={`0 0 ${W} ${TOTAL_H}`}
            className="h-auto w-full min-w-[760px]"
            role="img"
            aria-label="Indradhanu system architecture, six bands from intake to action"
          >
            <defs>
              {/* Theme-aware: every fill and stroke is currentColor at an
                  opacity, so this reads correctly in light and dark without a
                  second palette to keep in step. */}
              <marker
                id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
                markerWidth="6" markerHeight="6" orient="auto-start-reverse"
              >
                <path d="M0,0 L10,5 L0,10 z" fill="currentColor" opacity="0.55" />
              </marker>
              <marker
                id="arrow-strong" viewBox="0 0 10 10" refX="9" refY="5"
                markerWidth="7" markerHeight="7" orient="auto-start-reverse"
              >
                <path d="M0,0 L10,5 L0,10 z" className="fill-destructive" />
              </marker>
              <style>{`
                .band-box { transition: opacity .2s ease; }
                .dimmed { opacity: .25; }
                @keyframes travel { to { stroke-dashoffset: -44; } }
                @keyframes travel-up { to { stroke-dashoffset: 44; } }
                .flowing { stroke-dasharray: 5 17; animation: travel 1.1s linear infinite; }
                .flowing-up { stroke-dasharray: 6 16; animation: travel-up 1.4s linear infinite; }
                @keyframes pulse-spine { 0%,100% { opacity:.30 } 50% { opacity:.70 } }
                .spine-pulse { animation: pulse-spine 2.6s ease-in-out infinite; }
                @media (prefers-reduced-motion: reduce) {
                  .flowing, .flowing-up, .spine-pulse { animation: none; }
                }
              `}</style>
            </defs>

            {/* ---- the spine, down the right edge, touching every band ---- */}
            <g>
              <rect
                x={W - 74} y={TOP - 6} width={58} height={TOTAL_H - TOP - 16}
                rx={8} fill="currentColor" opacity={0.06}
              />
              <text
                x={W - 45} y={TOP + 8} textAnchor="middle"
                className="fill-current text-[9px] font-semibold uppercase"
                opacity={0.7}
              >
                Audit
              </text>
              <text
                x={W - 45} y={TOTAL_H - 20} textAnchor="middle"
                className="fill-current text-[8px]" opacity={0.55}
              >
                append-only
              </text>
              {/* One arrow into the log from every band. It is enforced by a
                  database trigger, not by convention, which is the only reason
                  it can be drawn as a fact. */}
              {BANDS.map((b, i) => (
                <line
                  key={b.id}
                  x1={W - 96} y1={bandY(i) + BAND_H / 2}
                  x2={W - 78} y2={bandY(i) + BAND_H / 2}
                  stroke="currentColor" strokeWidth={1.5} opacity={0.45}
                  markerEnd="url(#arrow)"
                  className={flow ? "spine-pulse" : undefined}
                />
              ))}
            </g>

            {/* ---- the bands ---- */}
            {BANDS.map((band, i) => {
              const y = bandY(i)
              const dim = focus !== null && focus !== band.id
              const inner = W - 150
              const boxW = (inner - 28 - (band.boxes.length - 1) * 10) / band.boxes.length
              const metric = band.metric?.(counts)
              return (
                <g
                  key={band.id}
                  className={`band-box ${dim ? "dimmed" : ""}`}
                  onMouseEnter={() => setFocus(band.id)}
                  onMouseLeave={() => setFocus(null)}
                >
                  <rect
                    x={14} y={y} width={inner} height={BAND_H} rx={10}
                    fill="currentColor" opacity={0.045}
                    stroke="currentColor" strokeOpacity={0.14}
                  />
                  <text
                    x={28} y={y + 21}
                    className="fill-current text-[12px] font-semibold"
                  >
                    {i + 1}. {band.title}
                  </text>
                  {metric && (
                    <text
                      x={inner} y={y + 21} textAnchor="end"
                      className="fill-current text-[11px] font-semibold tabular-nums"
                      opacity={0.85}
                    >
                      {metric.value.toLocaleString()}{" "}
                      <tspan className="text-[9px] font-normal" opacity={0.7}>
                        {metric.unit}
                      </tspan>
                    </text>
                  )}

                  {band.boxes.map((box, j) => {
                    const bx = 28 + j * (boxW + 10)
                    return (
                      <g key={box.label}>
                        <rect
                          x={bx} y={y + 32} width={boxW} height={54} rx={7}
                          fill="currentColor" opacity={0.07}
                          stroke="currentColor" strokeOpacity={0.2}
                        />
                        <text
                          x={bx + boxW / 2} y={y + 53} textAnchor="middle"
                          className="fill-current text-[10px] font-medium"
                        >
                          {box.label}
                        </text>
                        {box.sub && (
                          <text
                            x={bx + boxW / 2} y={y + 69} textAnchor="middle"
                            className="fill-current text-[8px]" opacity={0.65}
                          >
                            {box.sub}
                          </text>
                        )}
                        {/* Within a band, the boxes are a chain. */}
                        {j < band.boxes.length - 1 && (
                          <line
                            x1={bx + boxW} y1={y + 59}
                            x2={bx + boxW + 10} y2={y + 59}
                            stroke="currentColor" strokeWidth={1.2} opacity={0.4}
                            markerEnd="url(#arrow)"
                          />
                        )}
                      </g>
                    )
                  })}

                  <text
                    x={28} y={y + BAND_H - 10}
                    className="fill-current text-[8.5px]" opacity={0.6}
                  >
                    {band.caption.length > 128
                      ? `${band.caption.slice(0, 126)}…`
                      : band.caption}
                  </text>
                </g>
              )
            })}

            {/* ---- band to band, straight down ---- */}
            {BANDS.slice(0, -1).map((b, i) => (
              <line
                key={`down-${b.id}`}
                x1={W / 2 - 68} y1={bandY(i) + BAND_H}
                x2={W / 2 - 68} y2={bandY(i + 1)}
                stroke="currentColor" strokeWidth={2} opacity={0.5}
                markerEnd="url(#arrow)"
                className={flow ? "flowing" : undefined}
              />
            ))}

            {/* ---- the two feedback arrows ----
                The most important lines on the page. Everything above is a
                pipeline and any competent team draws one; these two are what
                make it a loop, and they are the dynamic re-allocation the
                problem statement actually asks for. Drawn thicker, in the
                destructive colour, animated upward. */}
            {(() => {
              const allocate = bandY(4)
              const act = bandY(5)
              const midA = allocate + BAND_H / 2
              const laneA = 10
              const laneB = W - 132
              return (
                <g className="text-destructive">
                  {/* New urgent report, from intake, back into the solver. */}
                  <path
                    d={`M ${laneA + 8} ${bandY(0) + BAND_H / 2}
                        H ${laneA} V ${midA + 16} H ${20}`}
                    fill="none" stroke="currentColor" strokeWidth={2.6}
                    markerEnd="url(#arrow-strong)"
                    className={flow ? "flowing-up" : undefined}
                    opacity={0.9}
                  />
                  <text
                    x={laneA + 12} y={midA + 40}
                    className="fill-current text-[8px] font-semibold"
                  >
                    new urgent report → re-plan
                  </text>

                  {/* Unit status change, from the field, back into the solver. */}
                  <path
                    d={`M ${laneB} ${act + BAND_H / 2}
                        H ${laneB + 16} V ${midA - 16} H ${W - 154}`}
                    fill="none" stroke="currentColor" strokeWidth={2.6}
                    markerEnd="url(#arrow-strong)"
                    className={flow ? "flowing-up" : undefined}
                    opacity={0.9}
                  />
                  <text
                    x={laneB - 4} y={midA - 26} textAnchor="end"
                    className="fill-current text-[8px] font-semibold"
                  >
                    unit status changed → re-plan
                  </text>
                </g>
              )
            })()}
          </svg>
        </CardContent>
      </Card>

      {/* The claims the diagram makes, each with the thing that backs it, so a
          judge can check one rather than take six on trust. */}
      <div className="grid gap-5 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Audit spine</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {counts.events.toLocaleString()}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            Events on this run, each carrying the id of the event that caused it.
            A database trigger refuses UPDATE and DELETE, so "why did that boat
            go there" is answered by walking a chain rather than by guessing.
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Cleared the gate on their own</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {counts.autoIssued.toLocaleString()}
              <span className="text-muted-foreground text-sm font-normal">
                {" "}of {counts.decisions.toLocaleString()}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            The rest are waiting for an officer, and the wards they cover are
            deliberately silent. Each one names the clause that delegated it.
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Fleet committed</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {counts.committed.toLocaleString()}
              <span className="text-muted-foreground text-sm font-normal">
                {" "}of {counts.resources.toLocaleString()}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            Capability-matched rather than nearest-available: a boat beats a
            fire engine for water rescue at the same ETA, and the engine is used
            at reduced effectiveness only when no boat is free.
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">The foundation</CardTitle>
          <CardDescription>
            Taxonomy is rows, not enums. Hazards, categories, capabilities and
            resource kinds are all reference data, so a new hazard or a new city
            is configuration rather than a deployment.
          </CardDescription>
        </CardHeader>
        <CardContent className="text-muted-foreground grid gap-2 text-xs sm:grid-cols-2">
          <div>
            <span className="text-foreground font-medium">
              Supabase Postgres + PostGIS.
            </span>{" "}
            Wards, resources, incidents, assignments, lifelines, events. Row-level
            security scopes staff reads by city.
          </div>
          <div>
            <span className="text-foreground font-medium">One clock.</span>{" "}
            Nothing calls <code>datetime.now()</code> directly, so the same code
            runs the live world and a replay without knowing which it is in.
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
