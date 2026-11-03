import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  AlertTriangle, ChevronLeft, ChevronRight, Loader2, Pause, Play,
} from "lucide-react"
import { request } from "@/api/httpClient"
import { LiveMap } from "@/components/map/LiveMap"
import { MapStage } from "@/components/map/MapStage"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Alert, AlertDescription } from "@/components/ui/alert"

/** The run, as it happened, at any point in it.
 *
 *  Everything on this screen already existed in `events` and none of it was
 *  visible. The most valuable text this system produces is written at the
 *  moment the solver moves a unit —
 *
 *    "Moved from 'Fallen tree — Warje–Malwadi' (6% of the way there) because
 *     'Structural damage — Sinhagad Road' is severity 4 with 8,900 exposed."
 *
 *  — and until now it went into a jsonb column and stayed there. This is the
 *  screen that reads those back in order, against the map they happened on.
 *
 *  What this is NOT: a comparison. There is one recorded stream and it was
 *  produced by the CP-SAT allocator. Showing what nearest-first would have done
 *  means re-solving, which is `scripts/benchmark_strategies.py`, not this. The
 *  claim this screen supports is auditability — why did this unit move, what
 *  caused it, who authorised the alert — and not optimality.
 */

type Session = {
  id: number
  startedAt: string
  endedAt: string
  events: number
  durationMinutes: number
}

type Catalogue = Record<string, {
  title: string; category: string; severity: number; wardId: string | null
  location: [number, number]
}>

type Fleet = Record<string, {
  label: string; kind: string; operator: string | null; base: [number, number]
}>

type Frame = {
  at: string
  eventId: number
  open: string[]
  assigned: Record<string, { incident: string; progress: number }>
  alerts: { id: string; wardId: string | null; severity: number | null
            audience: string | null; reach: number | null }[]
  plan: { engine: string; coverage: number; units: number
          demands: number; trigger: string } | null
  uncovered: { incidentId: string; capability: string; reason: string }[]
}

type Tick = {
  id: number; at: string; kind: string; actor: string | null
  wardId: string | null; subjectId: string | null
  causationId: number | null; text: string
}

type Replay = {
  from: string; to: string; steps: number
  incidents: Catalogue
  resources: Fleet
  frames: Frame[]
  events: Tick[]
  positionsAre: string
  positionsNote: string
}

const clock = (iso: string) =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })

/** Event kinds worth colouring in the ticker. Everything else is grey, because
 *  a ticker where every line shouts is a ticker nobody reads. */
const TONE: Record<string, string> = {
  "assignment.changed": "text-amber-500",
  "incident.opened": "text-orange-500",
  "alert.issued": "text-red-500",
  "demand.uncovered": "text-red-400",
  "incident.resolved": "text-emerald-500",
  "plan.generated": "text-sky-400",
}

/** Where a unit must have been.
 *
 *  Interpolated from its base toward the incident it was committed to, by the
 *  progress the assignment recorded. No per-unit location history exists, so
 *  this is a reconstruction — labelled as one on screen, because a dot that
 *  looks like a GPS trace and is not would be the one dishonest thing here.
 */
function positionOf(
  base: [number, number], target: [number, number], progressPct: number
): [number, number] {
  const t = Math.max(0, Math.min(1, progressPct / 100))
  return [base[0] + (target[0] - base[0]) * t, base[1] + (target[1] - base[1]) * t]
}

