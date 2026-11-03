import { useEffect, useMemo, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import mapboxgl from "mapbox-gl"
import "mapbox-gl/dist/mapbox-gl.css"
import {
  Activity, AlertTriangle, Ambulance, Download, FileJson, Flame, MapPin, Navigation, Route, Search, Shield,
  Truck, Users,
} from "lucide-react"
import { apiBaseUrl, request } from "@/api/httpClient"
import { accessToken } from "@/lib/supabase"
import { useRegion } from "@/lib/region"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StatCard, useSeries } from "@/components/common/StatCard"

/** Units, live: where each one is, where it has been, what happened to it, and
 *  which units are working each incident together.
 *
 *  Three questions an officer asks about a vehicle — where is it, why did it
 *  change course, what has it done today — answered from the same rows the
 *  planner uses: positions (GPS or simulated), reroutes with their reasons,
 *  dispatches, field reports. Every log exports as CSV or JSON for the
 *  after-action report.
 */

type Unit = {
  id: string; kind: string; label: string; status: string; agency_id: string | null
  status_note: string | null; unavailable_reason: string | null; last_reported_at: string | null
  lng: number | null; lat: number | null; assignment_id: string | null; incident_id: string | null
  capability_id: string | null; eta_minutes: number | null; progress: number | null
  eta_model?: { p50: number; p90: number; riskMax: number } | null
  assignment_status: string | null; incident_title: string | null; category: string | null
  positions: number; reroutes: number
}
type LogRow = {
  at: string; type: "event" | "field_report" | "position"; kind: string; actor?: string; summary: string
  incident?: string | null; sector?: string | null; hazard?: string | null; lng?: number; lat?: number
  offRouteM?: number | null; progress?: number | null
}
type Track = {
  unit: string; trail: GeoJSON.LineString; route: GeoJSON.LineString | null
  progress: number | null; etaMinutes: number | null; incidentId: string | null
}
type Team = {
  incidentId: string; title: string; category: string; severity: number | null; wardId: string
  needs: { capability: string; required: number; met: number }[]
  units: { unit: string; kind: string; label: string; capability: string | null; status: string; eta: number | null; progress: number | null }[]
  complete: boolean
}

const TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined
const STATUS_TONE: Record<string, string> = {
  available: "bg-emerald-600 text-white", assigned: "bg-amber-500 text-black", en_route: "bg-sky-600 text-white",
  on_site: "bg-violet-600 text-white", offline: "bg-muted text-muted-foreground", unavailable: "bg-muted text-muted-foreground",
}
const KIND_ICON: Record<string, typeof Truck> = {
  ambulance: Ambulance, fire_engine: Flame, police: Shield, rescue_team: Users,
}
const pretty = (s?: string | null) => (s ?? "").replace(/_/g, " ")
const ago = (iso?: string | null) => {
  if (!iso) return "never"
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`
}

async function download(path: string, filename: string) {
  const token = await accessToken()
  const res = await fetch(`${apiBaseUrl}${path}`, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
  if (!res.ok) throw new Error(`${res.status}`)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url; a.download = filename; a.click()
  URL.revokeObjectURL(url)
}

export default function UnitsPage() {
  const [q, setQ] = useState("")
  const [kind, setKind] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [tab, setTab] = useState("units")

  const [region] = useRegion()
  const unitsQ = useQuery({
    queryKey: ["units"], refetchInterval: 5000,
    queryFn: () => request<Unit[]>("/units", { toast: false }),
  })
  // Pune and Ghaziabad share one deployment: the header's region picker scopes
  // this page too (same 75.5 E line the server uses).
  const inRegion = (lng: number | null) =>
    region === "all" || lng == null || (region === "pune" ? lng < 75.5 : lng >= 75.5)
  const units = { ...unitsQ, data: unitsQ.data?.filter((u) => inRegion(u.lng)) }
  const teams = useQuery({
    queryKey: ["teams"], refetchInterval: 8000,
    queryFn: () => request<Team[]>("/units/teams", { toast: false }),
  })

  const list = useMemo(() => {
    const rows = (units.data ?? []).filter((u) =>
      (!kind || u.kind === kind) &&
      (!q || `${u.id} ${u.label} ${u.incident_title ?? ""}`.toLowerCase().includes(q.toLowerCase())))
    return rows
  }, [units.data, kind, q])
  const kinds = useMemo(() => {
    const m = new Map<string, number>()
    for (const u of units.data ?? []) m.set(u.kind, (m.get(u.kind) ?? 0) + 1)
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  }, [units.data])
  const sel = (units.data ?? []).find((u) => u.id === selected) ?? null
  useEffect(() => {
    if (!selected && list.length) setSelected((list.find((u) => u.assignment_id) ?? list[0]).id)
  }, [list, selected])

  const moving = (units.data ?? []).filter((u) => u.status === "en_route").length
  const onSite = (units.data ?? []).filter((u) => u.status === "on_site").length
  const free = (units.data ?? []).filter((u) => u.status === "available").length
  const rerouted = (units.data ?? []).reduce((n, u) => n + (u.reroutes || 0), 0)

  return (
    <div className="flex flex-col gap-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-sm text-muted-foreground">
            Where every unit is, where it has been, why it changed course, and who is working each incident.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" className="gap-1.5"
                  onClick={() => download("/units/log/export?format=csv", "units-log.csv")}>
            <Download className="size-4" /> All logs (CSV)
          </Button>
          <Button variant="outline" size="sm" className="gap-1.5"
                  onClick={() => download("/units/log/export?format=json", "units-log.json")}>
            <FileJson className="size-4" /> JSON
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat icon={Navigation} label="Moving" value={moving} tone="text-sky-500" />
        <Stat icon={MapPin} label="On scene" value={onSite} tone="text-violet-500" />
        <Stat icon={Activity} label="Free" value={free} tone="text-emerald-500" />
        <Stat icon={Route} label="Reroutes logged" value={rerouted} tone="text-amber-500" />
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="units">Units</TabsTrigger>
          <TabsTrigger value="teams">Teams on incidents</TabsTrigger>
          <TabsTrigger value="sites">Sites & stock</TabsTrigger>
        </TabsList>

        <TabsContent value="units" className="mt-3">
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
            <Card className="min-w-0">
              <CardHeader className="gap-3 pb-3">
                <div className="flex flex-wrap items-center gap-2">
                  <div className="relative">
                    <Search className="absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
                    <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Unit, label or incident"
                           className="h-9 w-56 pl-8" />
                  </div>
                  <Button size="sm" variant={kind ? "outline" : "default"} onClick={() => setKind(null)}>All</Button>
                  {kinds.map(([k, n]) => (
                    <Button key={k} size="sm" variant={kind === k ? "default" : "outline"} onClick={() => setKind(k)}>
                      {pretty(k)} <span className="ml-1 text-xs opacity-70">{n}</span>
                    </Button>
                  ))}
                </div>
              </CardHeader>
              <CardContent className="p-0">
                <ScrollArea className="h-[560px]">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Unit</TableHead><TableHead>Status</TableHead>
                        <TableHead>Doing</TableHead><TableHead className="w-28">Progress</TableHead>
                        <TableHead className="text-right">Seen</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {list.map((u) => {
                        const Icon = KIND_ICON[u.kind] ?? Truck
                        return (
                          <TableRow key={u.id} onClick={() => setSelected(u.id)}
                                    className={`cursor-pointer ${selected === u.id ? "bg-muted/70" : ""}`}>
                            <TableCell>
                              <div className="flex items-center gap-2">
                                <Icon className="size-4 text-muted-foreground" />
                                <div className="min-w-0">
                                  <div className="truncate font-medium">{u.label}</div>
                                  <div className="text-xs text-muted-foreground">{u.id}</div>
                                </div>
                              </div>
                            </TableCell>
                            <TableCell>
                              <Badge className={STATUS_TONE[u.status] ?? ""}>{pretty(u.status)}</Badge>
                              {u.reroutes > 0 && <span className="ml-1 text-xs text-amber-600">↻{u.reroutes}</span>}
                            </TableCell>
                            <TableCell className="max-w-56">
                              <div className="truncate text-sm">{u.incident_title ?? u.unavailable_reason ?? "—"}</div>
                              {u.capability_id && <div className="text-xs text-muted-foreground">{pretty(u.capability_id)}</div>}
                            </TableCell>
                            <TableCell>
                              {u.progress != null ? (
                                <div className="flex items-center gap-2">
                                  <Progress value={Math.round(u.progress * 100)} className="h-1.5" />
                                  <span className="text-xs tabular-nums" title={u.eta_model ? `model: ${u.eta_model.p50} min, 90% within ${u.eta_model.p90}; router said ${u.eta_minutes ?? "?"}` : "router estimate"}>{u.eta_model ? `${Math.round(u.eta_model.p50)}′ (≤${Math.round(u.eta_model.p90)}′)` : `${u.eta_minutes ?? "?"}′`}</span>
                                </div>
                              ) : <span className="text-xs text-muted-foreground">—</span>}
                            </TableCell>
                            <TableCell className="text-right text-xs text-muted-foreground">{ago(u.last_reported_at)}</TableCell>
                          </TableRow>
                        )
                      })}
                    </TableBody>
                  </Table>
                </ScrollArea>
              </CardContent>
            </Card>
            {sel ? <UnitDetail unit={sel} /> : <Card><CardContent className="p-6 text-sm text-muted-foreground">Pick a unit.</CardContent></Card>}
          </div>
        </TabsContent>

        <TabsContent value="sites" className="mt-3">
          <SitesBoard region={region === "ncr" ? "ncr" : "pune"} />
        </TabsContent>

        <TabsContent value="teams" className="mt-3">
          <TeamsBoard teams={(teams.data ?? []).filter((t) => region === "all" || (region === "ncr") === t.wardId?.startsWith("w-gzb"))} onPick={(id) => { setSelected(id); setTab("units") }} />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function Stat({ icon, label, value }: { icon: typeof Truck; label: string; value: number; tone?: string }) {
  const series = useSeries(`units:${label}`, value)
  return <StatCard icon={icon} label={label} value={value} series={series} />
}

function UnitDetail({ unit }: { unit: Unit }) {
  const [withPositions, setWithPositions] = useState(false)
  const log = useQuery({
    queryKey: ["unit-log", unit.id, withPositions], refetchInterval: 6000,
    queryFn: () => request<{ rows: LogRow[] }>(`/units/${encodeURIComponent(unit.id)}/log`,
      { toast: false, query: { positions: withPositions, limit: 400 } }),
  })
  const track = useQuery({
    queryKey: ["unit-track", unit.id], refetchInterval: 5000,
    queryFn: () => request<Track>(`/units/${encodeURIComponent(unit.id)}/track`, { toast: false, query: { limit: 600 } }),
  })
  const rows = log.data?.rows ?? []
  const reroutes = rows.filter((r) => r.kind === "assignment.rerouted")

  return (
    <Card className="min-w-0">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="text-base">{unit.label}</CardTitle>
            <CardDescription>
              {unit.id} · {pretty(unit.kind)} · {pretty(unit.status)}
              {unit.incident_title ? ` · to “${unit.incident_title}”` : ""}
            </CardDescription>
          </div>
          <div className="flex gap-1.5">
            <Button size="sm" variant="outline" className="gap-1.5"
                    onClick={() => download(`/units/${encodeURIComponent(unit.id)}/log?format=csv&limit=20000`, `${unit.id}-log.csv`)}>
              <Download className="size-4" /> CSV
            </Button>
            <Button size="sm" variant="outline" className="gap-1.5"
                    onClick={() => download(`/units/${encodeURIComponent(unit.id)}/log?format=json&limit=20000`, `${unit.id}-log.json`)}>
              <FileJson className="size-4" /> JSON
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <TrackMap track={track.data} unit={unit} />
        {reroutes.length > 0 && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-sm">
            <div className="flex items-center gap-1.5 font-medium text-amber-700 dark:text-amber-400">
              <AlertTriangle className="size-4" /> Last reroute, {ago(reroutes[0].at)}
            </div>
            <div className="mt-0.5">{reroutes[0].summary}</div>
          </div>
        )}
        <div className="flex items-center justify-between">
          <div className="text-sm font-medium">Log ({rows.length})</div>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input type="checkbox" checked={withPositions} onChange={(e) => setWithPositions(e.target.checked)} />
            include positions
          </label>
        </div>
        <ScrollArea className="h-[260px] pr-2">
          <ol className="relative ml-2 border-l pl-4">
            {rows.map((r, i) => (
              <li key={i} className="mb-3">
                <span className={`absolute -left-[5px] mt-1.5 size-2.5 rounded-full ${
                  r.kind === "assignment.rerouted" ? "bg-amber-500"
                    : r.type === "field_report" ? "bg-violet-500"
                      : r.type === "position" ? "bg-sky-400" : "bg-foreground/60"}`} />
                <div className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
                  <span className="tabular-nums">{new Date(r.at).toLocaleString()}</span>
                  <span className="font-medium text-foreground">{pretty(r.kind)}</span>
                  {r.sector && <Badge variant="outline" className="h-4 px-1 text-[10px]">{r.sector}</Badge>}
                  {r.hazard && <Badge variant="outline" className="h-4 px-1 text-[10px]">{r.hazard}</Badge>}
                </div>
                <div className="text-sm">{r.summary}</div>
              </li>
            ))}
            {!rows.length && <li className="text-sm text-muted-foreground">Nothing logged for this unit yet.</li>}
          </ol>
        </ScrollArea>
      </CardContent>
    </Card>
  )
}

function TrackMap({ track, unit }: { track?: Track; unit: Unit }) {
  const ref = useRef<HTMLDivElement | null>(null)
  const map = useRef<mapboxgl.Map | null>(null)
  const marker = useRef<mapboxgl.Marker | null>(null)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    if (!ref.current || map.current || !TOKEN) return
    mapboxgl.accessToken = TOKEN
    const dark = document.documentElement.classList.contains("dark")
    const m = new mapboxgl.Map({
      container: ref.current, style: dark ? "mapbox://styles/mapbox/dark-v11" : "mapbox://styles/mapbox/light-v11",
      center: [unit.lng ?? 73.79, unit.lat ?? 18.63], zoom: 13, attributionControl: false,
    })
    m.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right")
    m.on("load", () => {
      m.addSource("trail", { type: "geojson", data: { type: "FeatureCollection", features: [] } })
      m.addSource("route", { type: "geojson", data: { type: "FeatureCollection", features: [] } })
      m.addLayer({ id: "route", type: "line", source: "route",
                   paint: { "line-color": "#f59e0b", "line-width": 4, "line-dasharray": [2, 1.5], "line-opacity": 0.9 } })
      m.addLayer({ id: "trail", type: "line", source: "trail",
                   paint: { "line-color": "#0ea5e9", "line-width": 4, "line-opacity": 0.85 } })
      setReady(true)
    })
    map.current = m
    return () => { m.remove(); map.current = null; setReady(false) }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const m = map.current
    if (!m || !ready || !track) return
    const fc = (g: GeoJSON.LineString | null) => ({
      type: "FeatureCollection" as const,
      features: g && g.coordinates.length > 1 ? [{ type: "Feature" as const, properties: {}, geometry: g }] : [],
    })
    ;(m.getSource("trail") as mapboxgl.GeoJSONSource).setData(fc(track.trail))
    ;(m.getSource("route") as mapboxgl.GeoJSONSource).setData(fc(track.route))
    if (unit.lng != null && unit.lat != null) {
      if (!marker.current) {
        const el = document.createElement("div")
        el.className = "size-4 rounded-full border-2 border-white bg-sky-600 shadow-lg"
        marker.current = new mapboxgl.Marker({ element: el }).setLngLat([unit.lng, unit.lat]).addTo(m)
      } else marker.current.setLngLat([unit.lng, unit.lat])
    }
  }, [track, ready, unit.lng, unit.lat])

  useEffect(() => {
    // new unit picked: frame it
    const m = map.current
    if (!m || !ready) return
    const pts = [...(track?.trail.coordinates ?? []), ...(track?.route?.coordinates ?? [])] as [number, number][]
    if (pts.length > 1) {
      const b = pts.reduce((bb, p) => bb.extend(p), new mapboxgl.LngLatBounds(pts[0], pts[0]))
      m.fitBounds(b, { padding: 40, duration: 600, maxZoom: 15 })
    } else if (unit.lng != null && unit.lat != null) m.easeTo({ center: [unit.lng, unit.lat], zoom: 14 })
  }, [unit.id, ready]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!TOKEN) return <div className="rounded-md border p-4 text-sm text-muted-foreground">Set VITE_MAPBOX_TOKEN to see the map.</div>
  return (
    <div className="relative">
      <div ref={ref} className="h-[260px] w-full overflow-hidden rounded-md border" />
      <div className="pointer-events-none absolute bottom-2 left-2 flex gap-2 rounded bg-background/85 px-2 py-1 text-[11px] shadow">
        <span className="flex items-center gap-1"><span className="h-1 w-4 rounded bg-sky-500" /> driven</span>
        <span className="flex items-center gap-1"><span className="h-1 w-4 rounded bg-amber-500" /> road ahead</span>
        {track?.etaMinutes != null && <span>· {track.etaMinutes} min</span>}
      </div>
    </div>
  )
}

function TeamsBoard({ teams, onPick }: { teams: Team[]; onPick: (unit: string) => void }) {
  if (!teams.length) return <Card><CardContent className="p-6 text-sm text-muted-foreground">No open incidents.</CardContent></Card>
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {teams.map((t) => (
        <Card key={t.incidentId} className={t.complete ? "" : "border-amber-500/50"}>
          <CardHeader className="pb-2">
            <div className="flex items-start justify-between gap-2">
              <CardTitle className="text-sm leading-snug">{t.title}</CardTitle>
              {t.severity != null && <Badge variant="outline">S{t.severity}</Badge>}
            </div>
            <CardDescription>{pretty(t.category)} · {t.wardId}</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            <div className="flex flex-wrap gap-1.5">
              {t.needs.map((n) => (
                <Badge key={n.capability} variant={n.met >= n.required ? "default" : "destructive"} className="gap-1">
                  {pretty(n.capability)} {n.met}/{n.required}
                </Badge>
              ))}
            </div>
            <div className="flex flex-col gap-1">
              {t.units.map((u) => {
                const Icon = KIND_ICON[u.kind] ?? Truck
                return (
                  <button key={u.unit} onClick={() => onPick(u.unit)}
                          className="flex items-center justify-between rounded px-1.5 py-1 text-left hover:bg-muted">
                    <span className="flex items-center gap-1.5"><Icon className="size-4 text-muted-foreground" />{u.label}</span>
                    <span className="text-xs text-muted-foreground">
                      {pretty(u.capability)} · {pretty(u.status)}{u.eta != null ? ` · ${u.eta}′` : ""}
                    </span>
                  </button>
                )
              })}
              {!t.units.length && <span className="text-xs text-muted-foreground">Nobody on the way yet.</span>}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  )
}

type Site = { id: string; name: string; kind: string; status: string; capacity: number | null; occupancy: number
  supplies: Record<string, number>; supplies_baseline: Record<string, number>; ward_id: string; changes: number }
type SiteRow = { site: string; at: string; summary: string }

/** Resource sites: every change to occupancy, stock, status and capacity, as
 *  logged by the database itself (migration 036), viewable and exportable. */
function SitesBoard({ region }: { region: "pune" | "ncr" }) {
  const [pick, setPick] = useState<string | null>(null)
  const sites = useQuery({
    queryKey: ["sites", region], refetchInterval: 8000,
    queryFn: () => request<Site[]>("/units/sites", { toast: false, query: { region } }),
  })
  const log = useQuery({
    queryKey: ["site-log", pick], enabled: !!pick, refetchInterval: 6000,
    queryFn: () => request<{ rows: SiteRow[] }>(`/units/sites/${encodeURIComponent(pick!)}/log`, { toast: false }),
  })
  const sel = sites.data?.find((s) => s.id === pick)
  return (
    <div className="grid gap-5 xl:grid-cols-[1.3fr_1fr]">
      <Card>
        <CardHeader className="flex-row items-center justify-between pb-2">
          <div><CardTitle className="text-base">Sites</CardTitle>
            <CardDescription>Shelters, relief centres, kitchens, water points, camps, hospitals</CardDescription></div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" className="gap-1.5" onClick={() => download(`/units/sites/log/export?format=csv&region=${region}`, "sites-log.csv")}><Download className="size-3.5" /> CSV</Button>
            <Button size="sm" variant="outline" className="gap-1.5" onClick={() => download(`/units/sites/log/export?format=json&region=${region}`, "sites-log.json")}><FileJson className="size-3.5" /> JSON</Button>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          <ScrollArea className="h-[560px]">
            <Table>
              <TableHeader><TableRow><TableHead>Site</TableHead><TableHead>Status</TableHead><TableHead>People</TableHead><TableHead>Lowest stock</TableHead><TableHead>Changes</TableHead></TableRow></TableHeader>
              <TableBody>
                {(sites.data ?? []).map((s) => {
                  const low = Object.entries(s.supplies ?? {}).map(([k, v]) => [k, v / Math.max(1, s.supplies_baseline?.[k] ?? v ?? 1)] as const)
                    .sort((a, b) => a[1] - b[1])[0]
                  return (
                    <TableRow key={s.id} className={`cursor-pointer ${pick === s.id ? "bg-muted" : ""}`} onClick={() => setPick(s.id)}>
                      <TableCell><div className="font-medium">{s.name}</div><div className="text-xs text-muted-foreground">{s.kind.replace(/_/g, " ")}</div></TableCell>
                      <TableCell><Badge variant={s.status === "full" ? "destructive" : s.status === "closed" ? "outline" : "secondary"}>{s.status}</Badge></TableCell>
                      <TableCell className="tabular-nums">{s.capacity ? `${s.occupancy}/${s.capacity}` : "—"}</TableCell>
                      <TableCell className="text-xs">{low ? `${low[0].replace(/_/g, " ")} ${Math.round(low[1] * 100)}%` : "—"}</TableCell>
                      <TableCell className="tabular-nums">{s.changes}</TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </ScrollArea>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="flex-row items-center justify-between pb-2">
          <div><CardTitle className="text-base">{sel ? sel.name : "Pick a site"}</CardTitle>
            <CardDescription>Every change, newest first</CardDescription></div>
          {sel && (
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={() => download(`/units/sites/${encodeURIComponent(sel.id)}/log?format=csv&limit=20000`, `${sel.id}-log.csv`)}><Download className="size-3.5" /></Button>
              <Button size="sm" variant="outline" onClick={() => download(`/units/sites/${encodeURIComponent(sel.id)}/log?format=json&limit=20000`, `${sel.id}-log.json`)}><FileJson className="size-3.5" /></Button>
            </div>
          )}
        </CardHeader>
        <CardContent>
          <ScrollArea className="h-[520px] pr-2">
            <ol className="relative ml-2 border-l pl-4">
              {(log.data?.rows ?? []).map((r, i) => (
                <li key={i} className="mb-2.5">
                  <span className="absolute -left-[5px] mt-1.5 size-2.5 rounded-full bg-primary/70" />
                  <div className="text-xs text-muted-foreground">{new Date(r.at).toLocaleString()}</div>
                  <div className="text-sm">{r.summary}</div>
                </li>
              ))}
              {pick && !(log.data?.rows ?? []).length && <li className="text-sm text-muted-foreground">No changes logged yet (needs migration 036).</li>}
            </ol>
          </ScrollArea>
        </CardContent>
      </Card>
    </div>
  )
}
