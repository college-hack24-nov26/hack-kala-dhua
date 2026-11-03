import { useMemo, useState } from "react"
import { useSearchParams } from "react-router-dom"
import {
  AlertTriangle, Bluetooth, Camera, CheckCircle2, CircleDashed, Clock, HardHat,
  Handshake, Megaphone, Search, Siren, Smartphone, Truck,
} from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import type { Incident, RawReport } from "@/routes/demo/useDemo"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { hhmmss } from "./meshApi"
import EdgeEvidence from "./EdgeEvidence"

/** Every incident, the reports that opened it, and what is being done about it.
 *
 *  Left: the incidents, open ones first, worst first. Right, for the one
 *  selected, three questions in the order an officer asks them:
 *
 *    1. **Who told us?** The report that opened it, then every report merged
 *       into it, each with where it came from (app, mesh, camera, crew).
 *    2. **What are we doing?** Units assigned and their ETA, what is still
 *       unmet, approvals waiting, requests to other agencies, alerts issued.
 *    3. **What happened, in order?** The event log for this incident.
 */

function SourceIcon({ source, className }: { source: string; className?: string }) {
  const s = source.toLowerCase()
  if (s.startsWith("mesh")) return <Bluetooth className={className} aria-label={source} />
  if (s.includes("sensor") || s.includes("camera") || s.includes("vlm") || s.includes("vision"))
    return <Camera className={className} aria-label={source} />
  if (s.includes("field") || s.includes("crew")) return <HardHat className={className} aria-label={source} />
  return <Smartphone className={className} aria-label={source} />
}
const sourceName = (source: string) => {
  const s = source.toLowerCase()
  if (s === "mesh") return "Mesh (signed)"
  if (s === "mesh_unsigned") return "Mesh (unsigned)"
  if (s.includes("sensor")) return "Camera / VLM"
  if (s.includes("field")) return "Field crew"
  return s.charAt(0).toUpperCase() + s.slice(1)
}
const OPEN = (s: string) => !/resolved|closed|cancel/i.test(s)
const sevTone = (n: number) =>
  n >= 4 ? "bg-red-500 text-white" : n === 3 ? "bg-amber-500 text-black" : "bg-muted text-foreground"