export default function Replay() {
  const [sessions, setSessions] = useState<Session[]>([])
  const [picked, setPicked] = useState<Session | null>(null)
  const [data, setData] = useState<Replay | null>(null)
  const [i, setI] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(4)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const feed = useRef<HTMLDivElement>(null)

  useEffect(() => {
    request<Session[]>("/replay/sessions")
      .then((s) => {
        setSessions(s)
        // The longest run, not the newest: a 30-second session is a restart,
        // not something anybody wants to watch.
        const best = [...s].sort((a, b) => b.events - a.events)[0]
        if (best) setPicked(best)
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  const load = useCallback(async (s: Session) => {
    setBusy(true); setError(null); setPlaying(false)
    try {
      const r = await request<Replay>("/replay/frames", {
        query: { from: s.startedAt, to: s.endedAt, steps: 180 },
      })
      setData(r)
      setI(0)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setBusy(false) }
  }, [])

  useEffect(() => { if (picked) void load(picked) }, [picked, load])

  // The player. One frame per interval; stops itself at the end rather than
  // looping, because a loop makes it unclear where the run actually finished.
  useEffect(() => {
    if (!playing || !data) return
    const id = setInterval(() => {
      setI((n) => {
        if (n >= data.frames.length - 1) { setPlaying(false); return n }
        return n + 1
      })
    }, 1000 / speed)
    return () => clearInterval(id)
  }, [playing, speed, data])

  const frame = data?.frames[i] ?? null

  /** Everything that happened up to this moment, newest first. The ticker is
   *  the half of this screen that carries the argument. */
  const upto = useMemo(() => {
    if (!data || !frame) return []
    return data.events.filter((e) => e.id <= frame.eventId).slice(-400).reverse()
  }, [data, frame])

  useEffect(() => { feed.current?.scrollTo({ top: 0 }) }, [i])

  const incidents = useMemo(() => {
    if (!data || !frame) return []
    return frame.open.flatMap((id) => {
      const inc = data.incidents[id]
      if (!inc) return []
      return [{
        id, title: inc.title, category: inc.category, severity: inc.severity,
        reportCount: 0, location: inc.location,
        unitsEnRoute: Object.values(frame.assigned).filter((a) => a.incident === id).length,
      }]
    })
  }, [data, frame])

  const units = useMemo(() => {
    if (!data || !frame) return []
    return Object.entries(frame.assigned).flatMap(([resId, a]) => {
      const res = data.resources[resId]
      const inc = data.incidents[a.incident]
      if (!res || !inc) return []
      return [{
        id: resId, kind: res.kind, label: res.label, status: "en_route",
        operator: res.operator ?? undefined,
        location: positionOf(res.base, inc.location, a.progress),
        assignedTo: a.incident, etaMinutes: null,
      }]
    })
  }, [data, frame])

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <p className="text-muted-foreground max-w-2xl text-sm">
            Every decision this system made, in the order it made them, with the
            reason it recorded at the time. Nothing here is re-computed — it is
            the append-only event log, folded back into the world it described.
          </p>
        </div>

        <div className="flex flex-wrap gap-1.5">
          {sessions.map((s) => (
            <Button
              key={s.id} size="sm"
              variant={picked?.id === s.id ? "secondary" : "ghost"}
              className="h-8 text-xs"
              onClick={() => setPicked(s)}
            >
              {clock(s.startedAt)}
              <span className="text-muted-foreground ml-1.5 tabular-nums">
                {s.durationMinutes}m · {s.events}
              </span>
            </Button>
          ))}
        </div>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTriangle className="size-4" />
          <AlertDescription className="text-xs">{error}</AlertDescription>
        </Alert>
      )}

      {busy && (
        <p className="text-muted-foreground flex items-center gap-2 text-xs">
          <Loader2 className="size-3.5 animate-spin" /> Folding the event log…
        </p>
      )}

      {data && frame && (
        <>
          <MapStage
            panelTitle="What happened, up to this moment"
            panel={
              <div ref={feed} className="space-y-1.5">
                {upto.map((e) => (
                  <div key={e.id} className="border-b border-slate-500/15 pb-1.5 last:border-0">
                    <div className="flex gap-2">
                      <span className="shrink-0 text-[10px] tabular-nums text-slate-500">
                        {clock(e.at)}
                      </span>
                      <span className={`text-xs ${TONE[e.kind] ?? "text-slate-300"}`}>
                        {e.text}
                      </span>
                    </div>
                  </div>
                ))}
                {upto.length === 0 && (
                  <p className="text-slate-400">Nothing had happened yet.</p>
                )}
              </div>
            }
            map={(expanded) => (
              <LiveMap
                className={expanded ? "h-full w-full" : "h-[460px] w-full rounded-lg border"}
                wards={[]}
                incidents={incidents}
                resources={units}
                center={[73.88, 18.58]}
                zoom={10.6}
              />
            )}
            footer={
              /* The scrubber. */
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <Button
                    size="icon" variant="secondary" className="size-9"
                    onClick={() => setPlaying((p) => !p)}
                    aria-label={playing ? "Pause" : "Play"}
                  >
                    {playing ? <Pause className="size-4" /> : <Play className="size-4" />}
                  </Button>
                  <Button
                    size="icon" variant="ghost" className="size-9"
                    aria-label="Step back"
                    onClick={() => { setPlaying(false); setI((n) => Math.max(0, n - 1)) }}
                  >
                    <ChevronLeft className="size-4" />
                  </Button>
                  <Button
                    size="icon" variant="ghost" className="size-9"
                    aria-label="Step forward"
                    onClick={() => {
                      setPlaying(false)
                      setI((n) => Math.min(data.frames.length - 1, n + 1))
                    }}
                  >
                    <ChevronRight className="size-4" />
                  </Button>

                  <input
                    type="range"
                    min={0}
                    max={data.frames.length - 1}
                    value={i}
                    onChange={(e) => { setPlaying(false); setI(Number(e.target.value)) }}
                    className="accent-primary h-1.5 min-w-0 flex-1 cursor-pointer"
                    aria-label="Scrub through the run"
                  />

                  <span className="shrink-0 text-xs tabular-nums">{clock(frame.at)}</span>

                  {[1, 4, 12].map((s) => (
                    <Button
                      key={s} size="sm"
                      variant={speed === s ? "secondary" : "ghost"}
                      className="h-7 px-2 text-[11px]"
                      onClick={() => setSpeed(s)}
                    >
                      {s}×
                    </Button>
                  ))}
                </div>

                <p className="text-muted-foreground text-[11px]">
                  {data.positionsNote}
                </p>
              </div>
            }
          />

          <div className="grid gap-5 md:grid-cols-3">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">At {clock(frame.at)}</CardTitle>
                <CardDescription>
                  Folded from {frame.eventId ? `event ${frame.eventId}` : "no events yet"}.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-1 text-xs">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Incidents open</span>
                  <span className="tabular-nums">{frame.open.length}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Units committed</span>
                  <span className="tabular-nums">{Object.keys(frame.assigned).length}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Alerts issued</span>
                  <span className="tabular-nums">{frame.alerts.length}</span>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">The plan in force</CardTitle>
                <CardDescription>
                  The last solve before this moment.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-1 text-xs">
                {frame.plan ? (
                  <>
                    <div className="flex items-center justify-between">
                      <span className="text-muted-foreground">Engine</span>
                      <Badge variant={frame.plan.engine === "cp-sat" ? "secondary" : "outline"}>
                        {frame.plan.engine}
                      </Badge>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Coverage</span>
                      <span className="tabular-nums">
                        {Math.round((frame.plan.coverage ?? 0) * 100)}%
                      </span>
                    </div>
                    <div className="flex justify-between">
                      <span className="text-muted-foreground">Units / demands</span>
                      <span className="tabular-nums">
                        {frame.plan.units} / {frame.plan.demands}
                      </span>
                    </div>
                    <p className="text-muted-foreground pt-1">
                      Triggered by {frame.plan.trigger}.
                    </p>
                  </>
                ) : (
                  <p className="text-muted-foreground">Nothing solved yet at this point.</p>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">What it could not cover</CardTitle>
                <CardDescription>
                  Recorded rather than hidden. A plan that never admits a gap is
                  not a plan.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-1.5 text-xs">
                {frame.uncovered.length === 0 ? (
                  <p className="text-muted-foreground">Everything open was covered.</p>
                ) : (
                  frame.uncovered.slice(0, 4).map((u, n) => (
                    <div key={n}>
                      <Badge variant="outline" className="border-red-500/30">
                        {u.capability}
                      </Badge>
                      <p className="text-muted-foreground mt-0.5">{u.reason}</p>
                    </div>
                  ))
                )}
              </CardContent>
            </Card>
          </div>
        </>
      )}

      {!busy && !data && !error && sessions.length === 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Nothing recorded yet</CardTitle>
            <CardDescription>
              Replay reads the event log. Run live ingest on the command console
              for a few minutes and a session will appear here.
            </CardDescription>
          </CardHeader>
        </Card>
      )}
    </div>
  )
}
