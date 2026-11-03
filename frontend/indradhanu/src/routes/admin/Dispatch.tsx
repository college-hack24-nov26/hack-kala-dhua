import { useEffect, useMemo, useState } from "react"
import { Ban, Inbox, Pin, Siren, Truck } from "lucide-react"
import { request } from "@/api/httpClient"
import { Button } from "@/components/ui/button"
import { CancelDialog } from "@/components/ops/CancelDialog"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { StatCard, useSeries } from "@/components/common/StatCard"

/** The allocation ledger. Who is on what, what is spare, what nobody has.
 *
 *  The system assigns — CP-SAT solves it, the gate authorises it, the replanner
 *  revisits it. This screen does not. Its whole job is to make what the system
 *  decided legible, which until now was possible only by hovering coloured
 *  lines on a map: the map answers "where", and the question is "what is this
 *  unit attached to, and why that one".
 *
 *  Three tables, because the question has three halves and an officer asks them
 *  in this order:
 *
 *    **Assigned** — every committed unit, the incident it is attached to, the
 *    hazard behind that incident, how many reports built it, how far out it is,
 *    and which actor made the attachment. One row per unit, so "where is
 *    Ambulance 4" is a scan rather than a search.
 *
 *    **Spare** — what is left. The other half of every coverage question, and
 *    the one a map cannot show, because an idle unit looks exactly like a busy
 *    one from above.
 *
 *    **Nobody has it** — demand with no unit against it. Not a failure to
 *    display: the solver records these deliberately rather than quietly
 *    under-serving them, and this is where they surface.
 *
 *  Everything is derived from the one poll the console already runs. No new
 *  request, and no number on this screen that another screen could disagree
 *  with.
 */

const pretty = (s: string) => s.replace(/_/g, " ")

const time = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  })

const since = (iso: string) => {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (!Number.isFinite(s) || s < 0) return ""
  if (s < 90) return `${Math.round(s)}s`
  if (s < 5400) return `${Math.round(s / 60)}m`
  return `${Math.round(s / 3600)}h`
}

/** How far along the drive, as a bar rather than a percentage. A number needs
 *  reading; a bar is read from the doorway. */