export default function IncidentResponse() {
  const { state } = useDemo()
  const [params, setParams] = useSearchParams()
  const [q, setQ] = useState("")
  const [onlyOpen, setOnlyOpen] = useState(true)

  const reportsBy = useMemo(() => {
    const m = new Map<string, RawReport[]>()
    for (const r of state.reports) {
      if (!r.incidentId) continue
      const list = m.get(r.incidentId) ?? []
      list.push(r)
      m.set(r.incidentId, list)
    }
    for (const list of m.values()) list.sort((a, b) => Date.parse(a.createdAt) - Date.parse(b.createdAt))
    return m
  }, [state.reports])

  const wardName = useMemo(
    () => new Map(state.wards.map((w) => [w.id, w.name])),
    [state.wards],
  )

  const list = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return state.incidents
      .filter((i) => !onlyOpen || OPEN(i.status))
      .filter(
        (i) =>
          !needle ||
          i.title.toLowerCase().includes(needle) ||
          i.category.toLowerCase().includes(needle) ||
          (wardName.get(i.wardId) ?? "").toLowerCase().includes(needle),
      )
      .sort(
        (a, b) =>
          Number(OPEN(b.status)) - Number(OPEN(a.status)) ||
          b.severity - a.severity ||
          Date.parse(b.createdAt) - Date.parse(a.createdAt),
      )
  }, [state.incidents, q, onlyOpen, wardName])

  const selectedId = params.get("incident") ?? list[0]?.id ?? null
  const selected = state.incidents.find((i) => i.id === selectedId) ?? list[0] ?? null
  const select = (id: string) => setParams({ incident: id }, { replace: true })

  return (
    <div className="grid h-full min-h-0 gap-6 p-5 md:p-8 xl:px-10 lg:grid-cols-[minmax(280px,380px)_1fr]">
      <div className="flex min-h-0 flex-col gap-2">
        <div className="flex items-center gap-2">
          <div className="relative flex-1">
            <Search className="text-muted-foreground absolute top-2.5 left-2.5 size-4" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search incidents or wards"
              className="pl-8"
            />
          </div>
          <Button size="sm" variant={onlyOpen ? "default" : "outline"} onClick={() => setOnlyOpen((v) => !v)}>
            {onlyOpen ? "Open" : "All"}
          </Button>
        </div>
        <p className="text-muted-foreground text-xs">
          {list.length} incident{list.length === 1 ? "" : "s"} · {list.filter((i) => i.unitsEnRoute === 0 && OPEN(i.status)).length} with nobody on the way
        </p>
        <ul className="min-h-0 flex-1 space-y-1.5 overflow-y-auto pr-1">
          {list.map((i) => {
            const reps = reportsBy.get(i.id) ?? []
            const sources = [...new Set(reps.map((r) => r.source))]
            return (
              <li key={i.id}>
                <button
                  onClick={() => select(i.id)}
                  className={`w-full rounded-md border p-2.5 text-left text-sm transition-colors ${
                    selected?.id === i.id ? "border-primary bg-primary/5" : "bg-card hover:bg-muted/50"
                  }`}
                >
                  <div className="flex items-start gap-2">
                    <span className={`rounded px-1.5 text-xs font-semibold tabular-nums ${sevTone(i.severity)}`}>
                      S{i.severity}
                    </span>
                    <span className="flex-1 font-medium leading-tight">{i.title}</span>
                  </div>
                  <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-x-2 text-xs">
                    <span>{wardName.get(i.wardId) ?? i.wardId}</span>
                    <span>· {i.reportCount} report{i.reportCount === 1 ? "" : "s"}</span>
                    <span className={i.unitsEnRoute ? "" : "text-amber-600 dark:text-amber-400"}>
                      · {i.unitsEnRoute ? `${i.unitsEnRoute} unit${i.unitsEnRoute === 1 ? "" : "s"} en route` : "nobody assigned"}
                    </span>
                    {sources.map((s) => (
                      <SourceIcon key={s} source={s} className="size-3.5" />
                    ))}
                  </div>
                </button>
              </li>
            )
          })}
          {!list.length && (
            <li className="text-muted-foreground rounded-md border p-6 text-center text-sm">
              No {onlyOpen ? "open " : ""}incidents.
            </li>
          )}
        </ul>
      </div>

      {selected ? (
        <Detail incident={selected} reports={reportsBy.get(selected.id) ?? []} wardName={wardName.get(selected.wardId) ?? selected.wardId} />
      ) : (
        <Card className="text-muted-foreground flex items-center justify-center p-10 text-sm">
          <Siren className="mr-2 size-5" /> Select an incident.
        </Card>
      )}
    </div>
  )
}

