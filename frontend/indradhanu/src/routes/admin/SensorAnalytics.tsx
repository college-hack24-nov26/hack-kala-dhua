import { useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import mapboxgl from "mapbox-gl"
import "mapbox-gl/dist/mapbox-gl.css"
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts"
import { Activity, Flame, HardHat, Plus, Radio, Siren, UserRound } from "lucide-react"
import { toast } from "sonner"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  deployNode, fetchFleet, fetchOverview, fetchSeries, KIND_STYLE, setFleet, usePoll,
  type Evidence, type FleetStatus, type NodeKind, type SensorEvent, type SensorNode, type SeriesPoint,
} from "./iotApi"
import { regionOf } from "./zones"
import { useRegion } from "@/lib/region"

/** Live LoRa field telemetry.
 *
 *  Map: one heat layer over the field, switchable between the derived scores
 *  (overall risk, human presence, structural movement, gas/heat) and the raw
 *  channels. Click a node for its readings, then the charts below are that
 *  node's history: raw channels, human-presence evidence, structure, and the
 *  risk timeline with the moments it escalated into the incident pipeline.
 *
 *  The scores are the API's, never recomputed here, so this page, the agent
 *  and the incident queue always agree on what a reading meant.
 */

const TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined

type MetricKey =
  | "overall" | "human" | "structural" | "environmental"
  | "mq2" | "mq135" | "temp_c" | "mic" | "piezo" | "tilt_deg"

/** How a metric maps onto 0..1 for colour. Scores already are; raw channels
 *  get a fixed, labelled range so the legend can say what red means. */
const METRICS: { key: MetricKey; label: string; lo: number; hi: number; unit: string }[] = [
  { key: "overall", label: "Overall risk", lo: 0, hi: 1, unit: "%" },
  { key: "human", label: "Human presence", lo: 0, hi: 1, unit: "%" },
  { key: "structural", label: "Structural movement", lo: 0, hi: 1, unit: "%" },
  { key: "environmental", label: "Gas / heat", lo: 0, hi: 1, unit: "%" },
  { key: "mq2", label: "MQ-2", lo: 100, hi: 700, unit: "" },
  { key: "mq135", label: "MQ-135", lo: 150, hi: 800, unit: "" },
  { key: "temp_c", label: "Temperature", lo: 20, hi: 60, unit: "°C" },
  { key: "mic", label: "Sound", lo: 0, hi: 400, unit: "" },
  { key: "piezo", label: "Piezo / knocks", lo: 0, hi: 600, unit: "" },
  { key: "tilt_deg", label: "Tilt", lo: 0, hi: 20, unit: "°" },
]

const RAW_SERIES: { key: keyof SeriesPoint; label: string; color: string; unit: string }[] = [
  { key: "mq2", label: "MQ-2", color: "#ec835a", unit: "" },
  { key: "mq135", label: "MQ-135", color: "#fab219", unit: "" },
  { key: "temp_c", label: "Temperature", color: "#d03b3b", unit: "°C" },
  { key: "mic", label: "Sound", color: "#2a78d6", unit: "" },
  { key: "piezo", label: "Piezo", color: "#8b5cf6", unit: "" },
  { key: "tilt_deg", label: "Tilt", color: "#0ca30c", unit: "°" },
  { key: "gyro_dps", label: "Gyro", color: "#14b8a6", unit: "°/s" },
]

const C = { overall: "#d03b3b", human: "#2a78d6", structural: "#ec835a", environmental: "#fab219" }
const EVIDENCE_LABEL: Record<string, string> = {
  audio: "Voices / sound", tapping: "Tapping on rubble", warmth: "Warmth", breath: "CO₂ rise",
  pir: "PIR motion", lean: "Lean since install", tilt_switch: "Tilt switch", shock: "Shock",
  shaking: "Shaking", gas_mq2: "MQ-2 rise", gas_mq135: "MQ-135 rise", heat: "Heat",
}
const FLAG_LABEL: Record<string, string> = {
  gas_mq2: "MQ-2 gas", gas_mq135: "MQ-135 air", heat: "Heat", sound: "Sound",
  tapping: "Tapping", tilt_shift: "Tilt shift", tilt_switch: "Tilt switch", shock: "Shock",
  shaking: "Shaking", packet_loss: "Packets lost",
}
const WINDOWS = [{ m: 15, l: "15 min" }, { m: 60, l: "1 h" }, { m: 360, l: "6 h" }]

const axis = { fontSize: 11, fill: "var(--muted-foreground)" }
const tip = {
  contentStyle: {
    background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 8,
    fontSize: 12, color: "var(--popover-foreground)",
  },
}

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`)
const hhmmss = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"
const ago = (s: number | null | undefined) =>
  s == null ? "—" : s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`