function Progress({ value }: { value: number }) {
  return (
    <div className="bg-muted h-1.5 w-16 overflow-hidden rounded-full">
      <div
        className="h-1.5 rounded-full bg-emerald-500"
        style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` }}
      />
    </div>
  )
}

type Override = {
  id: string; kind: "pin" | "hold" | "forbid"; resource_id: string; unit: string
  incident: string | null; reason: string; createdBy: string; expiresAt: string | null
  text: string
}
type Op = { resourceId: string; reroutes: number; why: string; setBy: string }

/** Live operations from `/ops`: the standing orders the planner is obeying,
 *  and per-unit re-route counts. Polled more slowly than the world, because
 *  these change when a person acts, not every tick. */
function useOps() {
  const [overrides, setOverrides] = useState<Override[]>([])
  const [ops, setOps] = useState<Map<string, Op>>(new Map())
  const [tick, setTick] = useState(0)
  useEffect(() => {
    let live = true
    const load = () =>
      request<{ overrides: Override[]; operations: Op[] }>("/ops", { toast: false })
        .then((r) => {
          if (!live) return
          setOverrides(r.overrides ?? [])
          setOps(new Map((r.operations ?? []).map((o) => [o.resourceId, o] as const)))
        })
        .catch(() => { /* the table below still works from the poll */ })
    void load()
    const id = setInterval(load, 4000)
    return () => { live = false; clearInterval(id) }
  }, [tick])
  return { overrides, ops, refresh: () => setTick((t) => t + 1) }
}

export default function Dispatch() {
  const { state, selected, setSelected } = useDemo()
  const { overrides, ops, refresh } = useOps()
  const [cancelFor, setCancelFor] = useState<{ id: string; label: string } | null>(null)
  const pinned = useMemo(
    () => new Set(overrides.filter((o) => o.kind === "pin").map((o) => o.resource_id)),
    [overrides]
  )

  async function lift(id: string) {
    await request(`/ops/overrides/${id}/lift`, {
      method: "POST", toast: { loading: "Lifting…", success: "Lifted. The planner may use the unit again." },
    }).catch(() => undefined)
    refresh()
  }

  const wardName = useMemo(
    () => new Map(state.wards.map((w) => [w.id, w.name] as const)),
    [state.wards]
  )
  const byIncident = useMemo(
    () => new Map(state.incidents.map((i) => [i.id, i] as const)),
    [state.incidents]
  )

  /** Who attached this unit, and when.
   *
   *  Taken from the append-only log rather than from the assignment row,
   *  because the assignment knows *that* it exists and the log knows who caused
   *  it — the solver, the replanner, an officer's approval. "Assigned by" is
   *  the column that turns a status board into an account of a decision. */
  const attachedBy = useMemo(() => {
    const m = new Map<string, { actor: string; at: string; text: string; why: string }>()
    for (const e of state.events) {
      if (!e.kind.startsWith("assignment")) continue
      const p = (e.payload ?? {}) as Record<string, unknown>
      // A fresh assignment names the unit in the payload; a re-tasking names
      // the *assignment* there and the unit as its own subject. Both shapes,
      // because the column is about the unit either way.
      const rid = typeof p.resource_id === "string" ? p.resource_id : e.subjectId
      if (typeof rid !== "string") continue
      const prev = m.get(rid)
      if (!prev || prev.at < e.occurredAt) {
        m.set(rid, {
          actor: e.actor,
          at: e.occurredAt,
          text: e.text,
          // The planner writes its whole argument here — what it gave up, how
          // far along that unit already was, and which severity outranked it.
          // It was in the database and on no screen.
          why: typeof p.reason === "string" ? p.reason : "",
        })
      }
    }
    return m
  }, [state.events])

  /** The decision behind a committed unit, when one exists.
   *
   *  Not every assignment has one — the solver's own output does not go through
   *  the gate — but a prepositioning, a mutual-aid request or anything an
   *  officer approved does, and that decision carries the clause it was
   *  authorised under and the rationale in words. Keyed by the unit the
   *  decision names in its params, which is why those had to be sent. */
  const decisionFor = useMemo(() => {
    const m = new Map<string, (typeof state.decisions)[number]>()
    for (const d of state.decisions) {
      const rid = d.params?.resource_id
      if (typeof rid !== "string") continue
      const prev = m.get(rid)
      if (!prev || prev.createdAt < d.createdAt) m.set(rid, d)
    }
    return m
  }, [state.decisions])

  const reportsPerIncident = useMemo(() => {
    const m = new Map<string, number>()
    for (const r of state.reports) {
      if (!r.incidentId) continue
      m.set(r.incidentId, (m.get(r.incidentId) ?? 0) + 1)
    }
    return m
  }, [state.reports])

  const progressOf = useMemo(
    () => new Map(state.routes.map((r) => [r.resourceId, r.progress] as const)),
    [state.routes]
  )

  const assigned = useMemo(
    () =>
      state.resources
        .filter((r) => r.incidentId)
        .map((r) => ({
          unit: r,
          incident: r.incidentId ? byIncident.get(r.incidentId) : undefined,
          by: attachedBy.get(r.id),
          // Only `UnitRoute` carries progress — a resource row does not know
          // how far along its drive it is, and the fallback to `r.progress`
          // was reaching for a field that has never existed on it. No route
          // yet means not moving yet, which is what 0 says.
          progress: progressOf.get(r.id) ?? 0,
        }))
        .sort(
          (a, b) =>
            (b.incident?.severity ?? 0) - (a.incident?.severity ?? 0) ||
            (a.unit.etaMinutes ?? 999) - (b.unit.etaMinutes ?? 999)
        ),
    [state.resources, byIncident, attachedBy, progressOf]
  )

  const spare = useMemo(
    () =>
      state.resources
        .filter((r) => !r.incidentId)
        .sort(
          (a, b) =>
            Number(b.status === "available") - Number(a.status === "available") ||
            a.kind.localeCompare(b.kind)
        ),
    [state.resources]
  )

  /** Demand with nothing against it, joined to the incident and the hazard it
   *  came from, so the row says what is missing *and* what it is missing for. */
  const unmet = useMemo(
    () =>
      state.needs
        .filter((n) => n.met < n.required)
        .map((n) => ({ ...n, incident: byIncident.get(n.incidentId) }))
        .filter((n) => n.incident && n.incident.status !== "resolved")
        .sort(
          (a, b) =>
            (b.incident?.severity ?? 0) - (a.incident?.severity ?? 0) ||
            b.required - b.met - (a.required - a.met)
        ),
    [state.needs, byIncident]
  )

  const free = spare.filter((r) => r.status === "available").length

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      {/* Four numbers, and the only two that change a decision are the last
          two. Red is reserved for the one that means somebody is not coming. */}
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Units committed" value={`${assigned.length}/${state.resources.length}`} />
        <Stat label="Spare and ready" value={free} />
        <Stat
          label="Open incidents"
          value={state.incidents.filter((i) => i.status !== "resolved").length}
        />
        <Stat label="Demands nobody has" value={unmet.length} tone={unmet.length ? "bad" : undefined} />
      </div>

      {/* ------------------------------------------------------- assigned -- */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Truck className="size-4" /> Assigned right now
          </CardTitle>
          <CardDescription>
            One row per committed unit, live. Every one of these was decided by
            the solver — the last two columns say which actor wrote it, when,
            and the argument it gave. The number beside an incident is its
            severity on a 1&ndash;5 scale, where 5 is life at risk; red is 4 or
            above.
          </CardDescription>
        </CardHeader>
        <CardContent className="px-0">
          <div className="max-h-[26rem] overflow-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Unit</TableHead>
                  <TableHead>Attached to</TableHead>
                  <TableHead>Hazard</TableHead>
                  <TableHead>Ward</TableHead>
                  <TableHead className="text-right" title="How many separate reports were clustered into this incident">
                    Reports behind it
                  </TableHead>
                  <TableHead>Doing what</TableHead>
                  <TableHead className="text-right">ETA</TableHead>
                  <TableHead>Journey done</TableHead>
                  <TableHead>Attached by</TableHead>
                  <TableHead className="min-w-[20rem]">Why this unit</TableHead>
                  <TableHead>Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {assigned.map(({ unit, incident, by, progress }) => {
                  const decision = decisionFor.get(unit.id)
                  return (
                  <TableRow
                    key={unit.id}
                    className={`cursor-pointer ${
                      selected === incident?.id ? "bg-primary/5" : ""
                    }`}
                    onClick={() => setSelected(incident?.id ?? null)}
                  >
                    <TableCell className="font-medium">
                      {unit.label}
                      <span className="text-muted-foreground ml-1.5 text-xs font-normal">
                        {pretty(unit.kind)}
                      </span>
                      {pinned.has(unit.id) && (
                        <Badge variant="outline" className="ml-1.5 gap-1 font-normal" title="An officer pinned this unit; the planner may not move it">
                          <Pin className="size-3" /> pinned
                        </Badge>
                      )}
                      {(ops.get(unit.id)?.reroutes ?? 0) > 0 && (
                        <Badge variant="secondary" className="ml-1.5 font-normal" title="Re-routed round a reported road block on this job">
                          re-routed ×{ops.get(unit.id)?.reroutes}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="max-w-[16rem]">
                      <div className="flex items-center gap-1.5">
                        {incident && (
                          <Badge
                            variant={incident.severity >= 4 ? "destructive" : "secondary"}
                            className="tabular-nums"
                          >
                            {incident.severity}
                          </Badge>
                        )}
                        <span className="truncate text-sm">
                          {incident?.title ?? unit.assignedTo ?? "—"}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell className="text-muted-foreground text-xs">
                      {incident ? pretty(incident.category) : "—"}
                    </TableCell>
                    <TableCell className="text-xs">
                      {incident ? wardName.get(incident.wardId) ?? incident.wardId : "—"}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">
                      {incident ? reportsPerIncident.get(incident.id) ?? 0 : "—"}
                    </TableCell>
                    <TableCell className="text-xs">
                      {pretty(unit.assignmentStatus ?? unit.status)}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">
                      {unit.etaMinutes != null ? `${unit.etaMinutes} min` : "—"}
                      {unit.distanceKm != null && (
                        <span className="text-muted-foreground ml-1">
                          {unit.distanceKm.toFixed(1)} km
                        </span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Progress value={progress} />
                    </TableCell>
                    <TableCell className="text-muted-foreground max-w-[13rem] text-xs">
                      {by ? (
                        <span className="block truncate" title={by.text}>
                          {by.actor.replace(/^agent:/, "").replace(/[:_]/g, " ")} ·{" "}
                          {time(by.at)}
                        </span>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    {/* The argument, in the words whoever made the decision
                        wrote. Not a summary and not a template: the planner
                        already says what it gave up and why, and the gate
                        already says which clause let it. Both were in the
                        database and on no screen. */}
                    <TableCell className="max-w-[26rem] py-3 align-top text-xs leading-relaxed">
                      {by?.why || decision?.rationale ? (
                        <>
                          <p>{by?.why || decision?.rationale}</p>
                          {decision?.clause && (
                            <p className="text-muted-foreground mt-1">
                              Authorised under {decision.clause}
                              {decision.withinDelegation === false
                                ? ` — reserved to the ${decision.delegatedTo ?? "Commissioner"}, so an officer approved it`
                                : ""}
                            </p>
                          )}
                        </>
                      ) : (
                        <span className="text-muted-foreground">
                          Nearest capable unit in the last solve. No re-tasking,
                          so nothing was given up.
                        </span>
                      )}
                    </TableCell>
                    <TableCell onClick={(e) => e.stopPropagation()}>
                      <Button size="sm" variant="outline" className="h-7 gap-1 text-xs"
                              onClick={() => setCancelFor({ id: unit.id, label: unit.label })}>
                        <Ban className="size-3" /> Cancel…
                      </Button>
                    </TableCell>
                  </TableRow>
                  )
                })}
                {assigned.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={11} className="text-muted-foreground text-xs">
                      Nothing committed. Either nothing is open, or the planner
                      has not run since it opened.
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      {/* ------------------------------------------------ standing orders -- */}
      {overrides.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Pin className="size-4" /> Standing orders the planner is obeying
            </CardTitle>
            <CardDescription>
              Set by a person when they cancelled, redirected or held a unit. Every
              re-plan reads these first. They lapse on their own; lift one early if
              the reason has gone.
            </CardDescription>
          </CardHeader>
          <CardContent className="px-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Order</TableHead>
                  <TableHead>Why</TableHead>
                  <TableHead>By</TableHead>
                  <TableHead>Lapses</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {overrides.map((o) => (
                  <TableRow key={o.id}>
                    <TableCell className="text-sm">{o.text}</TableCell>
                    <TableCell className="text-muted-foreground max-w-[24rem] text-xs">{o.reason}</TableCell>
                    <TableCell className="text-xs">{o.createdBy}</TableCell>
                    <TableCell className="text-xs tabular-nums">
                      {o.expiresAt ? time(o.expiresAt) : "—"}
                    </TableCell>
                    <TableCell>
                      <Button size="sm" variant="ghost" className="h-7 text-xs" onClick={() => void lift(o.id)}>
                        Lift
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      <CancelDialog
        open={cancelFor !== null}
        onOpenChange={(o) => { if (!o) { setCancelFor(null); refresh() } }}
        resourceId={cancelFor?.id ?? null}
        unitLabel={cancelFor?.label ?? ""}
        incidents={state.incidents}
        wards={state.wards}
      />

      <div className="grid gap-5 lg:grid-cols-2">
        {/* --------------------------------------------------------- left -- */}
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Inbox className="size-4" /> What is left
            </CardTitle>
            <CardDescription>
              Not committed to anything. A map cannot show this — an idle unit
              looks exactly like a busy one from above — and it is half of every
              question about whether the city is covered.
            </CardDescription>
          </CardHeader>
          <CardContent className="px-0">
            <div className="max-h-[22rem] overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Unit</TableHead>
                    <TableHead>Can do</TableHead>
                    <TableHead>Doing what</TableHead>
                    <TableHead>Why not available</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {spare.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell className="font-medium">
                        {r.label}
                        <span className="text-muted-foreground ml-1.5 text-xs font-normal">
                          {pretty(r.kind)}
                        </span>
                      </TableCell>
                      <TableCell className="max-w-[12rem]">
                        <div className="flex flex-wrap gap-1">
                          {r.capabilities.slice(0, 2).map((c) => (
                            <Badge key={c} variant="outline" className="font-normal">
                              {pretty(c)}
                            </Badge>
                          ))}
                          {r.capabilities.length > 2 && (
                            <span className="text-muted-foreground text-xs">
                              +{r.capabilities.length - 2}
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-xs">
                        <Badge
                          variant={r.status === "available" ? "secondary" : "outline"}
                          className="font-normal"
                        >
                          {pretty(r.status)}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-muted-foreground max-w-[12rem] truncate text-xs">
                        {r.status === "available"
                          ? "—"
                          : r.unavailableReason ?? r.statusNote ?? pretty(r.status)}
                      </TableCell>
                    </TableRow>
                  ))}
                  {spare.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={4} className="text-muted-foreground text-xs">
                        Every unit in the fleet is committed.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>

        {/* -------------------------------------------------------- unmet -- */}
        <Card className={unmet.length ? "border-destructive/40" : undefined}>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Siren className="size-4" /> Demand nobody has
            </CardTitle>
            <CardDescription>
              Recorded rather than quietly under-served. Each row is a capability
              an open incident needs and no unit is meeting — the case for asking
              another agency, on the handoff screen.
            </CardDescription>
          </CardHeader>
          <CardContent className="px-0">
            <div className="max-h-[22rem] overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Needs</TableHead>
                    <TableHead>For</TableHead>
                    <TableHead>Ward</TableHead>
                    <TableHead className="text-right">Short by</TableHead>
                    <TableHead className="text-right">Waiting</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {unmet.map((n) => (
                    <TableRow
                      key={`${n.incidentId}-${n.capability}`}
                      className={`cursor-pointer ${
                        selected === n.incidentId ? "bg-primary/5" : ""
                      }`}
                      onClick={() => setSelected(n.incidentId)}
                    >
                      <TableCell className="font-medium">
                        {pretty(n.capability)}
                      </TableCell>
                      <TableCell className="max-w-[12rem]">
                        <div className="flex items-center gap-1.5">
                          <Badge
                            variant={
                              (n.incident?.severity ?? 0) >= 4 ? "destructive" : "secondary"
                            }
                            className="tabular-nums"
                          >
                            {n.incident?.severity}
                          </Badge>
                          <span className="truncate text-sm">{n.incident?.title}</span>
                        </div>
                      </TableCell>
                      <TableCell className="text-xs">
                        {n.incident
                          ? wardName.get(n.incident.wardId) ?? n.incident.wardId
                          : "—"}
                      </TableCell>
                      <TableCell className="text-right text-xs tabular-nums">
                        {n.required - n.met} of {n.required}
                      </TableCell>
                      <TableCell className="text-muted-foreground text-right text-xs tabular-nums">
                        {n.incident ? since(n.incident.createdAt) : ""}
                      </TableCell>
                    </TableRow>
                  ))}
                  {unmet.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={5} className="text-muted-foreground text-xs">
                        Everything every open incident needs has a unit against
                        it.
                      </TableCell>
                    </TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function Stat({
  label, value, tone,
}: {
  label: string
  value: string | number
  tone?: "bad"
}) {
  const series = useSeries(`dispatch:${label}`, parseFloat(String(value)))
  return <StatCard label={label} value={value} tone={tone} series={series} />
}
