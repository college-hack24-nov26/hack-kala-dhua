import { useMemo, useState } from "react"
import { Link } from "react-router-dom"
import { AlertTriangle, ArrowLeft, ChevronDown, GitBranch, Maximize2, ShieldCheck, Workflow } from "lucide-react"
import { Segmented } from "@/components/common/MacControls"
import { cn } from "@/lib/utils"
import { ScreenMap, unitIcon } from "./ZoneMap"
import { ZoneHeader, ZoneMissing, useZone } from "./ZonePage"
import { scopeState } from "./zones"
import { GROUP, GROUP_OF, pretty, time, zoneEvents, type Group, type KindGroup } from "./zoneLog"

/** How the agents are working one zone: the roads each unit was given and
 *  why, every decision the agents took here with the reason they wrote at the
 *  time, the approvals waiting on a person, and what could not be covered. */


export default function ZoneAgents() {
  const { id, state, region, zones, index, zone } = useZone()
  const [group, setGroup] = useState<Group>("all")
  const [openRoute, setOpenRoute] = useState<string | null>(null)
  const scoped = useMemo(() => scopeState(state, zone), [state, zone])

  const events = useMemo(() => (zone ? zoneEvents(state, zone) : []), [state, zone])

  if (!zone) return <ZoneMissing id={id} />

  const ids = new Set(zone.incidents.map((i) => i.id))
  const title = new Map(zone.incidents.map((i) => [i.id, i.title]))
  const counts = Object.fromEntries(
    (Object.keys(GROUP) as KindGroup[]).map((g) => [g, events.filter((e) => GROUP_OF(e.kind) === g).length]),
  ) as Record<KindGroup, number>
  const shown = group === "all" ? events : events.filter((e) => GROUP_OF(e.kind) === group)
  const uncovered = (state.plan?.uncovered ?? []).filter((u) => u.ward_id === zone.id || (u.incident_id && ids.has(u.incident_id)))
  const pending = scoped.decisions.filter((d) => /awaiting|proposed|pending/.test(d.status))
  const otherDecisions = scoped.decisions.filter((d) => !/awaiting|proposed|pending/.test(d.status))

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <Link to={`/admin/wall/zone/${encodeURIComponent(zone.id)}`}
            className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-3.5" /> {zone.name}
      </Link>

      <ZoneHeader zone={zone} index={index} zones={zones}>
        <Link to="/admin/graph" className="inline-flex h-9 items-center gap-1.5 rounded-lg border bg-card px-3 text-sm font-medium shadow-xs hover:bg-muted">
          <GitBranch className="size-4" /> Agent graph
        </Link>
        <Link to={`/admin/wall/zone/${encodeURIComponent(zone.id)}/live`}
              className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-primary px-3 text-sm font-medium text-primary-foreground shadow-xs hover:bg-primary/90">
          <Maximize2 className="size-4" /> Full screen
        </Link>
      </ZoneHeader>

      <div>
        <h3 className="flex items-center gap-2 text-base font-semibold tracking-tight">
          <Workflow className="size-4 text-primary" /> Agent routing & decisions
        </h3>
        <p className="mt-1 text-sm text-muted-foreground">
          The roads each unit was given, and every decision the agents took for this zone with the reason they wrote at the time.
        </p>
      </div>

      {/* routing */}
      <div className="grid gap-5 xl:grid-cols-[1.25fr_1fr]">
        <div className="relative overflow-hidden rounded-2xl border shadow-card">
          <ScreenMap state={state} zone={zone} region={region} big className="h-[520px] w-full" />
          <Link to={`/admin/wall/zone/${encodeURIComponent(zone.id)}/live`}
                className="absolute top-3 left-3 inline-flex h-9 items-center gap-1.5 rounded-lg border bg-card/95 px-3 text-sm font-medium shadow-sm backdrop-blur hover:bg-card">
            <Maximize2 className="size-4" /> Full screen
          </Link>
        </div>
        <section className="flex min-h-0 flex-col rounded-2xl border bg-card shadow-card">
          <div className="flex items-center justify-between border-b px-5 py-4">
            <h4 className="text-sm font-semibold">Routes in</h4>
            <span className="text-xs text-muted-foreground">{scoped.routes.length} unit{scoped.routes.length === 1 ? "" : "s"}</span>
          </div>
          {scoped.routes.length ? (
            <ul className="max-h-[520px] divide-y overflow-y-auto">
              {scoped.routes.map((r) => {
                const Icon = unitIcon(r.resourceKind)
                const open = openRoute === r.id
                const pct = Math.round(Math.max(0, Math.min(1, r.progress || 0)) * 100)
                return (
                  <li key={r.id} className="px-5 py-3.5">
                    <button onClick={() => setOpenRoute(open ? null : r.id)} className="flex w-full items-start gap-3 text-left">
                      <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-accent text-accent-foreground">
                        <Icon className="size-4" />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2">
                          <span className="truncate text-sm font-medium">{r.resourceLabel}</span>
                          <span className="rounded-md bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                            {r.status.replace(/_/g, " ")}
                          </span>
                        </span>
                        <span className="block truncate text-xs text-muted-foreground">→ {r.incidentTitle}</span>
                        <span className="mt-2 flex items-center gap-2">
                          <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                            <span className="block h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
                          </span>
                          <span className="text-[11px] tabular-nums text-muted-foreground">{pct}%</span>
                        </span>
                      </span>
                      <span className="shrink-0 text-right">
                        <span className="block text-sm font-semibold tabular-nums">
                          {r.etaMinutes != null ? `${Math.round(r.etaMinutes)} min` : "—"}
                        </span>
                        <span className="block text-[11px] tabular-nums text-muted-foreground">
                          {r.distanceKm != null ? `${r.distanceKm.toFixed(1)} km` : ""}{r.engine ? ` · ${r.engine}` : ""}
                        </span>
                      </span>
                      <ChevronDown className={cn("mt-2 size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-180")} />
                    </button>
                    {open && (
                      <ol className="mt-3 ml-12 space-y-1.5 border-l pl-4">
                        {r.steps.length ? r.steps.slice(0, 12).map((s, k) => (
                          <li key={k} className="text-xs">
                            <span className="font-medium">{s.instruction}</span>
                            <span className="text-muted-foreground">{s.street ? ` · ${s.street}` : ""} · {Math.round(s.distanceM)} m</span>
                          </li>
                        )) : <li className="text-xs text-muted-foreground">No turn-by-turn steps for this route.</li>}
                      </ol>
                    )}
                  </li>
                )
              })}
            </ul>
          ) : (
            <p className="p-5 text-sm text-muted-foreground">No unit is routed into this zone yet.</p>
          )}
        </section>
      </div>

      {/* decisions */}
      <div className="grid gap-5 xl:grid-cols-[1.4fr_1fr]">
        <section className="rounded-2xl border bg-card shadow-card">
          <div className="flex flex-wrap items-center gap-3 border-b px-5 py-4">
            <h4 className="text-sm font-semibold">Decision timeline</h4>
            <Segmented
              size="sm"
              className="ml-auto max-w-full overflow-x-auto"
              value={group}
              onChange={setGroup}
              options={[
                { value: "all", label: "All", count: events.length },
                ...(Object.keys(GROUP) as KindGroup[])
                  .filter((g) => counts[g] > 0)
                  .map((g) => ({ value: g, label: GROUP[g].label, count: counts[g] })),
              ]}
            />
          </div>
          {shown.length ? (
            <ol className="max-h-[640px] divide-y overflow-y-auto">
              {shown.slice(0, 150).map((e) => {
                const g = GROUP[GROUP_OF(e.kind)]
                const reason = typeof e.payload?.reason === "string" ? (e.payload.reason as string) : ""
                const incident = [e.subjectId, e.payload?.incident_id, e.payload?.to_incident]
                  .map((v) => (typeof v === "string" ? title.get(v) : undefined)).find(Boolean)
                return (
                  <li key={e.id} className="flex gap-3 px-5 py-3.5">
                    <span className={cn("grid size-8 shrink-0 place-items-center rounded-lg", g.tone)}>
                      <g.icon className="size-4" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
                        <span className="font-medium text-foreground">{pretty(e.kind)}</span>
                        <span>· {e.actor}</span>
                        {incident && <span className="truncate">· {incident}</span>}
                        <span className="ml-auto tabular-nums">{time(e.occurredAt)}</span>
                      </div>
                      {e.text && <p className="mt-0.5 text-sm">{e.text}</p>}
                      {reason && reason !== e.text && (
                        <p className="mt-1.5 rounded-lg bg-muted/70 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
                          <b className="font-medium text-foreground">Why: </b>{reason}
                        </p>
                      )}
                    </div>
                  </li>
                )
              })}
            </ol>
          ) : (
            <p className="p-5 text-sm text-muted-foreground">Nothing logged for this zone yet.</p>
          )}
        </section>

        <div className="space-y-5">
          <section className="rounded-2xl border bg-card p-5 shadow-card">
            <h4 className="mb-3 flex items-center gap-2 text-sm font-semibold">
              <ShieldCheck className="size-4 text-amber-600" /> Waiting on a person
              <span className="ml-auto text-xs font-normal text-muted-foreground">{pending.length}</span>
            </h4>
            {pending.length ? (
              <ul className="space-y-3">
                {pending.map((d) => (
                  <li key={d.id} className="rounded-xl border p-3">
                    <div className="flex items-center gap-2 text-sm font-medium">
                      {pretty(d.action)}
                      <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">{Math.round(d.confidence * 100)}% sure</span>
                    </div>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{d.rationale}</p>
                    {d.clause && <p className="mt-1 text-[11px] text-muted-foreground">Clause: {d.clause}</p>}
                    <Link to="/admin/decisions" className="mt-2 inline-block text-xs font-medium text-primary hover:underline">Open approvals →</Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">No decision for this zone is waiting.</p>
            )}
          </section>

          <section className="rounded-2xl border bg-card p-5 shadow-card">
            <h4 className="mb-3 flex items-center gap-2 text-sm font-semibold">
              <AlertTriangle className="size-4 text-red-600" /> Not covered, and why
              <span className="ml-auto text-xs font-normal text-muted-foreground">{uncovered.length}</span>
            </h4>
            {uncovered.length ? (
              <ul className="space-y-2.5">
                {uncovered.map((u, k) => (
                  <li key={k} className="text-xs leading-relaxed">
                    <b className="font-medium">{u.capability ? pretty(u.capability) : "Demand"}</b>
                    {u.incident_id && title.get(u.incident_id) ? ` for ${title.get(u.incident_id)}` : ""}
                    <span className="block text-muted-foreground">{u.reason}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">The last plan covered every demand here.</p>
            )}
            {scoped.needs.length > 0 && (
              <div className="mt-4 space-y-2 border-t pt-4">
                {scoped.needs.map((n, k) => (
                  <div key={k} className="flex items-center gap-2 text-xs">
                    <span className="min-w-0 flex-1 truncate">{pretty(n.capability)} · <span className="text-muted-foreground">{title.get(n.incidentId) ?? n.incidentId}</span></span>
                    <span className={cn("tabular-nums font-medium", n.met < n.required ? "text-red-600 dark:text-red-400" : "text-emerald-600")}>
                      {n.met}/{n.required}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </section>

          {otherDecisions.length > 0 && (
            <section className="rounded-2xl border bg-card p-5 shadow-card">
              <h4 className="mb-3 text-sm font-semibold">Decisions taken</h4>
              <ul className="space-y-2.5">
                {otherDecisions.slice(0, 8).map((d) => (
                  <li key={d.id} className="text-xs leading-relaxed">
                    <span className="font-medium">{pretty(d.action)}</span>
                    <span className="ml-1.5 rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">{pretty(d.status)}</span>
                    <span className="block text-muted-foreground">{d.rationale}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </div>
    </div>
  )
}
