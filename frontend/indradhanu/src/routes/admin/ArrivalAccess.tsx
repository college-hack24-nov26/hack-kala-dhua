import { useEffect, useMemo, useRef, useState } from "react"
import mapboxgl from "mapbox-gl"
import { Ambulance, Box, CheckCircle2, Flame, Footprints, Info, Map as MapIcon, Navigation, TriangleAlert } from "lucide-react"
import { request } from "@/api/httpClient"
import { useDemo } from "@/routes/demo/DemoProvider"
import type { UnitRoute } from "@/routes/demo/useDemo"
import { Segmented } from "@/components/common/MacControls"
import { StatCard } from "@/components/common/StatCard"
import { ribbon, setSource, useMapbox } from "@/components/map/useMapbox"
import { cn } from "@/lib/utils"
import { unitIcon } from "./ZoneMap"

/** Will this unit's road still be passable when it gets there?
 *
 *  Pick a unit that is on its way: the page takes its type (an ambulance can
 *  drive through 0.3 m of water, a fire tender 0.6 m, a crew on foot 0.2 m) and
 *  its ETA, asks the passability model about every road on and around its
 *  route at that arrival time, and says whether the route is clear. With no
 *  unit picked it shows the whole district for the unit type and time you
 *  choose. "Now" is the current-status baseline: what the reports say right
 *  now, with no prediction. */

type Profile = "ambulance" | "fire_engine" | "resident"
type When = "now" | "30" | "60" | "90"
type Props = { seg: string; p: number; sd: number; lo: number; hi: number; why: string; now: boolean; avoid: boolean; underpass: boolean; bridge: boolean }
type Arrival = {
  available: boolean; reason?: string; model?: string; computedAt?: number; notes?: string[]
  scored?: number; blockedNow?: number; predictedBlocked?: number; closingBeforeArrival?: number; uncertain?: number
  segments?: GeoJSON.FeatureCollection<GeoJSON.LineString, Props>
}
type Feature = GeoJSON.Feature<GeoJSON.LineString, Props>

const PROFILE: Record<Profile, { label: string; limit: string; icon: typeof Ambulance }> = {
  ambulance: { label: "Ambulance", limit: "0.3 m", icon: Ambulance },
  fire_engine: { label: "Fire tender", limit: "0.6 m", icon: Flame },
  resident: { label: "On foot", limit: "0.2 m", icon: Footprints },
}
const HEIGHT_M = 260
const RAMP: mapboxgl.Expression = ["interpolate", ["linear"], ["get", "c"], 0.1, "#9db8f0", 0.3, "#f2c14e", 0.6, "#ec835a", 0.85, "#d03b3b"]

/** Which of the model's three unit classes a vehicle drives like (by water limit). */
function profileOf(kind: string): Profile | null {
  if (/boat|heli|drone/.test(kind)) return null
  if (/resident|foot|walk/.test(kind)) return "resident"
  // A rescue team rides in a truck and wades to 0.5 m: the tender class (0.6 m) is the nearest.
  if (/fire|jcb|bus|pump|truck|tanker|excavat|rescue/.test(kind)) return "fire_engine"
  return "ambulance"
}
const horizonOf = (eta: number | null): When => (eta == null || eta <= 30 ? "30" : eta <= 60 ? "60" : "90")

/** Metres from point p to segment a-b (flat, good at city scale). */
function distM(p: [number, number], a: [number, number], b: [number, number]) {
  const kx = 111320 * Math.cos((p[1] * Math.PI) / 180), ky = 110540
  const ax = (a[0] - p[0]) * kx, ay = (a[1] - p[1]) * ky, bx = (b[0] - p[0]) * kx, by = (b[1] - p[1]) * ky
  const dx = bx - ax, dy = by - ay
  const t = Math.max(0, Math.min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy || 1)))
  return Math.hypot(ax + t * dx, ay + t * dy)
}
function nearRoute(f: Feature, path: [number, number][], m = 30) {
  const cs = f.geometry.coordinates as [number, number][]
  for (const c of [cs[0], cs[Math.floor(cs.length / 2)], cs[cs.length - 1]]) {
    for (let k = 0; k < path.length - 1; k++) if (distM(c, path[k], path[k + 1]) <= m) return true
  }
  return false
}