function Detail({ incident: i, reports, wardName }: { incident: Incident; reports: RawReport[]; wardName: string }) {
  const { state } = useDemo()
  const opener = reports.find((r) => r.opened) ?? reports[0] ?? null
  const merged = reports.filter((r) => r !== opener)

  const units = state.resources.filter((r) => r.incidentId === i.id)
  const routes = state.routes.filter((r) => r.incidentId === i.id)
  const needs = state.needs.filter((n) => n.incidentId === i.id)
  const decisions = state.decisions.filter(
    (d) => (d.params as Record<string, unknown> | undefined)?.incident_id === i.id || d.target === i.id,
  )
  const requests = state.agencyRequests.filter((r) => r.incidentId === i.id)
  const alerts = state.alerts.filter((a) => a.wardId === i.wardId)
  const planLines = [...(state.plan?.assigned ?? []), ...(state.plan?.reassigned ?? [])].filter(
    (c) => c.incident_id === i.id,
  )
  const uncovered = (state.plan?.uncovered ?? []).filter((u) => u.incident_id === i.id)
  const events = state.events
    .filter(
      (e) =>
        e.subjectId === i.id ||
        (e.payload as Record<string, unknown>)?.incident_id === i.id ||
        (e.payload as Record<string, unknown>)?.incidentId === i.id,
    )
    .sort((a, b) => Date.parse(b.occurredAt) - Date.parse(a.occurredAt))
    .slice(0, 12)

  const unmet = needs.filter((n) => n.met < n.required)
  const doing = units.length + decisions.length + requests.length

  return (
    <div className="min-h-0 space-y-4 overflow-y-auto pr-1">
      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <CardTitle className="text-lg">{i.title}</CardTitle>
              <CardDescription>
                {i.category.replace(/_/g, " ")} · {wardName}
                {i.street ? ` · ${i.street}` : ""} · opened {hhmmss(i.createdAt)}
              </CardDescription>
            </div>
            <div className="flex items-center gap-1.5">
              <span className={`rounded px-2 py-0.5 text-xs font-semibold ${sevTone(i.severity)}`}>severity {i.severity}</span>
              <Badge variant="outline">{i.status}</Badge>
              <Badge variant="outline">{Math.round(i.confidence * 100)}% confident</Badge>
            </div>
          </div>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm sm:grid-cols-3">
          <Stat label="Reports behind it" value={String(reports.length || i.reportCount)} />
          <Stat label="Units on it" value={String(units.length)} warn={units.length === 0 && OPEN(i.status)} />
          <Stat label="Unmet needs" value={String(unmet.length + uncovered.length)} warn={unmet.length + uncovered.length > 0} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">1 · Who told us</CardTitle>
          <CardDescription>The report that opened this incident, then every one merged into it.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {opener ? (
            <ReportRow r={opener} label="Opened it" />
          ) : (
            <p className="text-muted-foreground text-sm">
              Opened by the system (forecast, sensor rule or officer), not by a report on screen.
            </p>
          )}
          {merged.map((r) => (
            <ReportRow key={r.id} r={r} label="Merged" />
          ))}
        </CardContent>
      </Card>

      <EdgeEvidence incidentId={i.id} />

      <Card className={doing === 0 && OPEN(i.status) ? "border-amber-500/60" : undefined}>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">2 · What we are doing</CardTitle>
          <CardDescription>
            {doing === 0 && OPEN(i.status)
              ? "Nothing yet. The planner re-runs on every new report; if this stays empty, check Approvals."
              : "Assigned by the planner, authorised at the gate."}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          {units.map((u) => {
            const route = routes.find((r) => r.resourceId === u.id)
            const why = planLines.find((c) => c.resource_id === u.id)
            return (
              <div key={u.id} className="flex items-start gap-2">
                <Truck className="mt-0.5 size-4 shrink-0" />
                <div className="flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{u.label}</span>
                    <span className="text-muted-foreground text-xs">{u.kind} · {u.operator}</span>
                    <Badge variant="outline" className="text-xs">{u.assignmentStatus ?? u.status}</Badge>
                    {(route?.etaMinutes ?? u.etaMinutes) != null && (
                      <span className="inline-flex items-center gap-1 text-xs">
                        <Clock className="size-3" /> ETA {Math.round((route?.etaMinutes ?? u.etaMinutes)!)} min
                      </span>
                    )}
                    {route && route.progress > 0 && (
                      <span className="text-muted-foreground text-xs">{Math.round(route.progress * 100)}% of the way</span>
                    )}
                  </div>
                  {why?.reason && <p className="text-muted-foreground text-xs">{why.reason}</p>}
                </div>
              </div>
            )
          })}
          {needs.map((n) => (
            <div key={n.capability} className="flex items-center gap-2 text-xs">
              {n.met >= n.required ? (
                <CheckCircle2 className="size-3.5 text-emerald-600" />
              ) : (
                <CircleDashed className="size-3.5 text-amber-600" />
              )}
              <span>
                needs {n.required} × {n.capability.replace(/_/g, " ")} — {n.met} covered
              </span>
            </div>
          ))}
          {uncovered.map((u, k) => (
            <div key={k} className="flex items-center gap-2 text-xs text-amber-700 dark:text-amber-400">
              <AlertTriangle className="size-3.5" /> {u.capability ? `${u.capability}: ` : ""}{u.reason}
            </div>
          ))}
          {decisions.map((d) => (
            <div key={d.id} className="flex items-start gap-2 text-xs">
              <Siren className="mt-0.5 size-3.5 shrink-0" />
              <span>
                <b>{d.action}</b> — {d.status.replace(/_/g, " ")}
                {d.status === "awaiting_approval" && " (waiting in Approvals)"}
                <span className="text-muted-foreground"> · {d.rationale}</span>
              </span>
            </div>
          ))}
          {requests.map((r) => (
            <div key={r.id} className="flex items-center gap-2 text-xs">
              <Handshake className="size-3.5" />
              asked {r.toName ?? r.toAgency} for {r.quantity} × {r.capability} — {r.status}
            </div>
          ))}
          {alerts.map((a) => (
            <div key={a.id} className="flex items-center gap-2 text-xs">
              <Megaphone className="size-3.5" /> alert to {a.wardName ?? a.wardId}: {a.headline}
              <span className="text-muted-foreground">· {hhmmss(a.issuedAt)}</span>
            </div>
          ))}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">3 · What happened, in order</CardTitle>
        </CardHeader>
        <CardContent>
          {events.length ? (
            <ol className="space-y-1.5 text-xs">
              {events.map((e) => (
                <li key={e.id} className="flex gap-2">
                  <span className="text-muted-foreground w-16 shrink-0 tabular-nums">{hhmmss(e.occurredAt)}</span>
                  <span className="text-muted-foreground w-40 shrink-0 truncate">{e.kind}</span>
                  <span>{e.text}</span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-muted-foreground text-xs">No events recorded against this incident yet.</p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className={`rounded-md border p-2 ${warn ? "border-amber-500/60 bg-amber-500/5" : ""}`}>
      <div className="text-muted-foreground text-xs">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value}</div>
    </div>
  )
}

function ReportRow({ r, label }: { r: RawReport; label: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md border p-2 text-sm">
      <SourceIcon source={r.source} className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge variant={label === "Opened it" ? "default" : "outline"} className="h-5">{label}</Badge>
          <span className="font-medium">{sourceName(r.source)}</span>
          <span className="text-muted-foreground tabular-nums">{hhmmss(r.createdAt)}</span>
          {r.reporter && <span className="text-muted-foreground">{r.reporter}</span>}
          {r.trust != null && <span className="text-muted-foreground">trust {Math.round(r.trust * 100)}%</span>}
          {r.linkScore != null && label !== "Opened it" && (
            <span className="text-muted-foreground">match {Math.round(r.linkScore * 100)}%</span>
          )}
        </div>
        <p className="mt-1 text-xs">{r.text}</p>
        {r.severityRead && (
          <p className="mt-0.5 text-xs">
            <span className={`mr-1 rounded px-1 font-semibold ${sevTone(r.severityRead.severity)}`}>
              read as S{r.severityRead.severity}
            </span>
            <span className="text-muted-foreground">
              by {r.severityRead.method}: {r.severityRead.reason}
              {r.severityRead.life_threat ? " · life threat" : ""}
              {r.severityRead.injection ? " · instruction-like text, not sent to a model" : ""}
              {r.severityRead.redacted?.length ? ` · redacted ${r.severityRead.redacted.join(", ")}` : ""}
            </span>
          </p>
        )}
        {r.linkReason && label !== "Opened it" && (
          <p className="text-muted-foreground mt-0.5 text-xs">why merged: {r.linkReason}</p>
        )}
      </div>
    </div>
  )
}
