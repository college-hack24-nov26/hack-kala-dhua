import { useEffect, useMemo, useState } from "react"
import { Link, useNavigate, useParams } from "react-router-dom"
import {
  AlertTriangle, ArrowLeft, ChevronLeft, Footprints, Maximize2, ChevronRight, ClipboardList, Gauge, HeartPulse, Siren, Truck, Users, Workflow,
} from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Button } from "@/components/ui/button"
import { StatCard } from "@/components/common/StatCard"
import { RiskRing } from "@/components/common/MacControls"
import { cn } from "@/lib/utils"
import WallAnalytics from "./WallAnalytics"
import { WardPanel } from "./WardPanel"
import { HAZARD_ICON, SEV_BAR, ScreenMap, people } from "./ZoneMap"
import {
  ALLOCATION, HAZARD_LABEL, REGIONS, hazardOf, isLifeSafety, scopeState, zonesOf, type Zone,
} from "./zones"

/** One zone, full page: its live map, why it ranks where it does, its
 *  incidents, and every chart for it. "Agent routing & decisions" goes one
 *  level further, to how the agents are working it. */

export function useZone() {
  const { zoneId = "" } = useParams()
  const id = decodeURIComponent(zoneId)
  const { state, region: regionPick } = useDemo()
  const region = regionPick === "all" ? null : REGIONS.find((r) => r.id === regionPick) ?? null
  const zones = useMemo(() => zonesOf(state), [state])
  const index = zones.findIndex((z) => z.id === id)
  return { id, state, region, zones, index, zone: index >= 0 ? zones[index] : null }
}

export function ZoneMissing({ id }: { id: string }) {
  return (
    <div className="grid place-items-center p-16 text-center">
      <p className="text-sm font-medium">No open incident in this ward right now.</p>
      <p className="mt-1 text-xs text-muted-foreground">
        {id} may have been resolved; zones leave the wall when their last incident closes.
      </p>
      <Button asChild variant="outline" size="sm" className="mt-4">
        <Link to="/admin/wall"><ArrowLeft className="size-4" /> Back to the command wall</Link>
      </Button>
    </div>
  )
}

export function ZoneNav({ zones, index, base }: { zones: Zone[]; index: number; base: (z: Zone) => string }) {
  const navigate = useNavigate()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t?.closest("input, textarea, select, [contenteditable='true']")) return
      if (e.key === "ArrowRight" && index < zones.length - 1) navigate(base(zones[index + 1]))
      if (e.key === "ArrowLeft" && index > 0) navigate(base(zones[index - 1]))
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [zones, index, base, navigate])
  return (
    <div className="flex items-center gap-1 rounded-[10px] bg-muted p-[3px]">
      <button disabled={index <= 0} onClick={() => navigate(base(zones[index - 1]))}
              className="grid size-8 place-items-center rounded-[8px] text-muted-foreground hover:text-foreground disabled:opacity-40"
              aria-label="Riskier zone" title="Riskier zone (←)">
        <ChevronLeft className="size-4" />
      </button>
      <span className="px-1.5 text-xs font-medium tabular-nums text-muted-foreground">
        #{index + 1} of {zones.length}
      </span>
      <button disabled={index >= zones.length - 1} onClick={() => navigate(base(zones[index + 1]))}
              className="grid size-8 place-items-center rounded-[8px] text-muted-foreground hover:text-foreground disabled:opacity-40"
              aria-label="Next zone" title="Next zone (→)">
        <ChevronRight className="size-4" />
      </button>
    </div>
  )
}

export function ZoneHeader({ zone, index, zones, children }: {
  zone: Zone; index: number; zones: Zone[]; children?: React.ReactNode
}) {
  const a = ALLOCATION[zone.allocation]
  return (
    <div className="flex flex-wrap items-center gap-4">
      <RiskRing value={zone.risk} size={56} stroke={5} />
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="truncate text-xl font-semibold tracking-tight">{zone.name}</h2>
          <span className={cn("inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ring-1", a.tone)}>
            <span className={cn("size-1.5 rounded-full", a.dot)} />
            {a.label}
          </span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-muted-foreground">Rank #{index + 1} by risk · S{zone.severity} worst ·</span>
          {zone.hazards.map((h) => {
            const Icon = HAZARD_ICON[h]
            return (
              <span key={h} className="inline-flex items-center gap-1 rounded-md bg-muted px-1.5 py-0.5 text-[11px] font-medium">
                <Icon className="size-3 text-primary" /> {HAZARD_LABEL[h]}
              </span>
            )
          })}
        </div>
      </div>
      <div className="ml-auto flex flex-wrap items-center gap-2">
        <ZoneNav zones={zones} index={index} base={(z) => `/admin/wall/zone/${encodeURIComponent(z.id)}`} />
        {children}
      </div>
    </div>
  )
}