export default function ArrivalAccess() {
  const { state, region: regionPick } = useDemo()
  const [unitId, setUnitId] = useState<string | null>(null)
  const [profile, setProfile] = useState<Profile>("ambulance")
  const [when, setWhen] = useState<When>("30")
  const [three, setThree] = useState(true)
  const [others, setOthers] = useState(false)
  const [data, setData] = useState<Arrival | null>(null)
  const [loading, setLoading] = useState(false)
  const [tick, setTick] = useState(0)
  const { ref, map, ready, failed } = useMapbox({ center: [73.8, 18.63], zoom: 12.2, pitch: 50 })
  const popup = useRef<mapboxgl.Popup | null>(null)

  // units on their way, soonest first; only road units the model covers
  const moving = useMemo(() => state.routes
    .filter((r) => r.path?.length >= 2 && profileOf(r.resourceKind) && !/complete|cancel/.test(r.status))
    .sort((a, b) => (a.etaMinutes ?? 999) - (b.etaMinutes ?? 999)), [state.routes])
  const unit: UnitRoute | null = unitId ? moving.find((r) => r.resourceId === unitId) ?? null : null

  // a picked unit sets the unit type and the arrival time
  useEffect(() => {
    if (!unit) return
    setProfile(profileOf(unit.resourceKind) ?? "ambulance")
    setWhen(horizonOf(unit.etaMinutes))
  }, [unit?.resourceId, unit?.etaMinutes]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let alive = true
    setLoading(true)
    const h = when === "now" ? "30" : when
    request<Arrival>(`/nav/arrival?profile=${profile}&horizon=${h}&min_p=0.15`, { toast: false })
      .then((d) => alive && setData(d))
      .catch((e) => alive && setData({ available: false, reason: String((e as Error).message ?? e) }))
      .finally(() => alive && setLoading(false))
    return () => { alive = false }
  }, [profile, when, tick])
  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), 120_000)
    return () => clearInterval(id)
  }, [])

  const all = useMemo(() => {
    const fs = (data?.segments?.features ?? []) as Feature[]
    return when === "now" ? fs.filter((f) => f.properties.now) : fs
  }, [data, when])
  const onRoute = useMemo(() => (unit ? all.filter((f) => nearRoute(f, unit.path)) : []), [all, unit])
  const routeBad = onRoute.filter((f) => (when === "now" ? f.properties.now : f.properties.avoid))
    .sort((a, b) => b.properties.p - a.properties.p)
  const shown = unit ? (others ? all : onRoute) : all

  // draw
  useEffect(() => {
    const m = map.current
    if (!m || !ready) return
    const routeIds = new Set(onRoute.map((f) => f.properties.seg))
    const prop = (f: Feature) => ({
      ...f.properties, c: when === "now" ? 1 : f.properties.p, mode: when,
      dim: unit && !routeIds.has(f.properties.seg) ? 1 : 0,
      h: (when === "now" ? 1 : f.properties.p) * HEIGHT_M, hiH: (when === "now" ? 1 : f.properties.hi) * HEIGHT_M,
    })
    setSource(m, "arr-lines", { type: "FeatureCollection", features: shown.map((f) => ({ ...f, properties: prop(f) })) })
    setSource(m, "arr-ribbons", { type: "FeatureCollection", features: three ? shown
      .filter((f) => !unit || routeIds.has(f.properties.seg))
      .map((f) => ({ type: "Feature", geometry: ribbon(f.geometry.coordinates as [number, number][], 7), properties: prop(f) })) : [] })
    setSource(m, "arr-route", { type: "FeatureCollection", features: unit ? [{
      type: "Feature", geometry: { type: "LineString", coordinates: unit.path }, properties: {} }] : [] })
    const pos = unit ? state.resources.find((r) => r.id === unit.resourceId)?.location ?? unit.path[0] : null
    setSource(m, "arr-pins", { type: "FeatureCollection", features: unit && pos ? [
      { type: "Feature", geometry: { type: "Point", coordinates: pos }, properties: { k: "unit", label: unit.resourceLabel } },
      { type: "Feature", geometry: { type: "Point", coordinates: unit.path[unit.path.length - 1] }, properties: { k: "dest", label: unit.incidentTitle } },
    ] : [] })

    if (!m.getLayer("arr-line")) {
      m.addLayer({ id: "arr-route", type: "line", source: "arr-route",
        paint: { "line-color": "#3b6fe0", "line-width": ["interpolate", ["linear"], ["zoom"], 11, 4, 15, 9], "line-opacity": 0.35 },
        layout: { "line-cap": "round", "line-join": "round" } })
      m.addLayer({ id: "arr-line", type: "line", source: "arr-lines",
        paint: { "line-color": RAMP, "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1.5, 15, 5],
                 "line-opacity": ["case", ["==", ["get", "dim"], 1], 0.25, 0.95] },
        layout: { "line-cap": "round" } })
      m.addLayer({ id: "arr-uncertain", type: "line", source: "arr-lines",
        filter: ["all", ["!=", ["get", "mode"], "now"], [">=", ["get", "hi"], 0.6], ["<", ["get", "p"], 0.6]],
        paint: { "line-color": "#ec835a", "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1, 15, 3], "line-dasharray": [1.5, 1.5], "line-offset": 3 } })
      m.addLayer({ id: "arr-band", type: "fill-extrusion", source: "arr-ribbons",
        paint: { "fill-extrusion-color": "#ec835a", "fill-extrusion-base": ["get", "h"], "fill-extrusion-height": ["get", "hiH"], "fill-extrusion-opacity": 0.22 } })
      m.addLayer({ id: "arr-volume", type: "fill-extrusion", source: "arr-ribbons",
        paint: { "fill-extrusion-color": RAMP, "fill-extrusion-height": ["get", "h"], "fill-extrusion-opacity": 0.85 } })
      m.addLayer({ id: "arr-pins", type: "circle", source: "arr-pins", paint: {
        "circle-radius": 9, "circle-color": ["case", ["==", ["get", "k"], "unit"], "#3b6fe0", "#d03b3b"],
        "circle-stroke-color": "#ffffff", "circle-stroke-width": 3 } })
      m.addLayer({ id: "arr-pin-labels", type: "symbol", source: "arr-pins",
        layout: { "text-field": ["get", "label"], "text-size": 12, "text-offset": [0, 1.6], "text-anchor": "top",
                  "text-font": ["DIN Pro Medium", "Arial Unicode MS Regular"] },
        paint: { "text-color": "#111827", "text-halo-color": "#ffffff", "text-halo-width": 1.5 } })
      const show = (e: mapboxgl.MapLayerMouseEvent) => {
        const f = e.features?.[0]
        if (!f) return
        const p = f.properties as unknown as Props & { mode: string }
        m.getCanvas().style.cursor = "pointer"
        popup.current ??= new mapboxgl.Popup({ closeButton: false, closeOnClick: false, offset: 10, className: "indra-pop", maxWidth: "300px" })
        const pct = (x: number) => `${Math.round(Number(x) * 100)}%`
        popup.current.setLngLat(e.lngLat).setHTML(
          (p.mode === "now"
            ? `<div class="ip-title">Blocked now (current status)</div><div class="ip-sub">model at +30 min: ${pct(p.p)}</div>`
            : `<div class="ip-title">${pct(p.p)} chance blocked at +${p.mode} min</div>`) +
          `<div class="ip-sub">likely range ${pct(p.lo)}–${pct(p.hi)} · ${String(p.why)}</div><div class="ip-hr"></div>` +
          `<div class="ip-row"><span>Right now (reports)</span><span>${String(p.now) === "true" ? "blocked" : "open"}</span></div>` +
          `<div class="ip-row"><span>Router</span><span>${String(p.avoid) === "true" ? "avoids this road" : "may use it"}</span></div>` +
          (String(p.underpass) === "true" ? `<div class="ip-row"><span>Road</span><span>underpass</span></div>` : "") +
          (String(p.bridge) === "true" ? `<div class="ip-row"><span>Road</span><span>bridge</span></div>` : ""),
        ).addTo(m)
      }
      const hide = () => { m.getCanvas().style.cursor = ""; popup.current?.remove() }
      for (const id of ["arr-line", "arr-volume"]) {
        m.on("mousemove", id, show)
        m.on("mouseleave", id, hide)
      }
    }
  }, [shown, onRoute, unit, three, ready, map, when, state.resources])

  // camera: fit the picked unit's route, tilt for 3D
  useEffect(() => {
    const m = map.current
    if (!m || !ready) return
    if (unit) {
      const b = new mapboxgl.LngLatBounds(unit.path[0], unit.path[0])
      for (const c of unit.path) b.extend(c)
      m.fitBounds(b, { padding: 90, maxZoom: 15, pitch: three ? 50 : 0, duration: 800 })
    } else {
      m.easeTo({ pitch: three ? 50 : 0, duration: 600 })
    }
  }, [unit?.resourceId, three, ready, map]) // eslint-disable-line react-hooks/exhaustive-deps

  const P = PROFILE[profile]
  const ncr = regionPick === "ncr"

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <p className="max-w-3xl text-sm text-muted-foreground">
        Pick a unit on its way: this shows whether the roads on its route will still be passable <b className="font-medium text-foreground">when it gets
        there</b>, for that kind of vehicle, from the passability model on the live evidence. With no unit picked, it shows the whole district.
      </p>

      <div className="grid gap-5 xl:grid-cols-[340px_1fr]">
        {/* units */}
        <aside className="space-y-5">
          <section className="rounded-2xl border bg-card shadow-card">
            <div className="flex items-center justify-between border-b px-5 py-4">
              <h3 className="text-sm font-semibold">Units on their way</h3>
              {unit && <button onClick={() => setUnitId(null)} className="text-xs font-medium text-primary hover:underline">Whole district</button>}
            </div>
            {moving.length ? (
              <ul className="max-h-[46vh] divide-y overflow-y-auto">
                {moving.map((r) => {
                  const Icon = unitIcon(r.resourceKind)
                  const on = r.resourceId === unitId
                  return (
                    <li key={r.id}>
                      <button onClick={() => setUnitId(on ? null : r.resourceId)}
                              className={cn("flex w-full items-center gap-3 px-5 py-3 text-left transition-colors", on ? "bg-accent" : "hover:bg-muted/50")}>
                        <span className={cn("grid size-9 shrink-0 place-items-center rounded-xl", on ? "bg-primary text-primary-foreground" : "bg-accent text-accent-foreground")}>
                          <Icon className="size-4" />
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium">{r.resourceLabel}</span>
                          <span className="block truncate text-xs text-muted-foreground">→ {r.incidentTitle}</span>
                        </span>
                        <span className="shrink-0 text-right text-xs tabular-nums">
                          <span className="block font-semibold">{r.etaMinutes != null ? `${Math.round(r.etaMinutes)} min` : "—"}</span>
                          <span className="text-muted-foreground">{PROFILE[profileOf(r.resourceKind) ?? "ambulance"].limit}</span>
                        </span>
                      </button>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <p className="p-5 text-sm text-muted-foreground">
                No road unit is on its way right now. Start the live run on the Live map, or use the whole-district view.
              </p>
            )}
          </section>

          {unit && data?.available && (
            <section className={cn("rounded-2xl border p-5 shadow-card",
              routeBad.length ? "border-red-200 bg-red-50/60 dark:border-red-500/30 dark:bg-red-500/10" : "border-emerald-200 bg-emerald-50/60 dark:border-emerald-500/30 dark:bg-emerald-500/10")}>
              <div className="flex items-start gap-3">
                {routeBad.length
                  ? <TriangleAlert className="mt-0.5 size-5 shrink-0 text-red-600" />
                  : <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-emerald-600" />}
                <div>
                  <h3 className="text-sm font-semibold">
                    {routeBad.length
                      ? `${routeBad.length} stretch${routeBad.length === 1 ? "" : "es"} of its route may be impassable when it arrives`
                      : when === "now" ? "Nothing on its route is reported blocked now" : "Its route should still be passable when it arrives"}
                  </h3>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {unit.resourceLabel} · {P.label.toLowerCase()} (water limit {P.limit}) · ETA {unit.etaMinutes != null ? `${Math.round(unit.etaMinutes)} min` : "unknown"} ·
                    {when === "now" ? " current status" : ` scored at +${when} min`}
                  </p>
                </div>
              </div>
              {routeBad.length > 0 && (
                <ul className="mt-3 space-y-1.5">
                  {routeBad.slice(0, 5).map((f) => (
                    <li key={f.properties.seg} className="flex items-center gap-2 text-xs">
                      <span className="size-2 rounded-full" style={{ background: f.properties.p >= 0.85 ? "#d03b3b" : "#ec835a" }} />
                      <span className="flex-1">{f.properties.underpass ? "Underpass" : f.properties.bridge ? "Bridge" : "Road"} · {f.properties.why}</span>
                      <span className="tabular-nums font-medium">{Math.round(f.properties.p * 100)}%</span>
                    </li>
                  ))}
                </ul>
              )}
              <p className="mt-3 text-[11px] text-muted-foreground">
                {routeBad.length ? "The router avoids these roads on its next re-plan; " : ""}
                {onRoute.length} road{onRoute.length === 1 ? "" : "s"} near its route carry any risk.
              </p>
            </section>
          )}
        </aside>

        {/* map */}
        <div className="space-y-5">
          <div className="relative h-[66vh] min-h-[520px] overflow-hidden rounded-2xl border shadow-card">
            <div className="absolute inset-0"><div ref={ref} className="h-full w-full" /></div>
            {failed && (
              <div className="absolute inset-0 grid place-items-center bg-muted/80 p-6 text-center text-sm text-muted-foreground">{failed}</div>
            )}
            <div className="pointer-events-none absolute inset-x-0 top-0 flex flex-wrap items-start gap-2 p-3">
              <div className="pointer-events-auto flex flex-wrap items-center gap-2 rounded-2xl border bg-card/95 p-2 shadow-md backdrop-blur">
                {unit ? (
                  <span className="inline-flex h-8 items-center gap-1.5 rounded-lg bg-accent px-3 text-xs font-medium text-accent-foreground">
                    <Navigation className="size-3.5" /> {unit.resourceLabel} · scored as {P.label.toLowerCase()} ({P.limit})
                  </span>
                ) : (
                  <Segmented size="sm" value={profile} onChange={setProfile} ariaLabel="Unit type"
                    options={(Object.keys(PROFILE) as Profile[]).map((k) => ({ value: k, label: PROFILE[k].label, icon: PROFILE[k].icon, title: `water limit ${PROFILE[k].limit}` }))} />
                )}
                <Segmented size="sm" value={when} onChange={setWhen} ariaLabel="Arrival time"
                  options={[{ value: "now", label: "Now" }, { value: "30", label: "+30" }, { value: "60", label: "+60" }, { value: "90", label: "+90 min" }]} />
                <Segmented size="sm" value={three ? "3d" : "2d"} onChange={(v) => setThree(v === "3d")} ariaLabel="View"
                  options={[{ value: "2d", label: "2D", icon: MapIcon }, { value: "3d", label: "3D", icon: Box }]} />
                {unit && (
                  <label className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground">
                    <input type="checkbox" checked={others} onChange={(e) => setOthers(e.target.checked)} /> other roads
                  </label>
                )}
              </div>
            </div>
            <div className="pointer-events-none absolute bottom-3 left-3 rounded-xl border bg-card/95 px-3 py-2 text-[11px] shadow-sm backdrop-blur">
              <div className="mb-1 font-medium">Road blocked when the unit arrives</div>
              <div className="h-2 w-48 rounded-full" style={{ background: "linear-gradient(90deg,#9db8f0,#f2c14e,#ec835a,#d03b3b)" }} />
              <div className="mt-0.5 flex w-48 justify-between text-muted-foreground"><span>15%</span><span>30%</span><span>60% avoided</span><span>85%</span></div>
              <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-muted-foreground">
                <span className="flex items-center gap-1"><span className="h-1.5 w-4 rounded bg-[#3b6fe0]/40" /> its route</span>
                <span className="flex items-center gap-1"><span className="size-2.5 rounded-full bg-[#3b6fe0] ring-2 ring-white" /> unit</span>
                <span className="flex items-center gap-1"><span className="size-2.5 rounded-full bg-[#d03b3b] ring-2 ring-white" /> where it is going</span>
                <span className="flex items-center gap-1"><span className="h-0 w-4 border-t-2 border-dashed border-[#ec835a]" /> uncertain</span>
              </div>
            </div>
            {loading && <div className="absolute right-14 top-3 rounded-lg border bg-card/95 px-2.5 py-1.5 text-xs text-muted-foreground shadow-sm">Scoring…</div>}
            {data?.available && !loading && all.length === 0 && (
              <div className="absolute inset-x-0 top-20 mx-auto w-fit max-w-md rounded-xl border bg-card/95 px-4 py-3 text-center text-xs shadow-md backdrop-blur">
                <b className="font-medium">No road in the district is {when === "now" ? "reported blocked" : `expected blocked at +${when} min`} for {P.label.toLowerCase()}.</b>
                <span className="mt-1 block text-muted-foreground">The model is live; roads appear here as rain, river levels and water reports come in.
                  {data.notes?.length ? ` ${data.notes.join(" · ")}` : ""}</span>
              </div>
            )}
          </div>

          {data && !data.available ? (
            <div className="flex items-start gap-2 rounded-2xl border bg-card p-5 text-sm text-muted-foreground shadow-card">
              <Info className="mt-0.5 size-4 shrink-0" />
              {data.reason ?? "The model is not available."}
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              <StatCard label="Blocked now" value={data?.blockedNow ?? "—"} sub="current status (reports)" />
              <StatCard label={`Blocked at +${when === "now" ? 30 : when} min`} value={data?.predictedBlocked ?? "—"} sub={`for ${P.label.toLowerCase()} · router avoids`} tone={(data?.predictedBlocked ?? 0) > 0 ? "warn" : undefined} />
              <StatCard label="Close before arrival" value={data?.closingBeforeArrival ?? "—"} sub="open now, blocked by then" tone={(data?.closingBeforeArrival ?? 0) > 0 ? "bad" : undefined} />
              <StatCard label="Uncertain" value={data?.uncertain ?? "—"} sub={`model ${data?.model ?? "—"}`} />
            </div>
          )}
          {ncr && (
            <p className="text-xs text-muted-foreground">The road model covers the PCMC district (Pune). Ghaziabad routes use reported blocks only.</p>
          )}
        </div>
      </div>
    </div>
  )
}