function level(v: number | null | undefined) {
  const x = v ?? 0
  if (x >= 0.7) return { label: "HIGH", cls: "text-red-500", dot: "bg-red-500" }
  if (x >= 0.4) return { label: "MEDIUM", cls: "text-amber-500", dot: "bg-amber-500" }
  if (x >= 0.15) return { label: "LOW", cls: "text-emerald-500", dot: "bg-emerald-500" }
  return { label: "NONE", cls: "text-muted-foreground", dot: "bg-zinc-400" }
}

function norm(node: SensorNode, m: (typeof METRICS)[number]): number | null {
  const v = node[m.key] as number | null
  if (v == null) return null
  return Math.max(0, Math.min(1, (v - m.lo) / (m.hi - m.lo)))
}

function fmt(v: number | null | undefined, m: { unit: string }, digits = 0) {
  if (v == null) return "—"
  if (m.unit === "%") return pct(v)
  return `${v.toFixed(digits)}${m.unit}`
}

// ------------------------------------------------------------------- map ---
function FieldMap({ nodes, metric, selected, onSelect }: {
  nodes: SensorNode[]
  metric: (typeof METRICS)[number]
  selected: string | null
  onSelect: (id: string) => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const map = useRef<mapboxgl.Map | null>(null)
  const ready = useRef(false)
  const fitted = useRef(false)
  const select = useRef(onSelect)
  select.current = onSelect
  const [failed, setFailed] = useState<string | null>(TOKEN ? null : "VITE_MAPBOX_TOKEN is not set")
  const [tick, setTick] = useState(0)

  const geo = useMemo<GeoJSON.FeatureCollection>(() => ({
    type: "FeatureCollection",
    features: nodes.filter((n) => n.lat != null && n.lon != null).map((n) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [n.lon as number, n.lat as number] },
      properties: {
        id: n.id, w: norm(n, metric) ?? 0, online: n.online ? 1 : 0,
        sel: n.id === selected ? 1 : 0, label: n.id,
        ring: KIND_STYLE[n.kind]?.color ?? "#ffffff", fresh: n.is_new && n.online ? 1 : 0,
      },
    })),
  }), [nodes, metric, selected])

  useEffect(() => {
    if (!box.current || map.current || !TOKEN) return
    mapboxgl.accessToken = TOKEN
    const dark = document.documentElement.classList.contains("dark")
    let m: mapboxgl.Map
    try {
      m = new mapboxgl.Map({
        container: box.current,
        style: dark ? "mapbox://styles/mapbox/dark-v11" : "mapbox://styles/mapbox/light-v11",
        center: [73.8567, 18.5204], zoom: 14, attributionControl: false,
        fadeDuration: 0, projection: "mercator",
      } as mapboxgl.MapOptions)
    } catch (e) {
      setFailed(e instanceof Error ? e.message : "Map failed to start")
      return
    }
    map.current = m
    // Re-measure whenever the card changes size (sidebar toggle, window resize,
    // the grid switching to two columns); Mapbox only measures at start.
    const ro = new ResizeObserver(() => m.resize())
    ro.observe(box.current)
    m.once("remove", () => ro.disconnect())
    m.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right")
    m.on("load", () => {
      m.addSource("nodes", { type: "geojson", data: { type: "FeatureCollection", features: [] } })
      m.addLayer({
        id: "heat", type: "heatmap", source: "nodes",
        paint: {
          "heatmap-weight": ["max", 0.05, ["get", "w"]],
          "heatmap-intensity": 1.1,
          "heatmap-radius": ["interpolate", ["exponential", 2], ["zoom"], 10, 18, 14, 70, 17, 260],
          "heatmap-opacity": 0.75,
          "heatmap-color": [
            "interpolate", ["linear"], ["heatmap-density"],
            0, "rgba(0,0,0,0)", 0.15, "rgba(12,163,12,0.55)", 0.4, "#fab219",
            0.65, "#ec835a", 0.9, "#d03b3b",
          ],
        },
      })
      // A soft halo behind nodes that joined in the last few minutes.
      m.addLayer({
        id: "fresh", type: "circle", source: "nodes", filter: ["==", ["get", "fresh"], 1],
        paint: { "circle-radius": 20, "circle-color": "#22c55e", "circle-opacity": 0.25,
          "circle-stroke-width": 2, "circle-stroke-color": "#22c55e", "circle-stroke-opacity": 0.8 },
      })
      m.addLayer({
        id: "dots", type: "circle", source: "nodes",
        paint: {
          "circle-radius": ["case", ["==", ["get", "sel"], 1], 11, 8],
          "circle-color": [
            "interpolate", ["linear"], ["get", "w"],
            0, "#0ca30c", 0.4, "#fab219", 0.7, "#ec835a", 0.9, "#d03b3b",
          ],
          "circle-opacity": ["case", ["==", ["get", "online"], 1], 1, 0.35],
          "circle-stroke-width": ["case", ["==", ["get", "sel"], 1], 4, 3],
          "circle-stroke-color": ["get", "ring"],
        },
      })
      m.addLayer({
        id: "labels", type: "symbol", source: "nodes",
        layout: {
          "text-field": ["get", "label"], "text-size": 11, "text-offset": [0, 1.5],
          "text-anchor": "top", "text-allow-overlap": true,
        },
        paint: { "text-color": dark ? "#e6edf7" : "#0f172a", "text-halo-color": dark ? "#0b1220" : "#ffffff", "text-halo-width": 1.2 },
      })
      m.on("click", "dots", (e) => {
        const id = e.features?.[0]?.properties?.id
        if (id) select.current(String(id))
      })
      m.on("mouseenter", "dots", () => { m.getCanvas().style.cursor = "pointer" })
      m.on("mouseleave", "dots", () => { m.getCanvas().style.cursor = "" })
      ready.current = true
      setTick((t) => t + 1)
    })
    m.on("error", (e) => {
      const err = (e as unknown as { error?: { status?: number } })?.error
      if (err?.status === 401) setFailed("Mapbox rejected the token (401).")
    })
    return () => {
      m.remove()
      map.current = null
      ready.current = false
    }
  }, [])

  useEffect(() => {
    const m = map.current
    if (!m || !ready.current) return
    ;(m.getSource("nodes") as mapboxgl.GeoJSONSource | undefined)?.setData(geo)
    if (!fitted.current && geo.features.length) {
      fitted.current = true
      const pts = geo.features.map((f) => (f.geometry as GeoJSON.Point).coordinates as [number, number])
      if (pts.length === 1) m.jumpTo({ center: pts[0], zoom: 16 })
      else {
        const b = new mapboxgl.LngLatBounds(pts[0], pts[0])
        pts.forEach((p) => b.extend(p))
        m.fitBounds(b, { padding: 80, maxZoom: 17, duration: 0 })
      }
    }
  }, [geo, tick])

  return (
    <div className="relative h-[460px] w-full overflow-hidden rounded-lg border">
      {/* Sized explicitly, not with absolute + inset-0: mapbox-gl.css sets
          .mapboxgl-map { position: relative } on this element, which beats the
          utility class, and the container then collapses to 0 px tall. */}
      <div ref={box} className="h-full w-full" />
      {failed && (
        <div className="bg-muted/80 absolute inset-0 grid place-items-center p-6 text-center text-sm">{failed}</div>
      )}
      <div className="bg-background/90 absolute bottom-3 left-3 rounded-md border px-3 py-2 text-xs shadow-sm">
        <div className="mb-1 font-medium">{metric.label}</div>
        <div className="h-2 w-44 rounded-full" style={{ background: "linear-gradient(90deg,#0ca30c,#fab219,#ec835a,#d03b3b)" }} />
        <div className="text-muted-foreground mt-1 flex justify-between">
          <span>{metric.unit === "%" ? "0%" : `${metric.lo}${metric.unit}`}</span>
          <span>{metric.unit === "%" ? "100%" : `${metric.hi}${metric.unit}`}</span>
        </div>
        <div className="mt-2 flex flex-wrap gap-x-2.5 gap-y-1">
          {(Object.keys(KIND_STYLE) as NodeKind[]).map((k) => (
            <span key={k} className="flex items-center gap-1">
              <span className="size-2.5 rounded-full border-2" style={{ borderColor: KIND_STYLE[k].color }} />
              {KIND_STYLE[k].short}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

// ------------------------------------------------------------ node panel ---
function ScoreBar({ label, v, color }: { label: string; v: number | null; color: string }) {
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium tabular-nums">{pct(v)}</span>
      </div>
      <div className="bg-muted h-1.5 overflow-hidden rounded-full">
        <div className="h-full rounded-full" style={{ width: `${Math.round((v ?? 0) * 100)}%`, background: color }} />
      </div>
    </div>
  )
}

function NodePanel({ n }: { n: SensorNode | null }) {
  if (!n) {
    return (
      <Card className="h-full">
        <CardHeader><CardTitle className="text-base">No node selected</CardTitle>
          <CardDescription>Click a node on the map.</CardDescription></CardHeader>
      </Card>
    )
  }
  const all: [string, number | null, number | undefined, string, number, string][] = [
    ["MQ-2", n.mq2, n.baseline.mq2, "", 0, "mq2"],
    ["MQ-135", n.mq135, n.baseline.mq135, "", 0, "mq135"],
    ["Temperature", n.temp_c, n.baseline.temp_c, "°C", 1, "temp_c"],
    ["Tilt", n.tilt_deg, n.baseline.tilt_deg, "°", 1, "tilt_deg"],
    ["Gyro peak", n.gyro_dps, undefined, "°/s", 1, "gyro_dps"],
    ["Sound", n.mic, n.baseline.mic, "", 0, "mic"],
    ["Piezo", n.piezo, n.baseline.piezo, "", 0, "piezo"],
    ["Knocks", n.knocks, undefined, "", 0, "knocks"],
  ]
  // Only the channels this kind of node actually has.
  const raw = all.filter((r) => !n.channels?.length || n.channels.includes(r[5]) || r[1] != null)
  const flags = (n.flags ?? []).filter((f) => f !== "learning" && !f.startsWith("escalated:") && !f.startsWith("simulated"))
  return (
    <Card className="h-full">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base">{n.id}</CardTitle>
          <span className={`flex items-center gap-1.5 text-xs ${n.online ? "text-emerald-500" : "text-muted-foreground"}`}>
            <span className={`size-2 rounded-full ${n.online ? "bg-emerald-500" : "bg-zinc-400"}`} />
            {n.online ? "ONLINE" : "OFFLINE"}
          </span>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant="outline" style={{ borderColor: KIND_STYLE[n.kind]?.color, color: KIND_STYLE[n.kind]?.color }}>
            {n.kind_label}
          </Badge>
          {n.simulated ? <Badge variant="secondary">simulated</Badge> : <Badge>real hardware</Badge>}
          {n.is_new && <Badge className="bg-emerald-600 hover:bg-emerald-600">new</Badge>}
        </div>
        {n.label && n.label !== n.id && <p className="text-sm">{n.label}</p>}
        <CardDescription>
          {n.kind_detail} · last update {ago(n.age_s)} · RSSI {n.rssi ?? "—"} dBm · {n.lost} packets lost
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2.5">
          <ScoreBar label="Human likelihood" v={n.human} color={C.human} />
          <ScoreBar label="Structural risk" v={n.structural} color={C.structural} />
          <ScoreBar label="Environmental risk" v={n.environmental} color={C.environmental} />
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
          {raw.map(([label, v, b, unit, d]) => (
            <div key={label} className="flex justify-between gap-2">
              <span className="text-muted-foreground">{label}</span>
              <span className="tabular-nums">
                {v == null ? "—" : `${v.toFixed(d)}${unit}`}
                {b != null && v != null && <span className="text-muted-foreground"> /{b.toFixed(d)}</span>}
              </span>
            </div>
          ))}
        </div>
        <p className="text-muted-foreground text-[11px]">Small grey number = the node's learned quiet baseline.</p>
        {flags.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {flags.map((f) => (
              <Badge key={f} variant={f === "warming_up" ? "outline" : "secondary"}>
                {f === "warming_up" ? "gas sensors warming up" : FLAG_LABEL[f] ?? f}
              </Badge>
            ))}
          </div>
        )}
        {n.lat == null && (
          <p className="text-xs text-amber-600">No location yet: start the bridge with --loc {n.id}=lat,lon</p>
        )}
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------- charts ---
function Panel({ title, sub, children, right }: {
  title: string; sub?: string; children: ReactNode; right?: ReactNode
}) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-2 pb-2">
        <div>
          <CardTitle className="text-sm">{title}</CardTitle>
          {sub && <CardDescription className="text-xs">{sub}</CardDescription>}
        </div>
        {right}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

function Chips<T extends string>({ items, value, onChange }: {
  items: { key: T; label: string }[]; value: T; onChange: (k: T) => void
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {items.map((i) => (
        <button key={i.key} type="button" onClick={() => onChange(i.key)}
          className={`rounded-full border px-2.5 py-0.5 text-xs transition ${value === i.key
            ? "bg-primary text-primary-foreground border-primary" : "hover:bg-muted"}`}>
          {i.label}
        </button>
      ))}
    </div>
  )
}

function Empty({ text }: { text: string }) {
  return <div className="text-muted-foreground grid h-[200px] place-items-center text-sm">{text}</div>
}

function eventType(e: SensorEvent): { label: string; value: string; sev: number } {
  const f = e.flags.filter((x) => x !== "warming_up" && x !== "learning" && !x.startsWith("simulated"))
  const esc = f.find((x) => x.startsWith("escalated:"))
  if (esc) return { label: `Escalated: ${esc.split(":")[1]}`, value: pct(e.overall), sev: 3 }
  const order = ["tapping", "tilt_shift", "tilt_switch", "heat", "gas_mq2", "shock", "sound", "gas_mq135", "shaking", "packet_loss"]
  const top = order.find((x) => f.includes(x)) ?? f[0] ?? "reading"
  const value =
    top === "tapping" ? `${e.knocks ?? 0} knocks` :
    top === "tilt_shift" ? `${e.tilt_deg?.toFixed(1) ?? "—"}°` :
    top === "heat" ? `${e.temp_c?.toFixed(1) ?? "—"}°C` :
    top === "gas_mq2" ? `${Math.round(e.mq2 ?? 0)}` :
    top === "gas_mq135" ? `${Math.round(e.mq135 ?? 0)}` :
    top === "sound" ? `${Math.round(e.mic ?? 0)}` : pct(e.overall)
  const sev = (e.overall ?? 0) >= 0.7 ? 2 : (e.overall ?? 0) >= 0.4 ? 1 : 0
  return { label: FLAG_LABEL[top] ?? top, value, sev }
}

// ------------------------------------------------------------------ page ---
export default function SensorAnalytics() {
  const [metricKey, setMetricKey] = useState<MetricKey>("overall")
  const [selected, setSelected] = useState<string | null>(null)
  const [minutes, setMinutes] = useState(15)
  const [rawKey, setRawKey] = useState<string>("mq2")

  const { data, error } = usePoll(() => fetchOverview(60), 3000, [])
  const [regionPick] = useRegion()
  const [fleetBump, setFleetBump] = useState(0)
  const { data: fleet } = usePoll(() => fetchFleet().catch(() => null as FleetStatus | null), 10000, [fleetBump])

  // Announce a node the first time it is heard, after the first load.
  const seen = useRef<Set<string> | null>(null)
  useEffect(() => {
    if (!data) return
    if (seen.current === null) {
      seen.current = new Set(data.nodes.map((n) => n.id))
      return
    }
    for (const n of data.nodes) {
      if (seen.current.has(n.id)) continue
      seen.current.add(n.id)
      toast.success(`New sensor node joined: ${n.id}`, {
        description: `${n.kind_label}${n.simulated ? " (simulated)" : " (real hardware)"}${n.label && n.label !== n.id ? ` · ${n.label}` : ""}`,
        action: { label: "Show", onClick: () => setSelected(n.id) },
      })
    }
  }, [data])
  // Same region scoping as the rest of the console; a node with no location yet
  // is shown everywhere so it can be found and placed.
  const nodes = useMemo(() => (data?.nodes ?? []).filter((n) =>
    regionPick === "all" || n.lat == null || n.lon == null || regionOf([n.lon, n.lat])?.id === regionPick), [data, regionPick])

  useEffect(() => {
    if (!selected && nodes.length) {
      const top = [...nodes].sort((a, b) => (b.overall ?? 0) - (a.overall ?? 0))[0]
      setSelected(top.id)
    }
  }, [nodes, selected])

  const { data: series } = usePoll(
    selected ? () => fetchSeries(selected, minutes) : null, 5000, [selected, minutes],
  )
  const points = useMemo(
    () => (series?.node === selected ? series.points : []).map((p) => ({ ...p, t: hhmmss(p.observed_at) })),
    [series, selected],
  )
  const escalations = useMemo(
    () => points.filter((p) => (p.flags ?? []).some((f) => f.startsWith("escalated:")))
      .map((p) => ({ t: p.t, what: (p.flags ?? []).find((f) => f.startsWith("escalated:"))!.split(":")[1] })),
    [points],
  )
  const structuralMarks = useMemo(
    () => points.filter((p) => (p.flags ?? []).some((f) => f === "tilt_shift" || f === "shock" || f === "tilt_switch")),
    [points],
  )

  const metric = METRICS.find((m) => m.key === metricKey)!
  const node = nodes.find((n) => n.id === selected) ?? null
  const raw = RAW_SERIES.find((r) => r.key === rawKey) ?? RAW_SERIES[0]
  const s = data?.summary
  const evidence: Evidence = series?.node === selected ? series?.evidence ?? {} : {}
  const humanBars = (["audio", "tapping", "warmth", "breath", "pir"] as const)
    .map((k) => ({ name: EVIDENCE_LABEL[k], value: Math.round((evidence[k] ?? 0) * 100) }))

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-muted-foreground text-sm">
            Live LoRa field telemetry · gas, heat, sound, tapping, tilt and shock from each node
          </p>
        </div>
        <span className="text-muted-foreground flex items-center gap-2 text-xs">
          <span className="relative inline-flex size-2">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-60" />
            <span className="relative inline-flex size-2 rounded-full bg-emerald-500" />
          </span>
          refreshing every 3 s
        </span>
      </div>

      <FleetBar fleet={fleet} region={regionPick === "all" ? "pune" : regionPick}
        onChange={() => setFleetBump((b) => b + 1)} />

      {error && <Card className="border-destructive/50 p-4 text-sm">Could not read sensor data: {error}</Card>}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <Kpi icon={<Radio className="size-4" />} label="Active nodes"
          value={s ? `${s.online}` : "—"}
          sub={s ? `${s.total} known · ${s.real ?? 0} real · ${s.simulated ?? 0} simulated` : ""} />
        <Kpi icon={<UserRound className="size-4" />} label="Human signal" v={s?.human?.value}
          sub={s?.human ? `strongest at ${s.human.node}` : "no live nodes"} />
        <Kpi icon={<HardHat className="size-4" />} label="Structural risk" v={s?.structural?.value}
          sub={s?.structural ? `highest at ${s.structural.node}` : "no live nodes"} />
        <Kpi icon={<Flame className="size-4" />} label="Gas / heat" v={s?.environmental?.value}
          sub={s?.environmental ? `highest at ${s.environmental.node}` : "no live nodes"} />
        <Kpi icon={<Siren className="size-4" />} label="Escalated (1 h)"
          value={s ? `${s.escalated_1h}` : "—"} sub="filed into incidents & response" />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <CardTitle className="text-base">Sensor field map</CardTitle>
              <CardDescription>Heat layer of the selected measure; click a node for detail</CardDescription>
            </div>
            <Chips items={METRICS} value={metricKey} onChange={setMetricKey} />
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid gap-5 xl:grid-cols-[1fr_340px]">
            {nodes.length ? (
              <FieldMap nodes={nodes} metric={metric} selected={selected} onSelect={setSelected} />
            ) : (
              <div className="text-muted-foreground grid h-[460px] place-items-center rounded-lg border p-6 text-center text-sm">
                No sensor node has reported yet in this region.<br />
                The simulated fleet starts with the API; switch it on above, or start the LoRa bridge.
              </div>
            )}
            <NodePanel n={node} />
          </div>
          {nodes.length > 1 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {nodes.map((n) => {
                const l = level(n.overall)
                return (
                  <button key={n.id} type="button" onClick={() => setSelected(n.id)}
                    className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs ${n.id === selected ? "border-primary" : ""}`}>
                    <span className={`size-2 rounded-full ${n.online ? l.dot : "bg-zinc-400"}`} />
                    <span className="size-2 rounded-full border-2" style={{ borderColor: KIND_STYLE[n.kind]?.color }}
                      title={n.kind_label} />
                    {n.id}{!n.simulated && <span className="font-semibold text-sky-600">·HW</span>}
                    {n.is_new && n.online && <span className="font-semibold text-emerald-600">NEW</span>}
                    <span className="text-muted-foreground">{fmt(n[metricKey] as number | null, metric, 1)}</span>
                  </button>
                )
              })}
            </div>
          )}
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-medium">
          History {selected ? <>for <span className="font-semibold">{selected}</span></> : ""}
        </h2>
        <Chips items={WINDOWS.map((w) => ({ key: String(w.m), label: w.l }))} value={String(minutes)}
          onChange={(k) => setMinutes(Number(k))} />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        <Panel title="Sensor readings over time" sub={`${raw.label}${raw.unit ? ` (${raw.unit})` : ""}`}
          right={<Chips items={RAW_SERIES.map((r) => ({ key: String(r.key), label: r.label }))} value={rawKey} onChange={setRawKey} />}>
          {points.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={points} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis dataKey="t" tick={axis} tickLine={false} axisLine={false} minTickGap={40} />
                <YAxis tick={axis} tickLine={false} axisLine={false} width={44} />
                <Tooltip {...tip} />
                {node?.baseline[raw.key as keyof typeof node.baseline] != null && (
                  <ReferenceLine y={node.baseline[raw.key as keyof typeof node.baseline]} stroke="var(--muted-foreground)"
                    strokeDasharray="4 4" label={{ value: "baseline", fontSize: 10, fill: "var(--muted-foreground)", position: "insideTopLeft" }} />
                )}
                <Line isAnimationActive={false} type="monotone" dataKey={raw.key as string} name={raw.label}
                  stroke={raw.color} strokeWidth={2} dot={false} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          ) : <Empty text="No readings in this window" />}
        </Panel>

        <Panel title="Human-presence evidence" sub="Likelihood over time, and what it is made of right now">
          {points.length ? (
            <div className="grid gap-3 md:grid-cols-[1fr_200px]">
              <ResponsiveContainer width="100%" height={220}>
                <AreaChart data={points} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                  <CartesianGrid vertical={false} stroke="var(--border)" />
                  <XAxis dataKey="t" tick={axis} tickLine={false} axisLine={false} minTickGap={40} />
                  <YAxis domain={[0, 1]} tickFormatter={(v: number) => `${Math.round(v * 100)}%`} tick={axis} tickLine={false} axisLine={false} width={44} />
                  <Tooltip {...tip} formatter={(v) => pct(Number(v))} />
                  <Area isAnimationActive={false} type="monotone" dataKey="human" name="human" stroke={C.human}
                    fill={C.human} fillOpacity={0.18} strokeWidth={2} connectNulls />
                </AreaChart>
              </ResponsiveContainer>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={humanBars} layout="vertical" margin={{ top: 8, right: 16, left: 8, bottom: 0 }}>
                  <XAxis type="number" domain={[0, 100]} hide />
                  <YAxis type="category" dataKey="name" tick={axis} tickLine={false} axisLine={false} width={100} />
                  <Tooltip {...tip} formatter={(v) => `${v}%`} />
                  <Bar isAnimationActive={false} dataKey="value" fill={C.human} radius={[0, 4, 4, 0]} maxBarSize={16} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : <Empty text="No readings in this window" />}
        </Panel>

        <Panel title="Structural movement" sub="Tilt from vertical and peak rotation; markers where movement was flagged">
          {points.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={points} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis dataKey="t" tick={axis} tickLine={false} axisLine={false} minTickGap={40} />
                <YAxis yAxisId="t" tick={axis} tickLine={false} axisLine={false} width={44} />
                <YAxis yAxisId="g" orientation="right" tick={axis} tickLine={false} axisLine={false} width={36} />
                <Tooltip {...tip} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {structuralMarks.slice(-8).map((p) => (
                  <ReferenceLine key={p.observed_at} yAxisId="t" x={p.t} stroke="#d03b3b" strokeOpacity={0.35} />
                ))}
                <Line yAxisId="t" isAnimationActive={false} type="monotone" dataKey="tilt_deg" name="tilt °"
                  stroke={C.structural} strokeWidth={2} dot={false} connectNulls />
                <Line yAxisId="g" isAnimationActive={false} type="monotone" dataKey="gyro_dps" name="gyro °/s"
                  stroke="#14b8a6" strokeWidth={1.5} dot={false} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          ) : <Empty text="No readings in this window" />}
        </Panel>

        <Panel title="Risk timeline" sub="Red lines: moments this node escalated into the incident pipeline">
          {points.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={points} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis dataKey="t" tick={axis} tickLine={false} axisLine={false} minTickGap={40} />
                <YAxis domain={[0, 1]} tickFormatter={(v: number) => `${Math.round(v * 100)}`} tick={axis} tickLine={false} axisLine={false} width={44} />
                <Tooltip {...tip} formatter={(v) => pct(Number(v))} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {escalations.map((e) => (
                  <ReferenceLine key={`${e.t}-${e.what}`} x={e.t} stroke="#d03b3b" strokeWidth={2}
                    label={{ value: e.what, fontSize: 10, fill: "#d03b3b", position: "insideTopRight" }} />
                ))}
                <Line isAnimationActive={false} type="monotone" dataKey="overall" name="overall" stroke={C.overall} strokeWidth={2.5} dot={false} connectNulls />
                <Line isAnimationActive={false} type="monotone" dataKey="human" name="human" stroke={C.human} strokeWidth={1.5} dot={false} connectNulls />
                <Line isAnimationActive={false} type="monotone" dataKey="structural" name="structural" stroke={C.structural} strokeWidth={1.5} dot={false} connectNulls />
                <Line isAnimationActive={false} type="monotone" dataKey="environmental" name="gas/heat" stroke={C.environmental} strokeWidth={1.5} dot={false} connectNulls />
              </LineChart>
            </ResponsiveContainer>
          ) : <Empty text="No readings in this window" />}
        </Panel>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm"><Activity className="size-4" /> Live sensor events</CardTitle>
          <CardDescription className="text-xs">Readings the API flagged in the last hour, newest first</CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto">
          {data?.events.length ? (
            <table className="w-full text-xs">
              <thead className="text-muted-foreground text-left">
                <tr className="border-b">
                  <th className="py-1.5 pr-3 font-medium">Time</th>
                  <th className="pr-3 font-medium">Node</th>
                  <th className="pr-3 font-medium">Type</th>
                  <th className="pr-3 font-medium">Value</th>
                  <th className="pr-3 font-medium">Human</th>
                  <th className="pr-3 font-medium">Structural</th>
                  <th className="pr-3 font-medium">Gas/heat</th>
                  <th className="font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.events.slice(0, 25).map((e) => {
                  const t = eventType(e)
                  return (
                    <tr key={e.id} className="hover:bg-muted/50 cursor-pointer border-b last:border-0" onClick={() => setSelected(e.node_id)}>
                      <td className="py-1.5 pr-3 tabular-nums">{hhmmss(e.observed_at)}</td>
                      <td className="pr-3">{e.node_id}</td>
                      <td className="pr-3">{t.label}</td>
                      <td className="pr-3 tabular-nums">{t.value}</td>
                      <td className="pr-3 tabular-nums">{pct(e.human)}</td>
                      <td className="pr-3 tabular-nums">{pct(e.structural)}</td>
                      <td className="pr-3 tabular-nums">{pct(e.environmental)}</td>
                      <td>
                        <span className={`inline-flex items-center gap-1 font-medium ${t.sev >= 3 ? "text-red-500" : t.sev === 2 ? "text-red-500" : t.sev === 1 ? "text-amber-500" : "text-emerald-600"}`}>
                          <span className={`size-1.5 rounded-full ${t.sev >= 2 ? "bg-red-500" : t.sev === 1 ? "bg-amber-500" : "bg-emerald-500"}`} />
                          {t.sev >= 3 ? "escalated" : t.sev === 2 ? "critical" : t.sev === 1 ? "watch" : "noted"}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          ) : <Empty text="Nothing flagged in the last hour" />}
        </CardContent>
      </Card>

      <p className="text-muted-foreground text-xs">
        Scores are transparent evidence weights computed by the API against each node's own learned baseline, not a
        trained model. Human presence leans on sound and repeated tapping on the piezo disc; a temperature probe is
        weak evidence of a person.
      </p>
    </div>
  )
}

function Kpi({ icon, label, value, v, sub }: {
  icon: ReactNode; label: string; value?: string; v?: number | null; sub?: string
}) {
  const l = v === undefined ? null : level(v)
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="flex items-center gap-1.5">{icon}{label}</CardDescription>
        <CardTitle className={`text-2xl tabular-nums ${l?.cls ?? ""}`}>
          {value ?? (l ? `${l.label}` : "—")}
          {l && v != null && <span className="text-muted-foreground ml-2 text-sm font-normal">{pct(v)}</span>}
        </CardTitle>
      </CardHeader>
      {sub && <p className="text-muted-foreground px-6 pb-4 text-xs">{sub}</p>}
    </Card>
  )
}

const DEPLOY: { kind: NodeKind; label: string; episode?: "f" | "c" | "t" }[] = [
  { kind: "rescue", label: "Rubble listening probe" },
  { kind: "gas", label: "Gas & heat sentinel" },
  { kind: "struct", label: "Structural monitor" },
  { kind: "field", label: "Field module" },
]

function FleetBar({ fleet, region, onChange }: {
  fleet: FleetStatus | null; region: string; onChange: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [kind, setKind] = useState<NodeKind>("rescue")
  const [withEvent, setWithEvent] = useState(false)
  if (!fleet) return null
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    try { await fn() } catch { /* the request already toasted */ } finally { setBusy(false); onChange() }
  }
  const episode = ({ rescue: "t", gas: "f", struct: "c", field: "t" } as const)[kind]
  return (
    <Card className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 text-xs">
      <div className="flex items-center gap-2">
        <span className={`size-2 rounded-full ${fleet.enabled ? "bg-emerald-500" : "bg-zinc-400"}`} />
        <span className="font-medium">Simulated sensor fleet</span>
        <span className="text-muted-foreground">
          {fleet.enabled ? `${fleet.nodes} nodes · reading every ${fleet.period_s}s` : "off"}
          {fleet.enabled && fleet.episodes.length > 0 && ` · ${fleet.episodes.length} event(s) in progress`}
        </span>
        <button type="button" disabled={busy} onClick={() => act(() => setFleet(!fleet.enabled))}
          className="hover:bg-muted rounded-md border px-2 py-0.5 disabled:opacity-50">
          {fleet.enabled ? "Turn off" : "Turn on"}
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <select value={kind} onChange={(e) => setKind(e.target.value as NodeKind)}
          className="bg-background rounded-md border px-2 py-1">
          {DEPLOY.map((d) => <option key={d.kind} value={d.kind}>{d.label}</option>)}
        </select>
        <label className="text-muted-foreground flex items-center gap-1">
          <input type="checkbox" checked={withEvent} onChange={(e) => setWithEvent(e.target.checked)} />
          lands in an active event
        </label>
        <button type="button" disabled={busy || !fleet.enabled}
          onClick={() => act(() => deployNode(kind, region, withEvent ? episode : undefined))}
          className="bg-primary text-primary-foreground flex items-center gap-1 rounded-md px-2.5 py-1 disabled:opacity-50">
          <Plus className="size-3.5" /> Deploy node
        </button>
      </div>
      <span className="text-muted-foreground basis-full">
        Simulated nodes go through the same scoring and escalation as the LoRa hardware and are labelled
        "simulated"; a real node joins the field the moment the bridge posts its first reading.
      </span>
    </Card>
  )
}
