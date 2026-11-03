import { useEffect, useMemo, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  ChevronLeft, ChevronRight, Database, HeartPulse, Map as MapIcon, Maximize2, RotateCcw, Search,
  Siren, Truck, Users, X,
} from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import type { DemoState } from "@/routes/demo/useDemo"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Pill, RiskRing, Segmented } from "@/components/common/MacControls"
import { cn } from "@/lib/utils"
import WallAnalytics from "./WallAnalytics"
import { WallMiniMap } from "./WallMiniMap"
import { WardPanel } from "./WardPanel"
import { UnassignedIncidents } from "./UnassignedIncidents"
import {
  HAZARD_ICON, SEV_CHIP, ScreenMap, people, useSeenOnce, useSize, useThrottledState,
} from "./ZoneMap"
import {
  ALLOCATION, HAZARD_LABEL, REGIONS, scopeState, zonesOf,
  type Allocation, type Hazard, type Region, type Zone,
} from "./zones"

/** The command wall: the first screen an officer sees.
 *
 *  One screen per ward with an open incident, ranked by risk (severity, lives
 *  at stake, people exposed, nobody on the way), six to a page. Filters narrow
 *  the wall by severity, allocation state and hazard; a click on a screen
 *  focuses the analytics underneath on that zone; ⤢ opens the zone's own page,
 *  and from there its agent routing and decisions.
 */

const PAGE = 6
type Sort = "risk" | "newest" | "unassigned"
type SevFilter = "all" | "3" | "4" | "5"
const FILTER_KEY = "wall:filters:v2"

function load<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    return raw ? { ...fallback, ...(JSON.parse(raw) as T) } : fallback
  } catch {
    return fallback
  }
}
function save(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* private mode: filters just are not remembered */
  }
}

type Filters = { sort: Sort; sev: SevFilter; alloc: Allocation[]; hazards: Hazard[]; q: string }
const NO_FILTERS: Filters = { sort: "risk", sev: "all", alloc: [], hazards: [], q: "" }

function Metric({ icon: Icon, value, label, warn }: {
  icon: typeof Siren; value: string | number; label: string; warn?: boolean
}) {
  return (
    <div className="min-w-0" title={label}>
      <div className={cn("flex items-center gap-1.5 text-[15px] font-semibold tabular-nums", warn && "text-red-600 dark:text-red-400")}>
        <Icon className={cn("size-3.5 shrink-0", warn ? "text-red-500" : "text-muted-foreground")} />
        {value}
      </div>
      <div className="truncate text-[11px] text-muted-foreground">{label}</div>
    </div>
  )
}

