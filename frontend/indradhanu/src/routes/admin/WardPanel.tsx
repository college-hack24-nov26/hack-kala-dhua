import { useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { Check, ClipboardCheck, Megaphone, Siren, Truck, X } from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { isOpen, km } from "./zones"

/** Everything about one ward, and the approvals waiting for it.
 *
 *  Opened by clicking a ward on any wall screen. Numbers come from the same
 *  snapshot the wall draws from, so the panel and the map never disagree.
 *  Approve / reject go through the same endpoints as the Approvals page.
 */

const SEV: Record<number, string> = {
  5: "bg-[#d03b3b] text-white", 4: "bg-[#ec835a] text-black", 3: "bg-[#fab219] text-black",
}

export function WardPanel({ wardId, onClose }: { wardId: string | null; onClose: () => void }) {
  const { state, run, busy } = useDemo()
  const navigate = useNavigate()
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [])

  const d = useMemo(() => {
    if (!wardId) return null
    const ward = state.wards.find((w) => w.id === wardId)
    if (!ward) return null
    const incidents = state.incidents.filter((i) => i.wardId === wardId)
    const open = incidents.filter((i) => isOpen(i.status)).sort((a, b) => b.severity - a.severity)
    const openIds = new Set(open.map((i) => i.id))
    const units = state.resources.filter((r) => r.incidentId && openIds.has(r.incidentId))
    const needs = state.needs.filter((n) => openIds.has(n.incidentId))
    const required = needs.reduce((s, n) => s + n.required, 0)
    const met = needs.reduce((s, n) => s + Math.min(n.met, n.required), 0)
    const centre = ward.centroid
    const nearby = state.facilities
      .filter((f) => f.wardId === wardId || km(centre, f.location) <= 2)
      .sort((a, b) => km(centre, a.location) - km(centre, b.location))
      .slice(0, 6)
    const decisions = state.decisions
      .filter((x) => x.wardId === wardId || (x.params as Record<string, unknown> | undefined)?.ward_id === wardId
        || (typeof (x.params as Record<string, unknown> | undefined)?.incident_id === "string"
            && openIds.has((x.params as Record<string, string>).incident_id)))
      .sort((a, b) => Number(b.status === "awaiting_approval") - Number(a.status === "awaiting_approval")
        || Date.parse(b.createdAt) - Date.parse(a.createdAt))
    const alerts = state.alerts.filter((a) => a.wardId === wardId)
    const reports = state.reports.filter((r) => r.wardId === wardId)
    const hour = now - 3600_000
    return {
      ward, open, units, required, met, nearby, decisions, alerts,
      reportsHour: reports.filter((r) => Date.parse(r.createdAt) >= hour).length,
      reports: reports.length,
      blocks: state.roadBlocks.filter((b) => km(centre, b.location) <= 2).length,
    }
  }, [wardId, state, now])

  const waiting = d?.decisions.filter((x) => x.status === "awaiting_approval") ?? []

  return (
    <Sheet open={!!wardId} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-md">
        {d ? (
          <>
            <SheetHeader>
              <SheetTitle className="flex items-center gap-2">
                {d.ward.name}
                {d.ward.severity ? (
                  <span className={`rounded px-1.5 text-xs ${SEV[d.ward.severity] ?? "bg-muted"}`}>
                    risk S{d.ward.severity}
                  </span>
                ) : null}
              </SheetTitle>
              <SheetDescription>
                Ward {d.ward.number} · population {d.ward.population.toLocaleString()}
                {d.ward.populationAtRisk ? ` · ${d.ward.populationAtRisk.toLocaleString()} exposed` : ""}
                {d.ward.score != null ? ` · flood risk ${Math.round(d.ward.score * 100)}%` : ""}
              </SheetDescription>
            </SheetHeader>

            <div className="space-y-4 px-4 pb-6 text-sm">
              <div className="grid grid-cols-3 gap-2 text-center">
                <Kpi label="Open incidents" value={d.open.length} />
                <Kpi label="Units on them" value={d.units.length} warn={d.open.length > 0 && d.units.length === 0} />
                <Kpi label="Needs covered" value={d.required ? `${Math.round((d.met / d.required) * 100)}%` : "—"}
                     warn={d.met < d.required} />
                <Kpi label="Reports, last hour" value={d.reportsHour} />
                <Kpi label="Roads blocked nearby" value={d.blocks} />
                <Kpi label="Alerts issued" value={d.alerts.length} />
              </div>

              <section className="space-y-1.5">
                <h3 className="flex items-center gap-1.5 font-medium">
                  <ClipboardCheck className="size-4" /> Approvals
                  {waiting.length > 0 && <Badge className="bg-amber-500 text-black">{waiting.length} waiting</Badge>}
                </h3>
                {d.decisions.length === 0 && <p className="text-muted-foreground text-xs">No decisions for this ward.</p>}
                {d.decisions.slice(0, 8).map((x) => (
                  <div key={x.id} className="rounded-md border p-2 text-xs">
                    <div className="flex items-start gap-2">
                      <span className="flex-1 font-medium">{x.action}</span>
                      <Badge variant="outline" className="text-[10px]">{x.status.replace(/_/g, " ")}</Badge>
                    </div>
                    <p className="text-muted-foreground mt-0.5">{x.rationale}</p>
                    {x.clause && <p className="text-muted-foreground">{x.clause} · {x.delegatedTo}</p>}
                    {x.status === "awaiting_approval" && (
                      <div className="mt-1.5 flex gap-1.5">
                        <Button size="sm" className="h-7" disabled={!!busy}
                                onClick={() => void run(`ok-${x.id}`, `/demo/decisions/${x.id}/approve`)}>
                          <Check className="size-3.5" /> Approve
                        </Button>
                        <Button size="sm" variant="outline" className="h-7" disabled={!!busy}
                                onClick={() => void run(`no-${x.id}`, `/demo/decisions/${x.id}/reject`)}>
                          <X className="size-3.5" /> Reject
                        </Button>
                      </div>
                    )}
                  </div>
                ))}
              </section>

              <section className="space-y-1.5">
                <h3 className="flex items-center gap-1.5 font-medium"><Siren className="size-4" /> Open incidents</h3>
                {d.open.length === 0 && <p className="text-muted-foreground text-xs">None open.</p>}
                {d.open.map((i) => (
                  <button key={i.id} onClick={() => navigate(`/admin/response?incident=${i.id}`)}
                          className="hover:border-primary w-full rounded-md border p-2 text-left text-xs">
                    <div className="flex gap-2">
                      <span className={`rounded px-1 font-semibold ${SEV[i.severity] ?? "bg-muted"}`}>S{i.severity}</span>
                      <span className="flex-1">{i.title}</span>
                    </div>
                    <div className="text-muted-foreground mt-0.5">
                      {i.reportCount} report(s) · {i.unitsEnRoute ? `${i.unitsEnRoute} unit(s) en route` : <span className="text-amber-600">nobody assigned</span>}
                    </div>
                  </button>
                ))}
              </section>

              {d.units.length > 0 && (
                <section className="space-y-1">
                  <h3 className="flex items-center gap-1.5 font-medium"><Truck className="size-4" /> Units working here</h3>
                  {d.units.map((u) => (
                    <div key={u.id} className="flex justify-between text-xs">
                      <span>{u.label} <span className="text-muted-foreground">({u.kind})</span></span>
                      <span className="text-muted-foreground">
                        {u.assignmentStatus ?? u.status}{u.etaMinutes != null ? ` · ETA ${Math.round(u.etaMinutes)} min` : ""}
                      </span>
                    </div>
                  ))}
                </section>
              )}

              {d.nearby.length > 0 && (
                <section className="space-y-1">
                  <h3 className="font-medium">Shelters and hospitals nearby</h3>
                  {d.nearby.map((f) => (
                    <div key={f.id} className="flex justify-between text-xs">
                      <span>{f.name}</span>
                      <span className="text-muted-foreground">
                        {f.capacity ? `${f.occupancy ?? 0}/${f.capacity}` : f.kindLabel}
                      </span>
                    </div>
                  ))}
                </section>
              )}

              {d.alerts.length > 0 && (
                <section className="space-y-1">
                  <h3 className="flex items-center gap-1.5 font-medium"><Megaphone className="size-4" /> Alerts issued</h3>
                  {d.alerts.slice(0, 4).map((a) => (
                    <p key={a.id} className="text-xs">{a.headline}</p>
                  ))}
                </section>
              )}
            </div>
          </>
        ) : (
          <SheetHeader><SheetTitle>Ward</SheetTitle></SheetHeader>
        )}
      </SheetContent>
    </Sheet>
  )
}

function Kpi({ label, value, warn }: { label: string; value: string | number; warn?: boolean }) {
  return (
    <div className={`rounded-md border p-2 ${warn ? "border-amber-500/60 bg-amber-500/5" : ""}`}>
      <div className="text-lg font-semibold tabular-nums">{value}</div>
      <div className="text-muted-foreground text-[11px] leading-tight">{label}</div>
    </div>
  )
}
