import { useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  AlertTriangle, HeartPulse, Minimize2, Radio, ShieldCheck, Siren, Truck, Users, X,
} from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Pill, RiskRing, Segmented } from "@/components/common/MacControls"
import { cn } from "@/lib/utils"
import { HAZARD_ICON, ScreenMap, people, unitIcon } from "./ZoneMap"
import { ZoneMissing, ZoneNav, useZone } from "./ZonePage"
import { ALLOCATION, HAZARD_LABEL, scopeState } from "./zones"
import { GROUP, GROUP_OF, pretty, reasonOf, time, zoneBeats, zoneEvents, type KindGroup } from "./zoneLog"

/** One zone, full screen: the live map takes the display and a side panel
 *  says what the agents are doing there right now (narration, routes, what
 *  waits on a person, what is uncovered) and the full log for the area. */

type Tab = "now" | "log" | "routes"

function Chip({ icon: Icon, value, label, warn }: { icon: typeof Siren; value: string | number; label: string; warn?: boolean }) {
  return (
    <div className="flex items-center gap-2 rounded-xl border bg-card/95 px-3 py-2 shadow-sm backdrop-blur" title={label}>
      <Icon className={cn("size-4", warn ? "text-red-500" : "text-muted-foreground")} />
      <div className="leading-tight">
        <div className={cn("text-sm font-semibold tabular-nums", warn && "text-red-600 dark:text-red-400")}>{value}</div>
        <div className="text-[10px] text-muted-foreground">{label}</div>
      </div>
    </div>
  )
}