function ZoneTile({ state, zone, rank, region, fresh, selected, onSelect, onExpand }: {
  state: DemoState; zone: Zone; rank: number; region: Region | null; fresh: boolean
  selected: boolean; onSelect: () => void; onExpand: () => void
}) {
  const [seenRef, seen] = useSeenOnce<HTMLDivElement>()
  const [mapRef, size] = useSize<HTMLDivElement>()
  const a = ALLOCATION[zone.allocation]
  const covered = zone.needsTotal ? Math.round((zone.needsMet / zone.needsTotal) * 100) : null
  return (
    <article
      ref={seenRef}
      className={cn(
        "group flex min-w-0 flex-col overflow-hidden rounded-2xl border bg-card shadow-card transition-all hover:shadow-md",
        selected ? "border-primary ring-2 ring-primary/25" : "border-border",
        fresh && !selected && "ring-2 ring-amber-300/70",
      )}
    >
      <div
        ref={mapRef}
        role="button"
        tabIndex={0}
        aria-pressed={selected}
        title={`Focus the analytics on ${zone.name} · double-click to open`}
        onClick={onSelect}
        onDoubleClick={onExpand}
        onKeyDown={(e) => {
          if (e.key === "Enter") onExpand()
          if (e.key === " ") {
            e.preventDefault()
            onSelect()
          }
        }}
        className="relative aspect-[16/10] cursor-pointer overflow-hidden bg-muted"
      >
        {seen && <WallMiniMap state={state} zone={zone} region={region} w={size.w} h={size.h} />}

        <div className="pointer-events-none absolute inset-x-0 top-0 flex items-start gap-1.5 p-3">
          <span className="rounded-lg bg-foreground px-2 py-1 text-[11px] font-semibold tabular-nums text-background shadow-sm">
            #{rank}
          </span>
          <span className={cn("rounded-lg px-2 py-1 text-[11px] font-semibold shadow-sm", SEV_CHIP[zone.severity])}>
            S{zone.severity}
          </span>
          {fresh && <Badge className="h-[22px] rounded-lg bg-amber-400 text-[10px] text-black">NEW</Badge>}
          <button
            onClick={(e) => {
              e.stopPropagation()
              onExpand()
            }}
            className="pointer-events-auto ml-auto grid size-8 place-items-center rounded-lg border bg-card/95 text-foreground shadow-sm backdrop-blur transition hover:bg-card focus-visible:opacity-100 md:opacity-0 md:group-hover:opacity-100"
            aria-label={`Open ${zone.name}`}
            title="Open this zone"
          >
            <Maximize2 className="size-4" />
          </button>
          <div className="rounded-full bg-card/95 p-0.5 shadow-sm backdrop-blur" title="Risk index, 0-100">
            <RiskRing value={zone.risk} size={40} stroke={3.5} />
          </div>
        </div>

        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-end gap-1.5 p-3">
          <div className="flex flex-wrap gap-1">
            {zone.hazards.map((h) => {
              const Icon = HAZARD_ICON[h]
              return (
                <span key={h} title={HAZARD_LABEL[h]}
                      className="inline-flex items-center gap-1 rounded-lg bg-card/95 px-1.5 py-1 text-[11px] font-medium shadow-sm backdrop-blur">
                  <Icon className="size-3.5 text-primary" />
                  <span className="hidden sm:inline">{HAZARD_LABEL[h]}</span>
                </span>
              )
            })}
          </div>
        </div>
      </div>

      <div className="space-y-3.5 p-4">
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-[15px] font-semibold tracking-tight">{zone.name}</h3>
            <p className="truncate text-xs text-muted-foreground">
              {zone.incidents[0]?.title ?? "—"}
            </p>
          </div>
          <span className={cn("inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-medium ring-1", a.tone)}>
            <span className={cn("size-1.5 rounded-full", a.dot)} />
            {a.label}
          </span>
        </div>
        <div className="grid grid-cols-4 gap-2">
          <Metric icon={Siren} value={zone.incidents.length} label="Incidents" />
          <Metric icon={HeartPulse} value={zone.lifeSafety} label="Life-safety" warn={zone.lifeSafety > 0} />
          <Metric icon={Users} value={people(zone.exposed)} label="Exposed" />
          <Metric icon={Truck} value={`${zone.unitsEnRoute}`} label="En route" />
        </div>
        <div>
          <div className="mb-1 flex items-center justify-between text-[11px] text-muted-foreground">
            <span>Needs covered{zone.onScene > 0 ? ` · ${zone.onScene} unit${zone.onScene === 1 ? "" : "s"} on scene` : ""}</span>
            <span className="tabular-nums">
              {zone.needsTotal ? `${zone.needsMet}/${zone.needsTotal}` : "none recorded"}
              {zone.unattended > 0 && <b className="ml-1.5 font-medium text-red-600 dark:text-red-400">· {zone.unattended} unassigned</b>}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              className={cn("h-full rounded-full", covered == null ? "" : covered >= 100 ? "bg-emerald-500" : covered >= 50 ? "bg-primary" : "bg-amber-500")}
              style={{ width: `${covered ?? 0}%` }}
            />
          </div>
        </div>
      </div>
    </article>
  )
}