/** The four parts of the risk index, each as a bar of its share. */
function RiskBreakdown({ zone }: { zone: Zone }) {
  const parts = [
    { label: "Worst severity", value: (zone.severity / 5) * 50, max: 50, note: `S${zone.severity}` },
    { label: "Life-safety incidents", value: (Math.min(zone.lifeSafety, 5) / 5) * 25, max: 25, note: `${zone.lifeSafety} incident${zone.lifeSafety === 1 ? "" : "s"}` },
    { label: "People exposed", value: Math.min((zone.exposed ?? 0) / 20000, 1) * 15, max: 15, note: zone.exposed == null ? "ward not scored" : people(zone.exposed) },
    { label: "Nobody on the way", value: (Math.min(zone.unattended, 3) / 3) * 10, max: 10, note: `${zone.unattended} incident${zone.unattended === 1 ? "" : "s"}` },
  ]
  return (
    <div className="space-y-3">
      {parts.map((p) => (
        <div key={p.label}>
          <div className="mb-1 flex items-baseline justify-between text-xs">
            <span className="font-medium">{p.label}</span>
            <span className="tabular-nums text-muted-foreground">{p.note} · {Math.round(p.value)}/{p.max}</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-gradient-to-r from-[#f0a24a] to-[#d03b3b]" style={{ width: `${(p.value / p.max) * 100}%` }} />
          </div>
        </div>
      ))}
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        Fixed weights, not learned, so the arithmetic can be checked: severity 50, lives at stake 25,
        people exposed 15, nobody assigned 10.
      </p>
    </div>
  )
}

export default function ZonePage() {
  const navigate = useNavigate()
  const { id, state, region, zones, index, zone } = useZone()
  const [ward, setWard] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 5000)
    return () => clearInterval(t)
  }, [])
  const scoped = useMemo(() => scopeState(state, zone), [state, zone])
  if (!zone) return <ZoneMissing id={id} />

  const covered = zone.needsTotal ? Math.round((zone.needsMet / zone.needsTotal) * 100) : null
  const unitsHere = scoped.resources.filter((r) => r.incidentId && zone.incidents.some((i) => i.id === r.incidentId))

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <Link to="/admin/wall" className="inline-flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-3.5" /> Command wall
      </Link>

      <ZoneHeader zone={zone} index={index} zones={zones}>
        <Button variant="outline" size="sm" className="h-9 gap-1.5" onClick={() => setWard(zone.id)}>
          <ClipboardList className="size-4" /> Ward numbers & approvals
        </Button>
        <Button variant="outline" size="sm" className="h-9 gap-1.5" onClick={() => navigate(`/admin/evidence?ward=${encodeURIComponent(zone.id)}`)}>
          <Footprints className="size-4" /> Evidence trail
        </Button>
        <Button size="sm" className="h-9 gap-1.5" onClick={() => navigate(`/admin/wall/zone/${encodeURIComponent(zone.id)}/agents`)}>
          <Workflow className="size-4" /> Agent routing & decisions
        </Button>
      </ZoneHeader>

      <div className="grid gap-5 lg:grid-cols-[1fr_340px]">
        <div className="relative overflow-hidden rounded-2xl border shadow-card">
          <ScreenMap state={state} zone={zone} region={region} big className="h-[460px] w-full lg:h-[62vh]" onPickWard={setWard} />
          <button onClick={() => navigate(`/admin/wall/zone/${encodeURIComponent(zone.id)}/live`)}
                  className="absolute top-3 left-3 inline-flex h-9 items-center gap-1.5 rounded-lg border bg-card/95 px-3 text-sm font-medium shadow-sm backdrop-blur hover:bg-card">
            <Maximize2 className="size-4" /> Full screen
          </button>
        </div>
        <div className="space-y-5">
          <section className="rounded-2xl border bg-card p-5 shadow-card">
            <h3 className="mb-4 text-sm font-semibold">Why it ranks #{index + 1}</h3>
            <RiskBreakdown zone={zone} />
          </section>
          <section className="rounded-2xl border bg-card p-5 shadow-card">
            <h3 className="mb-3 text-sm font-semibold">Units working here</h3>
            {unitsHere.length ? (
              <ul className="space-y-2">
                {unitsHere.slice(0, 6).map((r) => (
                  <li key={r.id} className="flex items-center gap-2 text-sm">
                    <span className={cn("size-2 rounded-full", /on_site/.test(r.status) ? "bg-emerald-500" : "bg-blue-500")} />
                    <span className="min-w-0 flex-1 truncate">{r.label}</span>
                    <span className="text-xs tabular-nums text-muted-foreground">
                      {/on_site/.test(r.status) ? "on scene" : r.etaMinutes != null ? `${Math.round(r.etaMinutes)} min` : r.status.replace(/_/g, " ")}
                    </span>
                  </li>
                ))}
                {unitsHere.length > 6 && <li className="text-xs text-muted-foreground">+{unitsHere.length - 6} more</li>}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">Nobody is on the way yet.</p>
            )}
          </section>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <StatCard icon={Siren} label="Open incidents" value={zone.incidents.length} sub={`${zone.reports} reports`} />
        <StatCard icon={HeartPulse} label="Life-safety incidents" value={zone.lifeSafety} tone={zone.lifeSafety ? "bad" : undefined} sub="casualty, stranded, fire, collapse" />
        <StatCard icon={Users} label="People exposed" value={people(zone.exposed)} sub="ward's estimate" />
        <StatCard icon={Truck} label="Units en route" value={zone.unitsEnRoute} sub={`${zone.onScene} on scene`} />
        <StatCard icon={Gauge} label="Needs covered" value={covered == null ? "—" : `${covered}%`} sub={`${zone.needsMet} of ${zone.needsTotal}`} tone={covered != null && covered < 100 ? "warn" : undefined} />
        <StatCard icon={AlertTriangle} label="Nobody assigned" value={zone.unattended} tone={zone.unattended ? "bad" : undefined} sub="incidents" />
      </div>

      <section className="rounded-2xl border bg-card shadow-card">
        <div className="flex items-center justify-between border-b px-5 py-4">
          <h3 className="text-sm font-semibold">Incidents in {zone.name}</h3>
          <span className="text-xs text-muted-foreground">worst first · click to open the response</span>
        </div>
        <ul className="divide-y">
          {zone.incidents.map((i) => {
            const Icon = HAZARD_ICON[hazardOf(i.category)]
            return (
              <li key={i.id}>
                <button onClick={() => navigate(`/admin/response?incident=${i.id}`)}
                        className="flex w-full items-center gap-4 px-5 py-3.5 text-left transition-colors hover:bg-muted/50">
                  <span className={cn("h-8 w-1 shrink-0 rounded-full", SEV_BAR[i.severity] ?? "bg-zinc-300")} />
                  <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-accent text-accent-foreground">
                    <Icon className="size-4" />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{i.title}</span>
                    <span className="block truncate text-xs text-muted-foreground">
                      S{i.severity} · {i.category.replace(/_/g, " ")}{i.street ? ` · ${i.street}` : ""} · {i.reportCount} report{i.reportCount === 1 ? "" : "s"}
                      {isLifeSafety(i.category) && <b className="ml-1 font-medium text-red-600 dark:text-red-400">· life at stake</b>}
                    </span>
                  </span>
                  <span className={cn("shrink-0 rounded-full px-2.5 py-1 text-[11px] font-medium",
                    i.unitsEnRoute ? "bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300" : "bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300")}>
                    {i.unitsEnRoute ? `${i.unitsEnRoute} unit${i.unitsEnRoute === 1 ? "" : "s"}` : "nobody assigned"}
                  </span>
                  <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                </button>
              </li>
            )
          })}
        </ul>
      </section>

      <WallAnalytics zones={[zone]} now={now} state={scoped} scope={zone} stats={false} onClearScope={() => navigate("/admin/wall")} />
      <WardPanel wardId={ward} onClose={() => setWard(null)} />
    </div>
  )
}