export default function ZoneLive() {
  const navigate = useNavigate()
  const { id, state, region, zones, index, zone } = useZone()
  const { state: live } = useDemo()
  const [tab, setTab] = useState<Tab>("now")
  const [groups, setGroups] = useState<KindGroup[]>([])
  const back = () => navigate(zone ? `/admin/wall/zone/${encodeURIComponent(zone.id)}` : "/admin/wall")

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && back()
    window.addEventListener("keydown", onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = "hidden"
    return () => {
      window.removeEventListener("keydown", onKey)
      document.body.style.overflow = prev
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zone?.id])

  const scoped = useMemo(() => scopeState(state, zone), [state, zone])
  const events = useMemo(() => (zone ? zoneEvents(state, zone) : []), [state, zone])
  const beats = useMemo(() => (zone ? zoneBeats(state, zone) : []), [state, zone])

  if (!zone) {
    return (
      <div className="fixed inset-0 z-50 overflow-auto bg-background">
        <ZoneMissing id={id} />
      </div>
    )
  }

  const a = ALLOCATION[zone.allocation]
  const ids = new Set(zone.incidents.map((i) => i.id))
  const title = new Map(zone.incidents.map((i) => [i.id, i.title]))
  const pending = scoped.decisions.filter((d) => /awaiting|proposed|pending/.test(d.status))
  const uncovered = (state.plan?.uncovered ?? []).filter((u) => u.ward_id === zone.id || (u.incident_id && ids.has(u.incident_id)))
  const lastPlan = events.find((e) => e.kind.startsWith("plan."))
  const counts = Object.fromEntries(
    (Object.keys(GROUP) as KindGroup[]).map((g) => [g, events.filter((e) => GROUP_OF(e.kind) === g).length]),
  ) as Record<KindGroup, number>
  const log = groups.length ? events.filter((e) => groups.includes(GROUP_OF(e.kind))) : events

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-background lg:flex-row">
      {/* the map */}
      <div className="relative min-h-[45vh] flex-1">
        <div className="absolute inset-0">
          <ScreenMap state={state} zone={zone} region={region} big className="h-full w-full" />
        </div>

        <div className="pointer-events-none absolute inset-x-0 top-0 flex flex-wrap items-start gap-3 p-4">
          <div className="pointer-events-auto flex items-center gap-3 rounded-2xl border bg-card/95 py-2 pr-2 pl-2.5 shadow-md backdrop-blur">
            <RiskRing value={zone.risk} size={44} stroke={4} />
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="truncate text-[15px] font-semibold tracking-tight">{zone.name}</span>
                <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ring-1", a.tone)}>
                  <span className={cn("size-1.5 rounded-full", a.dot)} /> {a.label}
                </span>
              </div>
              <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
                #{index + 1} by risk · S{zone.severity}
                {zone.hazards.map((h) => {
                  const Icon = HAZARD_ICON[h]
                  return <Icon key={h} className="size-3.5 text-primary" aria-label={HAZARD_LABEL[h]} />
                })}
              </div>
            </div>
            <ZoneNav zones={zones} index={index} base={(z) => `/admin/wall/zone/${encodeURIComponent(z.id)}/live`} />
            <button onClick={back} title="Exit full screen (Esc)" aria-label="Exit full screen"
                    className="grid size-9 place-items-center rounded-lg hover:bg-muted">
              <Minimize2 className="size-4" />
            </button>
          </div>
        </div>

        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex flex-wrap gap-2 px-4 pt-4 pb-10">
          <div className="pointer-events-auto flex flex-wrap gap-2">
            <Chip icon={Siren} value={zone.incidents.length} label="incidents" />
            <Chip icon={HeartPulse} value={zone.lifeSafety} label="life-safety" warn={zone.lifeSafety > 0} />
            <Chip icon={Users} value={people(zone.exposed)} label="exposed" />
            <Chip icon={Truck} value={`${zone.unitsEnRoute} · ${zone.onScene}`} label="en route · on scene" />
            <Chip icon={AlertTriangle} value={zone.unattended} label="unassigned" warn={zone.unattended > 0} />
          </div>
        </div>
      </div>

      {/* the agents */}
      <aside className="flex h-[55vh] w-full shrink-0 flex-col border-t bg-card lg:h-full lg:w-[440px] lg:border-t-0 lg:border-l">
        <div className="flex items-center gap-2 border-b px-5 py-4">
          <span className="relative flex size-2">
            {live.running && <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-60" />}
            <span className={cn("relative inline-flex size-2 rounded-full", live.running ? "bg-emerald-500" : "bg-zinc-400")} />
          </span>
          <h3 className="text-sm font-semibold">Agents in {zone.name}</h3>
          <span className="ml-auto text-xs tabular-nums text-muted-foreground">tick {live.tick}</span>
          <button onClick={back} className="grid size-8 place-items-center rounded-lg hover:bg-muted lg:hidden" aria-label="Close">
            <X className="size-4" />
          </button>
        </div>
        <div className="border-b px-5 py-3">
          <Segmented
            size="sm"
            className="w-full [&>button]:flex-1 [&>button]:justify-center"
            value={tab}
            onChange={setTab}
            options={[
              { value: "now", label: "Now" },
              { value: "log", label: "Log", count: events.length },
              { value: "routes", label: "Routes", count: scoped.routes.length },
            ]}
          />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {tab === "now" && (
            <div className="space-y-6 p-5">
              {lastPlan && (
                <section>
                  <h4 className="mb-2 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">Latest plan</h4>
                  <div className="rounded-xl bg-accent/60 p-3">
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span className="font-medium text-accent-foreground">{lastPlan.actor}</span>
                      <span className="ml-auto tabular-nums">{time(lastPlan.occurredAt)}</span>
                    </div>
                    <p className="mt-1 text-sm">{lastPlan.text}</p>
                    {reasonOf(lastPlan) && <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{reasonOf(lastPlan)}</p>}
                  </div>
                </section>
              )}

              <section>
                <h4 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">
                  <Radio className="size-3.5" /> What the agents are doing
                </h4>
                {beats.length || events.length ? (
                  <ol className="relative space-y-3 border-l pl-4">
                    {(beats.length
                      ? beats.slice(0, 12).map((b, k) => ({ key: `b${b.tick}-${k}`, at: b.at, text: b.text, kind: b.kind, why: "" }))
                      : events.filter((e) => !e.kind.startsWith("plan.")).slice(0, 12)
                          .map((e) => ({ key: `e${e.id}`, at: e.occurredAt, text: e.text || pretty(e.kind), kind: e.kind, why: reasonOf(e) }))
                    ).map((x, k) => (
                      <li key={x.key} className="relative">
                        <span className={cn("absolute top-1.5 -left-[21px] size-2.5 rounded-full ring-4 ring-card", k === 0 ? "bg-primary" : "bg-muted-foreground/40")} />
                        <div className="text-[11px] tabular-nums text-muted-foreground">{time(x.at)} · {pretty(x.kind)}</div>
                        <p className={cn("text-sm", k === 0 && "font-medium")}>{x.text}</p>
                        {x.why && <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">{x.why}</p>}
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p className="text-sm text-muted-foreground">Nothing yet for this area.</p>
                )}
              </section>

              <section>
                <h4 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">
                  <ShieldCheck className="size-3.5" /> Waiting on a person · {pending.length}
                </h4>
                {pending.length ? pending.map((d) => (
                  <div key={d.id} className="mb-2 rounded-xl border p-3">
                    <div className="flex items-center text-sm font-medium">
                      {pretty(d.action)}
                      <span className="ml-auto text-[11px] tabular-nums text-muted-foreground">{Math.round(d.confidence * 100)}%</span>
                    </div>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{d.rationale}</p>
                  </div>
                )) : <p className="text-sm text-muted-foreground">Nothing waiting.</p>}
              </section>

              <section>
                <h4 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">
                  <AlertTriangle className="size-3.5" /> Not covered · {uncovered.length}
                </h4>
                {uncovered.length ? uncovered.map((u, k) => (
                  <p key={k} className="mb-2 text-xs leading-relaxed">
                    <b className="font-medium">{u.capability ? pretty(u.capability) : "Demand"}</b>
                    {u.incident_id && title.get(u.incident_id) ? ` for ${title.get(u.incident_id)}` : ""}
                    <span className="block text-muted-foreground">{u.reason}</span>
                  </p>
                )) : <p className="text-sm text-muted-foreground">The last plan covered every demand here.</p>}
              </section>
            </div>
          )}

          {tab === "log" && (
            <div>
              <div className="flex flex-wrap gap-1.5 border-b px-5 py-3">
                {(Object.keys(GROUP) as KindGroup[]).filter((g) => counts[g] > 0).map((g) => (
                  <Pill key={g} on={groups.includes(g)} count={counts[g]} icon={GROUP[g].icon}
                        onClick={() => setGroups((cur) => (cur.includes(g) ? cur.filter((x) => x !== g) : [...cur, g]))}>
                    {GROUP[g].label}
                  </Pill>
                ))}
              </div>
              {log.length ? (
                <ol className="divide-y">
                  {log.slice(0, 200).map((e) => {
                    const g = GROUP[GROUP_OF(e.kind)]
                    const why = reasonOf(e)
                    return (
                      <li key={e.id} className="flex gap-3 px-5 py-3">
                        <span className={cn("grid size-7 shrink-0 place-items-center rounded-lg", g.tone)}>
                          <g.icon className="size-3.5" />
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                            <span className="font-medium text-foreground">{pretty(e.kind)}</span> · {e.actor}
                            <span className="ml-auto tabular-nums">{time(e.occurredAt)}</span>
                          </div>
                          {e.text && <p className="mt-0.5 text-[13px]">{e.text}</p>}
                          {why && why !== e.text && <p className="mt-1 rounded-lg bg-muted/70 px-2.5 py-1.5 text-xs leading-relaxed text-muted-foreground">{why}</p>}
                        </div>
                      </li>
                    )
                  })}
                </ol>
              ) : (
                <p className="p-5 text-sm text-muted-foreground">Nothing logged for this area yet.</p>
              )}
            </div>
          )}

          {tab === "routes" && (
            scoped.routes.length ? (
              <ul className="divide-y">
                {scoped.routes.map((r) => {
                  const Icon = unitIcon(r.resourceKind)
                  const pct = Math.round(Math.max(0, Math.min(1, r.progress || 0)) * 100)
                  return (
                    <li key={r.id} className="flex gap-3 px-5 py-3.5">
                      <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-accent text-accent-foreground">
                        <Icon className="size-4" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="truncate text-sm font-medium">{r.resourceLabel}</span>
                          <span className="ml-auto text-sm font-semibold tabular-nums">
                            {r.etaMinutes != null ? `${Math.round(r.etaMinutes)} min` : "—"}
                          </span>
                        </div>
                        <div className="truncate text-xs text-muted-foreground">→ {r.incidentTitle}</div>
                        <div className="mt-1.5 flex items-center gap-2">
                          <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                            <span className="block h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
                          </span>
                          <span className="text-[11px] tabular-nums text-muted-foreground">{r.status.replace(/_/g, " ")} · {pct}%</span>
                        </div>
                        {r.steps[0] && (
                          <div className="mt-1 truncate text-[11px] text-muted-foreground">
                            Next: {r.steps[0].instruction}{r.steps[0].street ? ` · ${r.steps[0].street}` : ""}
                          </div>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            ) : <p className="p-5 text-sm text-muted-foreground">No unit is routed into this area yet.</p>
          )}
        </div>
      </aside>
    </div>
  )
}