/** The whole region on one interactive map, over everything. */
function CityOverlay({ state, region, onClose, onPickWard }: {
  state: DemoState; region: Region | null; onClose: () => void; onPickWard: (id: string) => void
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose()
    window.addEventListener("keydown", onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = "hidden"
    return () => {
      window.removeEventListener("keydown", onKey)
      document.body.style.overflow = prev
    }
  }, [onClose])
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-background p-5 md:p-8 xl:px-10">
      <div className="mb-4 flex items-center gap-3">
        <MapIcon className="size-5 text-primary" />
        <div>
          <div className="text-base font-semibold tracking-tight">{region?.name ?? "Every region"} · whole city</div>
          <div className="text-xs text-muted-foreground">Click a ward for its numbers and approvals · Esc to close</div>
        </div>
        <Button size="icon" variant="outline" className="ml-auto" onClick={onClose} aria-label="Close">
          <X className="size-4" />
        </Button>
      </div>
      <div className="relative min-h-0 flex-1 overflow-hidden rounded-2xl border shadow-card">
        <div className="absolute inset-0">
          <ScreenMap state={state} zone={null} region={region} big className="h-full w-full" onPickWard={onPickWard} />
        </div>
      </div>
    </div>
  )
}

function Pager({ page, pages, onPage, from, to, total }: {
  page: number; pages: number; onPage: (p: number) => void; from: number; to: number; total: number
}) {
  if (total === 0) return null
  return (
    <div className="flex flex-col items-center justify-between gap-3 sm:flex-row">
      <span className="text-xs text-muted-foreground tabular-nums">
        Showing {from}–{to} of {total} zone{total === 1 ? "" : "s"} · ranked by risk
      </span>
      <div className="flex items-center gap-1 rounded-[10px] bg-muted p-[3px]">
        <button
          onClick={() => onPage(page - 1)}
          disabled={page === 0}
          className="grid size-8 place-items-center rounded-[8px] text-muted-foreground hover:text-foreground disabled:opacity-40"
          aria-label="Previous page"
        >
          <ChevronLeft className="size-4" />
        </button>
        {Array.from({ length: pages }, (_, i) => i)
          .filter((i) => pages <= 7 || i === 0 || i === pages - 1 || Math.abs(i - page) <= 1)
          .map((i, k, arr) => (
            <span key={i} className="flex items-center">
              {k > 0 && arr[k - 1] !== i - 1 && <span className="px-1 text-xs text-muted-foreground">…</span>}
              <button
                onClick={() => onPage(i)}
                className={cn(
                  "h-8 min-w-8 rounded-[8px] px-2 text-[13px] font-medium tabular-nums transition-all",
                  i === page ? "bg-card text-foreground shadow-[0_1px_2px_rgb(16_24_40/0.08)]" : "text-muted-foreground hover:text-foreground",
                )}
                aria-current={i === page ? "page" : undefined}
              >
                {i + 1}
              </button>
            </span>
          ))}
        <button
          onClick={() => onPage(page + 1)}
          disabled={page >= pages - 1}
          className="grid size-8 place-items-center rounded-[8px] text-muted-foreground hover:text-foreground disabled:opacity-40"
          aria-label="Next page"
        >
          <ChevronRight className="size-4" />
        </button>
      </div>
    </div>
  )
}

export default function CommandWall() {
  const navigate = useNavigate()
  const { state, region: regionPick } = useDemo()
  const region = regionPick === "all" ? null : REGIONS.find((r) => r.id === regionPick) ?? null
  // Small screens redraw every 4 s; the analytics follow the same snapshot.
  const wallState = useThrottledState(state, 4000, region?.id ?? "all")
  const zones = useMemo(() => zonesOf(wallState), [wallState])
  const [f, setF] = useState<Filters>(() => load(FILTER_KEY, NO_FILTERS))
  const set = (patch: Partial<Filters>) => {
    setF((old) => {
      const next = { ...old, ...patch }
      save(FILTER_KEY, { ...next, q: "" })
      return next
    })
    setPage(0)
  }
  const toggle = <T,>(list: T[], v: T) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v])
  const [page, setPage] = useState(0)
  const [city, setCity] = useState(false)
  const [ward, setWard] = useState<string | null>(null)
  const [scopeId, setScopeId] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 5000)
    return () => clearInterval(id)
  }, [])

  // Rank is the zone's place in the whole risk order, whatever is filtered.
  const rankOf = useMemo(() => new Map(zones.map((z, i) => [z.id, i + 1])), [zones])

  const bySev = (z: Zone) => f.sev === "all" || z.severity >= Number(f.sev)
  const byAlloc = (z: Zone) => !f.alloc.length || f.alloc.includes(z.allocation)
  const byHaz = (z: Zone) => !f.hazards.length || z.hazards.some((h) => f.hazards.includes(h))
  const byQ = (z: Zone) =>
    !f.q.trim() || `${z.name} ${z.incidents.map((i) => i.title).join(" ")}`.toLowerCase().includes(f.q.trim().toLowerCase())

  const shown = useMemo(() => {
    const list = zones.filter((z) => bySev(z) && byAlloc(z) && byHaz(z) && byQ(z))
    if (f.sort === "newest") return [...list].sort((a, b) => b.since - a.since)
    if (f.sort === "unassigned") return [...list].sort((a, b) => b.unattended - a.unattended || b.risk - a.risk)
    return list
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [zones, f])

  // Counts for each chip, given the other filters.
  const allocCount = (a: Allocation) => zones.filter((z) => bySev(z) && byHaz(z) && byQ(z) && z.allocation === a).length
  const hazCount = (h: Hazard) => zones.filter((z) => bySev(z) && byAlloc(z) && byQ(z) && z.hazards.includes(h)).length
  const hazardsPresent = (Object.keys(HAZARD_LABEL) as Hazard[]).filter((h) => zones.some((z) => z.hazards.includes(h)))

  const pages = Math.max(1, Math.ceil(shown.length / PAGE))
  const p = Math.min(page, pages - 1)
  const pageZones = shown.slice(p * PAGE, p * PAGE + PAGE)
  const filtered = f.sev !== "all" || f.alloc.length > 0 || f.hazards.length > 0 || !!f.q.trim()

  const unassignedRef = useRef<HTMLElement | null>(null)
  const scope = scopeId ? zones.find((z) => z.id === scopeId) ?? null : null
  const scoped = useMemo(() => scopeState(wallState, scope), [wallState, scope])
  const unattended = zones.reduce((s, z) => s + z.unattended, 0)
  const lives = zones.reduce((s, z) => s + z.lifeSafety, 0)

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      {/* summary */}
      <section className="flex flex-wrap items-end gap-x-8 gap-y-3">
        <div>
          <p className="text-sm text-muted-foreground">
            {region?.name ?? "Every region"} · one screen per ward with an open incident, riskiest first
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {state.cachedAt && (
            <Badge variant="outline" className="gap-1 text-[11px]">
              <Database className="size-3" /> cached view, connecting…
            </Badge>
          )}
          <SummaryChip label="Active zones" value={zones.length} />
          <SummaryChip label="Life-safety incidents" value={lives} warn={lives > 0} />
          <SummaryChip label="Unassigned incidents" value={unattended} warn={unattended > 0}
                       onClick={() => unassignedRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })} />
          <SummaryChip label="Highest risk" value={zones[0]?.risk ?? 0} />
          <Button variant="outline" size="sm" className="h-9 gap-1.5" onClick={() => setCity(true)}>
            <MapIcon className="size-4" /> Whole city
          </Button>
        </div>
      </section>

      {/* filters */}
      <section className="space-y-4 rounded-2xl border bg-card p-4 shadow-card md:p-5">
        <div className="flex flex-wrap items-center gap-3">
          <label className="relative min-w-[200px] flex-1 md:max-w-xs">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={f.q}
              onChange={(e) => set({ q: e.target.value })}
              placeholder="Search wards or incidents"
              className="h-9 w-full rounded-[10px] border border-input bg-muted/50 pr-3 pl-9 text-sm outline-none transition focus:border-primary focus:bg-card focus:ring-3 focus:ring-primary/15"
            />
          </label>
          <Segmented
            ariaLabel="Sort"
            value={f.sort}
            onChange={(sort) => set({ sort })}
            options={[
              { value: "risk", label: "Risk" },
              { value: "unassigned", label: "Unassigned" },
              { value: "newest", label: "Newest" },
            ]}
          />
          <Segmented
            ariaLabel="Severity"
            value={f.sev}
            onChange={(sev) => set({ sev })}
            options={[
              { value: "all", label: "All severities" },
              { value: "3", label: "S3+" },
              { value: "4", label: "S4+" },
              { value: "5", label: "S5" },
            ]}
          />
          {filtered && (
            <button
              onClick={() => set({ ...NO_FILTERS, sort: f.sort })}
              className="ml-auto inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
            >
              <RotateCcw className="size-3.5" /> Clear filters
            </button>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="mr-1 text-xs font-medium text-muted-foreground">Allocation</span>
            {(Object.keys(ALLOCATION) as Allocation[]).map((a) => (
              <Pill key={a} on={f.alloc.includes(a)} onClick={() => set({ alloc: toggle(f.alloc, a) })}
                    dot={ALLOCATION[a].dot} count={allocCount(a)}>
                {ALLOCATION[a].label}
              </Pill>
            ))}
          </div>
          {hazardsPresent.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="mr-1 text-xs font-medium text-muted-foreground">Hazard</span>
              {hazardsPresent.map((h) => (
                <Pill key={h} on={f.hazards.includes(h)} onClick={() => set({ hazards: toggle(f.hazards, h) })}
                      icon={HAZARD_ICON[h]} count={hazCount(h)}>
                  {HAZARD_LABEL[h]}
                </Pill>
              ))}
            </div>
          )}
        </div>
      </section>

      {/* the wall */}
      <section className="space-y-5">
        {pageZones.length ? (
          <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
            {pageZones.map((z) => (
              <ZoneTile
                key={z.id}
                state={wallState}
                zone={z}
                rank={rankOf.get(z.id) ?? 0}
                region={region}
                fresh={now - z.since < 2 * 60_000}
                selected={scopeId === z.id}
                onSelect={() => setScopeId((cur) => (cur === z.id ? null : z.id))}
                onExpand={() => navigate(`/admin/wall/zone/${encodeURIComponent(z.id)}`)}
              />
            ))}
          </div>
        ) : (
          <div className="grid place-items-center rounded-2xl border-2 border-dashed p-12 text-center">
            <Users className="mb-3 size-6 text-muted-foreground" />
            <p className="text-sm font-medium">
              {zones.length ? "No zone matches these filters." : "No open incidents."}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {zones.length ? "Clear a filter to see more." : "A screen appears here the moment a ward has an open incident."}
            </p>
          </div>
        )}
        <Pager page={p} pages={pages} onPage={(n) => setPage(Math.max(0, Math.min(pages - 1, n)))}
               from={shown.length ? p * PAGE + 1 : 0} to={p * PAGE + pageZones.length} total={shown.length} />
      </section>

      <UnassignedIncidents
        ref={unassignedRef}
        state={wallState}
        now={now}
        sev={f.sev === "all" ? 0 : Number(f.sev)}
        hazards={f.hazards}
        q={f.q}
        rankOf={rankOf}
      />

      <WallAnalytics
        zones={scope ? [scope] : zones}
        now={now}
        state={scoped}
        scope={scope}
        onClearScope={() => setScopeId(null)}
      />

      {city && <CityOverlay state={state} region={region} onClose={() => setCity(false)} onPickWard={setWard} />}
      <WardPanel wardId={ward} onClose={() => setWard(null)} />
    </div>
  )
}

function SummaryChip({ label, value, warn, onClick }: { label: string; value: number; warn?: boolean; onClick?: () => void }) {
  const Comp = onClick ? "button" : "div"
  return (
    <Comp onClick={onClick} title={onClick ? "Show the list" : undefined}
          className={cn("flex h-9 items-center gap-2 rounded-[10px] border bg-card px-3 shadow-xs", onClick && "hover:border-primary/50")}>
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={cn("text-sm font-semibold tabular-nums", warn && "text-red-600 dark:text-red-400")}>{value}</span>
    </Comp>
  )
}
