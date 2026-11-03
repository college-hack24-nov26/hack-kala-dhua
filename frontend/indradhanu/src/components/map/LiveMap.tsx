import { useEffect, useRef, useState } from "react"
import { getRegion } from "@/lib/region"
import mapboxgl from "mapbox-gl"
import { request } from "@/api/httpClient"
import type { Feature, FeatureCollection } from "geojson"
import { badgeName, imageName, provideIcon, registerIcons } from "./icons"
import {
  CRITICAL_SEVERITY, MAP, incidentColour, placeColour, serviceColour, statusRing,
} from "./mapTheme"
import "mapbox-gl/dist/mapbox-gl.css"

/** The map, on Mapbox.
 *
 *  One component for all three interfaces. What differs between them is which
 *  layers get data, not how the map works, so the citizen portal and the
 *  operations console cannot drift into disagreeing about where something is.
 *
 *  Data goes in through GeoJSON sources that are *updated* rather than
 *  recreated. Tearing down and rebuilding a layer on every poll makes markers
 *  flicker once a second, which on a projector looks like the system is
 *  struggling. Updating `setData` lets Mapbox interpolate instead.
 */

export type Activity = { at: string; text: string; kind: string }
export type ActivityIndex = Map<string, Activity[]>

export type WardFeature = {
  id: string; name: string; number?: string; boundary: [number, number][] | null
  severity: number | null; score: number | null; population?: number
  populationAtRisk?: number | null
}
export type IncidentFeature = {
  id: string; title: string; category: string; severity: number
  reportCount: number; confidence?: number; unitsEnRoute?: number
  status?: string; createdAt?: string; wardId?: string; street?: string | null
  location: [number, number]
}
export type ResourceFeature = {
  id: string; kind: string; label: string; status: string; operator?: string
  capacity?: number; capabilities?: string[]; assignedTo?: string | null
  etaMinutes?: number | null; location: [number, number]
  incidentId?: string | null; assignmentStatus?: string | null
  distanceKm?: number | null; statusNote?: string | null
  unavailableReason?: string | null
}
export type FacilityFeature = {
  id: string; name: string; kind: string; status: string
  capacity: number | null; occupancy: number | null; location: [number, number]
  acceptsCasualties?: boolean
  kindLabel?: string
  /** Relief stock on hand: food packets, litres of water, medical kits. */
  supplies?: Record<string, number>
  servedPerHour?: number | null
}
export type BlockFeature = {
  id: string; reason: string; location: [number, number]; reportedBy?: string
  /** How far around the point the closure reaches, when the crew said. */
  radiusM?: number
}
/** A unit's road geometry to what it was tasked with. */
export type RouteFeature = {
  id: string; resourceId: string; resourceLabel: string
  incidentId: string; incidentTitle: string; status: string
  etaMinutes: number | null; distanceKm: number | null
  engine?: string | null; progress?: number
  steps?: { instruction: string; street: string; distanceM: number }[]
  path: [number, number][]
}
export type NeedFeature = {
  incidentId: string; capability: string; required: number; met: number
}

/** What a click landed on, for maps that drive a selection rather than hover cards. */
export type MapSelection = {
  kind: "incident" | "resource" | "facility" | "block"
  id: string
}

/** Ask the camera to move. A new `key` is a new request; the same key is ignored,
 *  so re-renders never re-fly the camera. */
export type CameraRequest = {
  key: string | number
  center?: [number, number]
  zoom?: number
  /** [[west, south], [east, north]] */
  bounds?: [[number, number], [number, number]]
  /** Zoom in or out by this much, keeping the centre. */
  zoomBy?: number
}

export type MapPadding = { top: number; right: number; bottom: number; left: number }

type Props = {
  wards?: WardFeature[]
  incidents?: IncidentFeature[]
  resources?: ResourceFeature[]
  facilities?: FacilityFeature[]
  blocks?: BlockFeature[]
  needs?: NeedFeature[]
  /** id -> what has happened to it, newest first. Drives the hover card. */
  activity?: ActivityIndex
  /** Every committed unit's road geometry, drawn as amber lines it can be
   *  hovered for the turn list. Replaces the dashed straight "link" lines. */
  routes?: RouteFeature[]
  /** A single highlighted route: the citizen's own, or the one an operator is
   *  inspecting. Drawn in green, over everything else. */
  route?: number[][]
  /** Label for that highlighted route, shown on hover. */
  routeLabel?: string
  /** The viewer's own position, if this interface has one. */
  me?: { lng: number; lat: number; label?: string; accuracyM?: number | null } | null
  center?: [number, number]
  zoom?: number
  className?: string
  onPickIncident?: (id: string) => void
  /** Recentre on `me` whenever it moves. On for the citizen, off for the console. */
  followMe?: boolean
  /** Called once when the person moves the camera themselves.
   *
   *  A follow-me camera that cannot be let go of is worse than one that never
   *  follows: a crew checking what is two streets over gets yanked back on the
   *  next fix, mid-look. The caller turns following off when this fires. */
  onUserMove?: () => void
  /** Bump to recentre on `me` again without `me` having moved. A caller used to
   *  do this by handing us a new object with the same coordinates in it, which
   *  worked only because the effect below compared object identity. */
  recentreKey?: number
  /** Called with a ward id when the ward itself (not an incident) is clicked. */
  onPickWard?: (id: string) => void

  // ---- The map experience (citizen and crew apps). All optional; the console
  // ---- passes none of them and renders exactly as it always has.

  /** `night`: Mapbox Standard with the night light preset — dark, but keeping
   *  every street name, river and landmark a person navigates by. */
  basemap?: "streets" | "night"
  /** Light preset for the `night` basemap, switchable without a style reload. */
  lightPreset?: "night" | "dusk" | "dawn" | "day"
  /** `badge`: round markers centred on their point, shared with the Android map. */
  markers?: "pin" | "badge"
  /** Group dense incidents, units and places. Critical incidents never cluster. */
  cluster?: boolean
  /** Hover cards. Off for touch, where a tap selects instead. */
  hoverCards?: boolean
  /** A click on a marker (or on nothing: `null`). When set, takes over from
   *  `onPickIncident`/`onPickWard`. */
  onSelect?: (hit: MapSelection | null) => void
  /** Draw a selection ring here. */
  selection?: { lng: number; lat: number; colour?: string } | null
  camera?: CameraRequest | null
  /** Space covered by floating UI, so centring and fitting use the visible map. */
  padding?: MapPadding
  /** How far the highlighted route can be trusted. `uncertain` draws it dashed. */
  routeStatus?: "clear" | "caution" | "uncertain"
  /** 0..1 of the highlighted route already travelled; that part is dimmed. */
  routeProgress?: number
  /** Pulse critical incidents. */
  pulseCritical?: boolean
  /** Turn-by-turn camera, as in Google Maps: while set (and `followMe`), the map
   *  tilts, zooms in, turns so the way ahead points up, and keeps `me` in the
   *  lower third. Cleared, the map flattens and faces north again. */
  navigation?: { heading: number | null } | null
}

const TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined

// Start Mapbox's web workers before the first map is built, so the first
// screen does not pay for spinning them up. Harmless if called again.
try {
  ;(mapboxgl as unknown as { prewarm?: () => void }).prewarm?.()
} catch {
  /* older builds without prewarm */
}

/** Mapbox's own popup stylesheet is a white card with a white arrow, which on
 *  the dark basemap rendered as near-white text on near-white and read as
 *  "hover does nothing". Injected once, from here, so the component carries its
 *  own appearance instead of depending on a global stylesheet that other work
 *  might rewrite. */
const POPUP_CSS = `
.indra-pop .mapboxgl-popup-content {
  background: #ffffff;
  color: #111827;
  border: 1px solid #e8eaef;
  border-radius: 12px;
  padding: 10px 12px;
  box-shadow: 0 8px 24px rgb(16 24 40 / 0.12);
  max-width: 320px;
  font: 12px/1.45 ui-sans-serif, system-ui, sans-serif;
}
.indra-pop .mapboxgl-popup-tip { display: none; }
.indra-pop .ip-title { font-size: 13px; font-weight: 600; color: #111827; }
.indra-pop .ip-sub { color: #6b7280; margin-top: 1px; }
.indra-pop .ip-row { display: flex; justify-content: space-between; gap: 12px; margin-top: 3px; }
.indra-pop .ip-row span:first-child { color: #6b7280; }
.indra-pop .ip-row span:last-child { color: #111827; font-variant-numeric: tabular-nums; }
.indra-pop .ip-chip {
  display: inline-block; padding: 1px 7px; border-radius: 999px;
  font-size: 11px; font-weight: 500; margin-top: 6px;
}
.indra-pop .ip-hr { border-top: 1px solid #eef0f3; margin: 8px 0 6px; }
.indra-pop .ip-head { color: #6b7280; font-size: 10.5px; letter-spacing: .06em;
  text-transform: uppercase; }
.indra-pop .ip-act { display: flex; gap: 7px; margin-top: 4px; }
.indra-pop .ip-act i { color: #9ca3af; font-style: normal; flex: none;
  font-variant-numeric: tabular-nums; }
.indra-pop .ip-act span { color: #374151; }
.indra-pop .ip-warn { color: #b91c1c; }
.indra-pop .ip-ok { color: #15803d; }
.indra-pulse { width: 28px; height: 28px; pointer-events: none; }
.indra-pulse::before, .indra-pulse::after {
  content: ""; position: absolute; inset: 0; border-radius: 999px;
  background: rgb(239 68 68 / 0.45); animation: indra-pulse 1.8s ease-out infinite;
}
.indra-pulse::after { animation-delay: .9s; }
@keyframes indra-pulse {
  from { transform: scale(1); opacity: .9; }
  to { transform: scale(2.4); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .indra-pulse::before, .indra-pulse::after { animation: none; opacity: .35; transform: scale(1.6); }
}
.mapboxgl-ctrl-bottom-left, .mapboxgl-ctrl-bottom-right { z-index: 1; }
`

let cssInjected = false
function injectCss() {
  if (cssInjected || typeof document === "undefined") return
  const el = document.createElement("style")
  el.dataset.indraMap = "1"
  el.textContent = POPUP_CSS
  document.head.appendChild(el)
  cssInjected = true
}

/** Report text arrives from the public. It is not markup. */
const esc = (v: unknown) =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!
  )

/** The basemap under everything else.
 *
 *  `light-v11` and `dark-v11` are built to disappear beneath a data overlay.
 *  That is the right instinct for a choropleth an analyst reads, and the wrong
 *  one for the citizen map: somebody deciding which way to walk out of a flood
 *  navigates by the things those styles delete — the street name, the river,
 *  the park, the petrol station that tells them they are on the right road.
 *  These two carry that detail and still leave room for the overlay, because
 *  the severity fill is 10-48% alpha and the ward shading is drawn *under* the
 *  labels rather than over them.
 */
const BASEMAP = {
  light: "mapbox://styles/mapbox/streets-v12",
  // Both themes, on purpose. `navigation-night-v1` was the obvious dark
  // counterpart and it composites `mapbox.mapbox-incidents-v1`, a traffic feed
  // this account is not entitled to: every pan produced a row of 404s in the
  // console for tiles that would never arrive. The navigation styles exist to
  // carry live traffic, and we do not have traffic — we have our own incidents,
  // drawn as our own layers on top. `streets-v12` is the same colourful base
  // without the feed we cannot fetch, and its dark rendering is legible enough
  // that a broken console is the worse trade.
  dark: "mapbox://styles/mapbox/streets-v12",
} as const

/** Mapbox Standard. With the night preset it is the dark, premium basemap of the
 *  citizen and crew maps — and unlike `dark-v11` it keeps the POIs, street names
 *  and water a person navigates by. It carries no traffic feed, so it has none
 *  of the 404s `navigation-night-v1` produced on this account. */
const NIGHT_STYLE = "mapbox://styles/mapbox/standard"

/** The lowest label layer in the basemap.
 *
 *  Anything inserted before it is drawn underneath every street name and place
 *  name the style ships. Mapbox does not promise a stable id for it across
 *  style versions, so find it by shape — the first symbol layer that draws
 *  text — rather than hard-coding `road-label` and silently getting `undefined`
 *  (which appends to the top) the next time the style is revised.
 */
function firstLabelLayer(m: mapboxgl.Map): string | undefined {
  const layers = m.getStyle()?.layers ?? []
  for (const l of layers) {
    if (l.type !== "symbol") continue
    const layout = (l as { layout?: Record<string, unknown> }).layout
    if (layout && "text-field" in layout) return l.id
  }
  return undefined
}

const SEVERITY_COLOR = [
  "interpolate", ["linear"], ["coalesce", ["get", "severity"], 0],
  0, "rgba(100,116,139,0.10)",
  2, "rgba(56,189,248,0.18)",
  3, "rgba(250,204,21,0.28)",
  4, "rgba(249,115,22,0.38)",
  5, "rgba(239,68,68,0.48)",
] as unknown as mapboxgl.Expression

/** Which drawn icon, and in what colour.
 *
 *  These replace two-letter codes, which were a correct fix for the emoji
 *  `glyphs > 65535` crash and a poor answer to "what is that dot". A tree reads
 *  as a tree at any zoom; "FT" is a puzzle at every one. The icons are raster
 *  images registered with `addImage`, so there is no codepoint limit to hit.
 */
const HAZARD_ICON: Record<string, string> = {
  flooded_road: "hz-flooded_road",
  waterlogging: "hz-waterlogging",
  fallen_tree: "hz-fallen_tree",
  power_line: "hz-power_line",
  structural_damage: "hz-structural_damage",
  blocked_drain: "hz-blocked_drain",
  person_stranded: "hz-person_stranded",
  heat_casualty: "hz-heat_casualty",
  supply_shortage: "hz-supply_shortage",
  fire: "hz-fire",
}
const hazardIcon = (category: string) =>
  HAZARD_ICON[category] ?? "hz-default"

const KIND_ICON: Record<string, string> = {
  ambulance: "rk-ambulance", boat: "rk-boat", pump: "rk-pump",
  fire_engine: "rk-fire_engine", rescue_team: "rk-rescue_team",
  bus: "rk-bus", jcb: "rk-jcb",
  supply_truck: "rk-supply_truck", water_tanker: "rk-water_tanker",
}
const kindIcon = (kind: string) => KIND_ICON[kind] ?? "rk-default"

const LIFELINE_ICON: Record<string, string> = {
  hospital: "lf-hospital", shelter: "lf-shelter",
  relief_centre: "lf-relief_centre", food_kitchen: "lf-food_kitchen",
  water_point: "lf-water_point", medical_camp: "lf-medical_camp",
  pump_station: "lf-pump_station", school: "lf-school",
  substation: "lf-substation",
}
const lifelineIcon = (kind: string) => LIFELINE_ICON[kind] ?? "lf-default"

const LIFELINE_COLOUR: Record<string, string> = {
  hospital: "#0284c7", shelter: "#0d9488",
  relief_centre: "#7c3aed", food_kitchen: "#7c3aed",
  water_point: "#0891b2", medical_camp: "#db2777",
  pump_station: "#475569", school: "#475569", substation: "#475569",
}

const severityColour = (severity: number) =>
  severity >= 5 ? "#ef4444" : severity >= 4 ? "#f97316" : "#eab308"

const statusColour = (status: string) =>
  status === "on_site" ? "#10b981"
  : status === "en_route" ? "#f59e0b"
  : status === "assigned" ? "#0ea5e9"
  : status === "offline" ? "#dc2626"
  : "#64748b"

const FONT = ["DIN Offc Pro Medium", "Arial Unicode MS Bold"]

const TIME = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit", minute: "2-digit", hour12: false,
})
const clock = (iso: string) => {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? "" : TIME.format(d)
}

const ago = (iso?: string) => {
  if (!iso) return ""
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (!Number.isFinite(s) || s < 0) return ""
  if (s < 90) return `${Math.round(s)}s ago`
  if (s < 5400) return `${Math.round(s / 60)} min ago`
  return `${Math.round(s / 3600)} h ago`
}

const row = (k: string, v: unknown) =>
  v === null || v === undefined || v === ""
    ? ""
    : `<div class="ip-row"><span>${esc(k)}</span><span>${esc(v)}</span></div>`

const chip = (text: string, color: string) =>
  `<div class="ip-chip" style="background:${color}22;color:${color}">${esc(text)}</div>`

/** The recent-activity block. This is the answer to "what happened here": the
 *  agent's own audit lines and the runner's narration, merged and timestamped,
 *  rather than a static label. */
function activityBlock(entries: Activity[] | undefined, heading = "Recent") {
  if (!entries?.length) {
    return `<div class="ip-hr"></div><div class="ip-head">${heading}</div>
            <div class="ip-act"><span style="color:#64748b">Nothing recorded yet.</span></div>`
  }
  return (
    `<div class="ip-hr"></div><div class="ip-head">${heading}</div>` +
    entries
      .slice(0, 5)
      .map(
        (a) =>
          `<div class="ip-act"><i>${esc(clock(a.at))}</i><span>${esc(a.text)}</span></div>`
      )
      .join("")
  )
}

/** A ground distance as a circle radius in pixels at zoom 22, for an
 *  exponential zoom interpolation from 0 px at zoom 0. Mapbox GL renders
 *  512 px tiles, so a pixel at z22 is 40075016.686 / (512 · 2²²) m at the equator. */
function metresToPxAt22(metres: number, lat: number): number {
  const mPerPx = (40075016.686 / (512 * 2 ** 22)) * Math.cos((lat * Math.PI) / 180)
  return mPerPx > 0 ? metres / mPerPx : 0
}

function fc(features: Feature[]): FeatureCollection {
  return { type: "FeatureCollection", features }
}
const point = (lng: number, lat: number, props: Record<string, unknown>): Feature => ({
  type: "Feature", geometry: { type: "Point", coordinates: [lng, lat] }, properties: props,
})

/** Mapbox v3 types a hovered feature as `GeoJSONFeature`, which does not expose
 *  `properties` even though every runtime feature has it. The properties we read
 *  are ones this component wrote a few lines above, so the narrowing is safe;
 *  the cast is here rather than inline so there is one of it. */
const propsOf = (f: unknown): Record<string, unknown> =>
  (f as { properties?: Record<string, unknown> } | undefined)?.properties ?? {}

export function LiveMap({
  wards = [], incidents = [], resources = [], facilities = [], blocks = [],
  needs = [], activity, routes = [],
  route, routeLabel, me, center = [73.88, 18.58], zoom = 11.6, className,
  onPickIncident, followMe = false, recentreKey = 0, onUserMove,
  onPickWard,
  basemap = "streets", lightPreset = "night", markers = "pin", cluster = false,
  hoverCards = true, onSelect, selection, camera, padding, routeStatus, navigation = null,
  routeProgress, pulseCritical = false,
}: Props) {
  const container = useRef<HTMLDivElement>(null)
  const map = useRef<mapboxgl.Map | null>(null)
  const popup = useRef<mapboxgl.Popup | null>(null)
  const [ready, setReady] = useState(false)
  // The latest callbacks, for handlers registered once at mount.
  const pickWard = useRef(onPickWard)
  useEffect(() => {
    pickWard.current = onPickWard
  }, [onPickWard])
  const selectRef = useRef(onSelect)
  useEffect(() => {
    selectRef.current = onSelect
  }, [onSelect])
  const hoverRef = useRef(hoverCards)
  useEffect(() => {
    hoverRef.current = hoverCards
    if (!hoverCards) popup.current?.remove()
  }, [hoverCards])
  // Read once, at construction: these decide which sources and layers exist.
  const badges = markers === "badge"
  const night = basemap === "night"
  const pulses = useRef(new Map<string, mapboxgl.Marker>())
  const [, setIconsReady] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)

  useEffect(() => {
    injectCss()
    if (!container.current || map.current) return
    if (!TOKEN) {
      setFailed("VITE_MAPBOX_TOKEN is not set in frontend/indradhanu/.env.local")
      return
    }
    mapboxgl.accessToken = TOKEN
    const dark = document.documentElement.classList.contains("dark")
    try {
      const options = {
        container: container.current,
        style: night ? NIGHT_STYLE : dark ? BASEMAP.dark : BASEMAP.light,
        // Standard's own configuration: the light preset, and no 3D. Buildings
        // extruded over a disaster map hide the markers behind them and cost a
        // phone frames it does not have.
        ...(night
          ? { config: { basemap: { lightPreset, show3dObjects: false, showPointOfInterestLabels: true } } }
          : {}),
        center, zoom, attributionControl: true,
        // Speed over polish: no tile fade-in, no re-fetching tiles that are
        // merely past their HTTP expiry, a larger in-memory tile cache, flat
        // mercator (the globe view is slower and useless at city scale), and
        // no antialiasing pass.
        fadeDuration: 0,
        refreshExpiredTiles: false,
        maxTileCacheSize: 400,
        antialias: false,
        projection: "mercator",
        performanceMetricsCollection: false,
      } as mapboxgl.MapOptions
      const m = new mapboxgl.Map(options)
      map.current = m
      // The experience maps bring their own floating controls.
      if (!badges) m.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right")
      popup.current = new mapboxgl.Popup({
        closeButton: false, closeOnClick: false, offset: 14,
        className: "indra-pop", maxWidth: "340px",
      })

      m.on("error", (e) => {
        // A tile 401 is almost always a bad or restricted token, and the map
        // otherwise just sits there blank looking like our bug.
        const err = (e as unknown as { error?: { message?: string; status?: number } })?.error
        const msg = err?.message
        // A whole-word 401 only: a style error quoting a number such as
        // 0.17401 used to be reported as a rejected token.
        if (err?.status === 401 || (msg && (/\b401\b/.test(msg) || /unauthori[sz]ed/i.test(msg)))) {
          setFailed("Mapbox rejected the token (401). Check VITE_MAPBOX_TOKEN and its URL restrictions.")
        }
      })

      m.on("load", () => {
        // Register the drawn icons before any source gets data, so the first
        // poll does not land on a sprite that does not exist yet and fill the
        // console with missing-image warnings.
        // Pins are rasterised once per page (shared across every map) and
        // only in the tints actually drawn; a missing one is supplied on demand.
        m.on("styleimagemissing", (e: { id: string }) => provideIcon(m as unknown as Parameters<typeof provideIcon>[0], e.id))
        void registerIcons(m as unknown as Parameters<typeof registerIcons>[0])
          .then(() => setIconsReady(true))
          .catch(() => setIconsReady(true))

        // Ward shading is context, not content: it goes beneath the street and
        // place names. Painted over them it would hide the one thing a resident
        // uses to confirm they are on the right road, and the colourful basemap
        // would have bought nothing.
        //
        // On Mapbox Standard the basemap's layers are not in `getStyle()`, so
        // there is no label layer to insert before. Standard has slots for this
        // instead: "bottom" sits under the roads, "middle" over the roads and
        // under the labels.
        const belowLabels = night ? undefined : firstLabelLayer(m)
        const slot = (name: "bottom" | "middle") => (night ? { slot: name } : {})
        // Layer specs with a `slot` are valid on v3; the typings lag behind.
        const add = (spec: Record<string, unknown>, before?: string) =>
          m.addLayer(spec as unknown as mapboxgl.AnyLayer, before)
        const notCluster = ["!", ["has", "point_count"]]

        // Round badges are drawn centred; pins stand on their tip.
        const markerLayout = (stops: number[]) => ({
          "icon-image": ["get", "icon"],
          "icon-size": badges
            ? ["interpolate", ["linear"], ["zoom"], 10, 0.72, 13, 0.88, 16, 1.04]
            : ["interpolate", ["linear"], ["zoom"], ...stops],
          "icon-anchor": badges ? "center" : "bottom",
          "icon-allow-overlap": true, "icon-ignore-placement": true,
        })

        m.addSource("wards", { type: "geojson", data: fc([]) })
        add({
          id: "ward-fill", type: "fill", source: "wards", ...slot("bottom"),
          paint: { "fill-color": SEVERITY_COLOR },
        }, belowLabels)
        add({
          id: "ward-line", type: "line", source: "wards", ...slot("bottom"),
          paint: { "line-color": "#64748b", "line-opacity": 0.55, "line-width": 1 },
        }, belowLabels)

        // `lineMetrics` lets the travelled part of a route be dimmed and the
        // route be drawn in, rather than appearing all at once.
        // Predicted road risk (passability model, PCMC): red where the model
        // expects the road blocked for an ambulance within 30 min, and the router
        // avoids it; amber where risk is rising. Drawn under the unit routes.
        m.addSource("nav-risk", { type: "geojson", data: fc([]) })
        add({
          id: "nav-risk", type: "line", source: "nav-risk", ...slot("middle"),
          paint: {
            "line-color": ["case", [">=", ["get", "p"], 0.6], "#dc2626", "#f59e0b"],
            "line-width": ["interpolate", ["linear"], ["zoom"], 11, 2, 15, 6],
            "line-opacity": ["interpolate", ["linear"], ["get", "p"], 0.3, 0.45, 1, 0.95],
          },
        })

        // Where each unit has actually been (last 30 min of GPS / simulated
        // positions) and where it was rerouted, with the reason on hover.
        m.addSource("unit-trails", { type: "geojson", data: fc([]) })
        add({
          id: "trail-line", type: "line", source: "unit-trails", ...slot("middle"),
          filter: ["==", ["get", "kind"], "trail"],
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#38bdf8", "line-width": ["interpolate", ["linear"], ["zoom"], 11, 1.2, 15, 3],
                   "line-opacity": 0.55, "line-dasharray": [1, 1.5] },
        })
        add({
          id: "reroute-dot", type: "circle", source: "unit-trails", ...slot("middle"),
          filter: ["==", ["get", "kind"], "reroute"],
          paint: { "circle-radius": 5, "circle-color": "#a855f7", "circle-stroke-color": "#fff", "circle-stroke-width": 1.2 },
        })

        m.addSource("route", { type: "geojson", data: fc([]), lineMetrics: true })
        add({
          id: "route-casing", type: "line", source: "route", ...slot("middle"),
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#0f172a", "line-width": badges ? 10 : 9, "line-opacity": badges ? 0.55 : 0.35 },
        })
        add({
          id: "route-travelled", type: "line", source: "route", ...slot("middle"),
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#94a3b8", "line-width": 4, "line-opacity": badges ? 0.45 : 0 },
        })
        add({
          id: "route-line", type: "line", source: "route", ...slot("middle"),
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#22c55e", "line-width": badges ? 5 : 4 },
        })

        // Unit routes: the streets each committed vehicle is actually driving.
        // This was a dashed straight line from the dot to the incident, which
        // was honest about nothing: the unit was not taking that path and the
        // path did not exist.
        m.addSource("links", { type: "geojson", data: fc([]) })
        add({
          id: "link-casing", type: "line", source: "links", ...slot("middle"),
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": "#0b1220", "line-width": 6, "line-opacity": 0.5 },
        })
        add({
          id: "link-line", type: "line", source: "links", ...slot("middle"),
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": [
              "match", ["get", "status"],
              "on_site", "#10b981",
              "en_route", "#f59e0b",
              "#0ea5e9",
            ] as unknown as mapboxgl.Expression,
            "line-width": 3,
            "line-opacity": badges ? 0.7 : 0.9,
          },
        })

        // The selected thing, ringed. Beneath the markers so the marker itself
        // stays crisp; the ring is the larger shape around it.
        m.addSource("selection", { type: "geojson", data: fc([]) })
        add({
          id: "selection-halo", type: "circle", source: "selection",
          paint: {
            "circle-radius": 26, "circle-color": ["get", "colour"], "circle-opacity": 0.22,
            "circle-radius-transition": { duration: 300 },
          },
        })
        add({
          id: "selection-ring", type: "circle", source: "selection",
          paint: {
            "circle-radius": 19, "circle-color": "rgba(0,0,0,0)",
            "circle-stroke-width": 2.5, "circle-stroke-color": "#ffffff",
          },
        })

        const clusterOptions = (props?: Record<string, unknown>) =>
          cluster
            ? { cluster: true, clusterRadius: 46, clusterMaxZoom: 14, ...(props ? { clusterProperties: props } : {}) }
            : {}
        const clusterLayers = (source: string, ring: unknown) => {
          if (!cluster) return
          add({
            id: `${source}-cluster`, type: "circle", source,
            filter: ["has", "point_count"],
            paint: {
              "circle-color": "rgba(14,17,22,0.92)",
              "circle-radius": ["step", ["get", "point_count"], 15, 5, 18, 15, 22],
              "circle-stroke-width": 2.5, "circle-stroke-color": ring,
            },
          })
          add({
            id: `${source}-cluster-count`, type: "symbol", source,
            filter: ["has", "point_count"],
            layout: {
              "text-field": ["get", "point_count_abbreviated"],
              "text-font": FONT, "text-size": 12, "text-allow-overlap": true,
            },
            paint: { "text-color": "#ffffff" },
          })
        }

        m.addSource("facilities", { type: "geojson", data: fc([]), ...clusterOptions() })
        add({
          id: "facility-dot", type: "symbol", source: "facilities", filter: notCluster,
          layout: markerLayout([10, 0.8, 13, 1.15, 16, 1.45]),
        })
        // A ring for anything that has reported itself full or closed. Colour
        // alone is not enough when there are four kinds of facility on screen.
        add({
          id: "facility-alarm", type: "circle", source: "facilities",
          filter: ["all", notCluster, ["in", ["get", "status"], ["literal", ["full", "closed"]]]],
          paint: {
            "circle-radius": badges ? 15 : 18, "circle-color": "rgba(0,0,0,0)",
            "circle-stroke-width": 2, "circle-stroke-color": "#ef4444",
          },
        }, "facility-dot")
        clusterLayers("facilities", MAP.shelter)

        m.addSource("blocks", { type: "geojson", data: fc([]) })
        // How far a closure reaches, in metres on the ground at every zoom.
        add({
          id: "block-zone", type: "circle", source: "blocks",
          filter: [">", ["coalesce", ["get", "pxAt22"], 0], 0],
          paint: {
            "circle-radius": [
              "interpolate", ["exponential", 2], ["zoom"],
              0, 0, 22, ["coalesce", ["get", "pxAt22"], 0],
            ],
            "circle-color": MAP.block, "circle-opacity": 0.12,
            "circle-stroke-width": 1, "circle-stroke-color": MAP.block, "circle-stroke-opacity": 0.5,
          },
        })
        add({
          id: "block-dot", type: "symbol", source: "blocks",
          layout: markerLayout([10, 0.9, 14, 1.35]),
        })

        m.addSource("incidents", {
          type: "geojson", data: fc([]),
          ...clusterOptions({ maxSev: ["max", ["get", "severity"]] }),
        })
        // The halo still carries severity and merge count — it is the thing you
        // read across a whole city — and the icon on top of it carries what kind
        // of hazard it is, which is the thing you read once you have found it.
        add({
          id: "incident-halo", type: "circle", source: "incidents", filter: notCluster,
          paint: {
            "circle-radius": badges
              ? ["+", 15, ["*", 2, ["min", ["get", "reportCount"], 8]]]
              : ["+", 17, ["*", 3.5, ["get", "reportCount"]]],
            "circle-color": ["get", "colour"],
            "circle-opacity": badges ? 0.14 : 0.18,
            "circle-stroke-width": 1, "circle-stroke-color": ["get", "colour"],
            "circle-stroke-opacity": 0.4,
          },
        })
        add({
          id: "incident-dot", type: "symbol", source: "incidents", filter: notCluster,
          layout: markerLayout([10, 0.95, 13, 1.35, 16, 1.7]),
        })
        // The merge count sits beside the icon rather than inside it, so the
        // hazard stays legible. It is the whole argument for deduplication and
        // it should not be hidden behind a picture of a tree.
        add({
          id: "incident-count", type: "symbol", source: "incidents",
          filter: ["all", notCluster, [">", ["get", "reportCount"], 1]],
          layout: {
            "text-field": ["to-string", ["get", "reportCount"]],
            "text-font": FONT, "text-size": badges ? 11 : 13, "text-allow-overlap": true,
            "text-offset": badges ? [1.15, -1.15] : [1.3, -2.3], "text-anchor": "left",
          },
          paint: {
            "text-color": "#ffffff",
            "text-halo-color": "rgba(9,12,20,0.9)", "text-halo-width": 1.4,
          },
        })
        clusterLayers("incidents", [
          "step", ["coalesce", ["get", "maxSev"], 0],
          MAP.sev3, 4, MAP.sev4, CRITICAL_SEVERITY, MAP.critical,
        ])

        // Critical incidents live in their own source so clustering can never
        // fold one into a number. Only used when clustering is on.
        m.addSource("incidents-critical", { type: "geojson", data: fc([]) })
        add({
          id: "critical-halo", type: "circle", source: "incidents-critical",
          paint: {
            "circle-radius": 20, "circle-color": MAP.critical, "circle-opacity": 0.2,
            "circle-stroke-width": 1.5, "circle-stroke-color": MAP.critical, "circle-stroke-opacity": 0.6,
          },
        })
        add({
          id: "critical-dot", type: "symbol", source: "incidents-critical",
          layout: markerLayout([10, 1.05, 13, 1.45, 16, 1.8]),
        })

        m.addSource("resources", { type: "geojson", data: fc([]), ...clusterOptions() })
        // Status as a ring around the badge, so the badge's own colour can say
        // which service it is. Pins carry status in their fill, as before.
        add({
          id: "resource-status", type: "circle", source: "resources",
          filter: ["all", notCluster, ["has", "ring"]],
          paint: {
            "circle-radius": 15.5, "circle-color": "rgba(0,0,0,0)",
            "circle-stroke-width": 2.5, "circle-stroke-color": ["get", "ring"],
          },
        })
        add({
          id: "resource-dot", type: "symbol", source: "resources", filter: notCluster,
          layout: markerLayout([10, 0.9, 13, 1.2, 16, 1.5]),
        })
        clusterLayers("resources", "#94a3b8")

        m.addSource("me", { type: "geojson", data: fc([]) })
        if (badges) {
          // The blue dot: accuracy circle in metres, a white keyline, a solid core.
          add({
            id: "me-accuracy", type: "circle", source: "me",
            filter: [">", ["coalesce", ["get", "pxAt22"], 0], 0],
            paint: {
              "circle-radius": [
                "interpolate", ["exponential", 2], ["zoom"],
                0, 0, 22, ["coalesce", ["get", "pxAt22"], 0],
              ],
              "circle-color": MAP.you, "circle-opacity": 0.12,
              "circle-stroke-width": 1, "circle-stroke-color": MAP.you, "circle-stroke-opacity": 0.35,
            },
          })
          add({
            id: "me-halo", type: "circle", source: "me",
            paint: { "circle-radius": 9.5, "circle-color": "#ffffff", "circle-blur": 0.1 },
          })
          add({
            id: "me-dot", type: "circle", source: "me",
            paint: { "circle-radius": 6.5, "circle-color": MAP.you },
          })
        } else {
          add({
            id: "me-halo", type: "circle", source: "me",
            paint: { "circle-radius": 26, "circle-color": "#8b5cf6", "circle-opacity": 0.18 },
          })
          add({
            id: "me-dot", type: "symbol", source: "me",
            layout: {
              "icon-image": ["get", "icon"],
              "icon-size": 1.4,
              "icon-anchor": "bottom",
              "icon-allow-overlap": true, "icon-ignore-placement": true,
            },
          })
        }

        // Where each route ends, which is the hazard the unit is going to.
        // A line that fades out into a dot is a line going nowhere in
        // particular; the endpoint names its destination.
        m.addSource("endpoints", { type: "geojson", data: fc([]) })
        add({
          id: "endpoint-ring", type: "circle", source: "endpoints",
          paint: {
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 11, 15, 20],
            "circle-color": "rgba(0,0,0,0)",
            "circle-stroke-width": 2.4,
            "circle-stroke-color": ["get", "colour"],
            "circle-stroke-opacity": 0.95,
          },
        })
        add({
          id: "endpoint-label", type: "symbol", source: "endpoints",
          minzoom: 12,
          // On the experience maps a label for every destination is clutter:
          // the selected one is named in the panel.
          ...(badges ? { layout: { visibility: "none" } } : {}),
          ...(badges ? {} : {
            layout: {
              "text-field": ["get", "label"], "text-font": FONT,
              "text-size": 12, "text-offset": [0, 1.9], "text-anchor": "top",
              "text-max-width": 12,
            },
          }),
          paint: {
            "text-color": "#e6edf7",
            "text-halo-color": "rgba(9,12,20,0.92)", "text-halo-width": 1.6,
          },
        })

        // One handler, one priority order.
        //
        // Per-layer `mousemove` handlers were the bug: Mapbox fires them for
        // every layer under the cursor independently, and a ward polygon covers
        // the entire city, so the ward's handler fired last and overwrote
        // whatever the incident or the unit had just put in the popup. Nothing
        // on top of a ward was ever hoverable.
        //
        // Querying once and taking the first match in a deliberate order fixes
        // it properly: the ward is the fallback rather than the winner, and the
        // order here is "smallest and most specific first", which is also the
        // order somebody's attention moves in.
        const HOVER_ORDER = [
          "me-dot", "critical-dot", "incident-dot", "resource-dot", "facility-dot",
          "block-dot", "reroute-dot", "endpoint-ring", "route-line", "trail-line", "link-line", "ward-fill",
        ]
        const present = () => HOVER_ORDER.filter((id) => m.getLayer(id))

        m.on("mousemove", (e) => {
          if (!hoverRef.current) return
          const hits = m.queryRenderedFeatures(e.point, { layers: present() })
          if (!hits.length) {
            m.getCanvas().style.cursor = ""
            popup.current?.remove()
            return
          }
          const rank = new Map(HOVER_ORDER.map((id, i) => [id, i] as const))
          let best = hits[0]
          for (const f of hits) {
            const a = rank.get(String(f.layer?.id)) ?? 99
            const b = rank.get(String(best.layer?.id)) ?? 99
            if (a < b) best = f
          }
          const html = propsOf(best).tip
          if (!html) {
            m.getCanvas().style.cursor = ""
            popup.current?.remove()
            return
          }
          m.getCanvas().style.cursor =
            best.layer?.id === "ward-fill" ? "" : "pointer"
          popup.current?.setLngLat(e.lngLat).setHTML(String(html)).addTo(m)
        })

        m.on("mouseout", () => {
          m.getCanvas().style.cursor = ""
          popup.current?.remove()
        })

        // A cluster opens to the zoom at which it comes apart.
        const CLUSTERS = ["incidents-cluster", "resources-cluster", "facilities-cluster"]
        const expandCluster = (e: mapboxgl.MapMouseEvent): boolean => {
          const layers = CLUSTERS.filter((id) => m.getLayer(id))
          if (!layers.length) return false
          const hit = m.queryRenderedFeatures(e.point, { layers })[0]
          if (!hit) return false
          const src = m.getSource(String(hit.layer?.source ?? "")) as mapboxgl.GeoJSONSource | undefined
          const clusterId = Number(propsOf(hit).cluster_id)
          const at = (hit.geometry as unknown as { coordinates: [number, number] }).coordinates
          src?.getClusterExpansionZoom(clusterId, (err, z) => {
            if (err || z === null || z === undefined) return
            m.easeTo({ center: at, zoom: z + 0.4, duration: 700 })
          })
          return true
        }

        const SELECTABLE: [string, MapSelection["kind"]][] = [
          ["critical-dot", "incident"], ["incident-dot", "incident"],
          ["resource-dot", "resource"], ["facility-dot", "facility"],
          ["block-dot", "block"], ["endpoint-ring", "incident"],
        ]

        // Clicking picks the incident under the cursor, whether that was the
        // hazard icon itself or the ring at the end of a route pointing at it.
        m.on("click", (e) => {
          if (expandCluster(e)) return
          if (selectRef.current) {
            const layers = SELECTABLE.map(([id]) => id).filter((id) => m.getLayer(id))
            const hits = layers.length ? m.queryRenderedFeatures(e.point, { layers }) : []
            for (const [layer, kind] of SELECTABLE) {
              const hit = hits.find((h) => h.layer?.id === layer)
              const id = hit ? propsOf(hit).incidentId ?? propsOf(hit).id : null
              if (id) {
                selectRef.current({ kind, id: String(id) })
                return
              }
            }
            selectRef.current(null)
            return
          }
          const layers = ["incident-dot", "critical-dot", "endpoint-ring"].filter((id) => m.getLayer(id))
          const hits = layers.length ? m.queryRenderedFeatures(e.point, { layers }) : []
          const id = hits.length ? propsOf(hits[0]).incidentId ?? propsOf(hits[0]).id : null
          if (id && onPickIncident) {
            onPickIncident(String(id))
            return
          }
          // Nothing more specific under the cursor: the ward itself.
          if (pickWard.current && m.getLayer("ward-fill")) {
            const ward = m.queryRenderedFeatures(e.point, { layers: ["ward-fill"] })
            const wid = ward.length ? propsOf(ward[0]).id : null
            if (wid) pickWard.current(String(wid))
          }
        })

        // Pointer cursor over anything clickable, even with hover cards off.
        m.on("mousemove", (e) => {
          if (hoverRef.current) return
          const layers = [...SELECTABLE.map(([id]) => id), ...CLUSTERS].filter((id) => m.getLayer(id))
          const over = layers.length ? m.queryRenderedFeatures(e.point, { layers }).length > 0 : false
          m.getCanvas().style.cursor = over ? "pointer" : ""
        })

        setReady(true)
      })
    } catch (err) {
      setFailed(err instanceof Error ? err.message : String(err))
    }

    return () => {
      map.current?.remove()
      map.current = null
    }
    // Intentionally mounts once. Data arrives through the effects below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const set = (id: string, data: FeatureCollection) => {
    const src = map.current?.getSource(id) as mapboxgl.GeoJSONSource | undefined
    src?.setData(data)
  }

  useEffect(() => {
    if (!ready) return
    set("wards", fc(
      wards
        .filter((w) => (w.boundary?.length ?? 0) >= 3)
        .map((w) => {
          const scored = w.score !== null && w.score !== undefined
          const sev = w.severity ?? 0
          const tip =
            `<div class="ip-title">${esc(w.name)}</div>` +
            `<div class="ip-sub">Ward ${esc(w.number ?? "—")}</div>` +
            (scored
              ? chip(
                  `Severity ${sev} · flood risk ${(w.score! * 100).toFixed(0)}%`,
                  sev >= 4 ? "#ef4444" : sev >= 3 ? "#f59e0b" : "#38bdf8"
                )
              : chip("Not scored yet", "#94a3b8")) +
            row("Population", w.population?.toLocaleString()) +
            row("Estimated exposed", w.populationAtRisk?.toLocaleString()) +
            activityBlock(activity?.get(w.id), "In this ward")
          return {
            type: "Feature" as const,
            geometry: { type: "Polygon" as const, coordinates: [w.boundary as number[][]] },
            properties: { id: w.id, severity: w.severity ?? 0, tip },
          }
        })
    ))
  }, [ready, wards, activity])

  useEffect(() => {
    if (!ready) return
    const needsBy = new Map<string, NeedFeature[]>()
    for (const n of needs) {
      const list = needsBy.get(n.incidentId)
      if (list) list.push(n)
      else needsBy.set(n.incidentId, [n])
    }
    const features = incidents.map((i) => {
      const mine = needsBy.get(i.id) ?? []
      const short = mine.filter((n) => n.met < n.required)
      const tip =
        `<div class="ip-title">${esc(i.title)}</div>` +
        `<div class="ip-sub">${esc(i.category.replace(/_/g, " "))}` +
        (i.street ? ` · ${esc(i.street)}` : "") +
        (i.createdAt ? ` · opened ${esc(ago(i.createdAt))}` : "") + `</div>` +
        chip(
          `Severity ${i.severity}${i.status ? ` · ${i.status.replace(/_/g, " ")}` : ""}`,
          i.severity >= 5 ? "#ef4444" : i.severity >= 4 ? "#f97316" : "#eab308"
        ) +
        row("Reports merged into it", i.reportCount) +
        (i.reportCount > 1
          ? row("Duplicate dispatches avoided", i.reportCount - 1)
          : "") +
        row("Confidence", i.confidence !== undefined
          ? `${(i.confidence * 100).toFixed(0)}%` : null) +
        row("Units committed", i.unitsEnRoute ?? 0) +
        (mine.length
          ? `<div class="ip-hr"></div><div class="ip-head">Needs</div>` +
            mine
              .map(
                (n) =>
                  `<div class="ip-row"><span>${esc(n.capability.replace(/_/g, " "))}</span>` +
                  `<span class="${n.met >= n.required ? "ip-ok" : "ip-warn"}">` +
                  `${n.met}/${n.required}</span></div>`
              )
              .join("")
          : "") +
        (short.length
          ? `<div class="ip-act ip-warn"><span>Short by ` +
            `${short.reduce((s, n) => s + (n.required - n.met), 0)} unit(s).</span></div>`
          : "") +
        activityBlock(activity?.get(i.id), "What happened") +
        `<div class="ip-act"><span style="color:#64748b">Click to open it.</span></div>`
      const colour = badges ? incidentColour(i.severity) : severityColour(i.severity)
      return point(i.location[0], i.location[1], {
        id: i.id, severity: i.severity, reportCount: i.reportCount, tip,
        colour,
        icon: badges
          ? badgeName(hazardIcon(i.category), colour)
          : imageName(hazardIcon(i.category), colour),
      })
    })
    // Clustering would fold a critical incident into a number. It gets its own
    // unclustered source instead, drawn above everything else of its kind.
    const critical = (f: Feature) => Number(propsOf(f).severity) >= CRITICAL_SEVERITY
    set("incidents", fc(cluster ? features.filter((f) => !critical(f)) : features))
    set("incidents-critical", fc(cluster ? features.filter(critical) : []))

    // The pulse. DOM markers rather than a per-frame paint update: the browser
    // animates CSS on the compositor, so a pulsing SOS costs no map redraws.
    const live = new Set<string>()
    if (pulseCritical) {
      for (const i of incidents) {
        if (i.severity < CRITICAL_SEVERITY) continue
        live.add(i.id)
        const held = pulses.current.get(i.id)
        if (held) {
          held.setLngLat(i.location)
        } else if (map.current) {
          const el = document.createElement("div")
          el.className = "indra-pulse"
          el.setAttribute("aria-hidden", "true")
          pulses.current.set(
            i.id,
            new mapboxgl.Marker({ element: el, anchor: "center" }).setLngLat(i.location).addTo(map.current),
          )
        }
      }
    }
    for (const [id, marker] of pulses.current) {
      if (!live.has(id)) {
        marker.remove()
        pulses.current.delete(id)
      }
    }
    // `badges` and `cluster` are fixed at mount; they are read, not tracked.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, incidents, needs, activity, pulseCritical])

  useEffect(() => {
    if (!ready) return
    set("resources", fc(resources.map((r) => {
      const colour = statusColour(r.status)
      const tip =
        `<div class="ip-title">${esc(r.label)}</div>` +
        `<div class="ip-sub">${esc(r.operator ?? "")}` +
        (r.capacity ? ` · capacity ${esc(r.capacity)}` : "") + `</div>` +
        chip(r.status.replace(/_/g, " "), colour) +
        row("Tasked to", r.assignedTo) +
        row("ETA", r.etaMinutes ? `${r.etaMinutes} min` : null) +
        row("Distance", r.distanceKm ? `${r.distanceKm.toFixed(1)} km` : null) +
        row("Task state", r.assignmentStatus?.replace(/_/g, " ")) +
        (r.statusNote ? row("Crew note", r.statusNote) : "") +
        (r.unavailableReason
          ? `<div class="ip-act ip-warn"><span>${esc(r.unavailableReason)}</span></div>`
          : "") +
        (r.capabilities?.length
          ? `<div class="ip-hr"></div><div class="ip-head">Can do</div>` +
            `<div class="ip-act"><span>${esc(
              r.capabilities.map((c) => c.replace(/_/g, " ")).join(", ")
            )}</span></div>`
          : "") +
        activityBlock(activity?.get(r.id), "Agent actions")
      return point(r.location[0], r.location[1], {
        id: r.id, status: r.status, tip,
        ...(badges
          ? { icon: badgeName(kindIcon(r.kind), serviceColour(r.kind)), ring: statusRing(r.status) }
          : { icon: imageName(kindIcon(r.kind), statusColour(r.status)) }),
      })
    })))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, resources, activity])

  useEffect(() => {
    if (!ready) return
    const drawable = routes.filter((r) => r.path.length > 1)

    set("links", fc(
      drawable.map((r) => {
        const turns = (r.steps ?? []).filter((s) => s.street)
        const tip =
          `<div class="ip-title">${esc(r.resourceLabel)} &rarr; ${esc(r.incidentTitle)}</div>` +
          `<div class="ip-sub">${esc((r.engine ?? "route").replace(/-/g, " "))}` +
          ` · ${Math.round((r.progress ?? 0) * 100)}% of the way</div>` +
          chip(r.status.replace(/_/g, " "), statusColour(r.status)) +
          row("Distance", r.distanceKm ? `${r.distanceKm.toFixed(1)} km` : null) +
          row("ETA", r.etaMinutes ? `${r.etaMinutes} min` : null) +
          (turns.length
            ? `<div class="ip-hr"></div><div class="ip-head">Streets</div>` +
              turns.slice(0, 5).map((s) =>
                `<div class="ip-act"><i>${Math.round(s.distanceM)}m</i>` +
                `<span>${esc(s.instruction)}</span></div>`).join("")
            : "")
        return {
          type: "Feature" as const,
          geometry: { type: "LineString" as const, coordinates: r.path as number[][] },
          properties: { id: r.id, status: r.status, incidentId: r.incidentId, tip },
        }
      })
    ))

    // Where each line ends, and what it ends at.
    //
    // A route that just fades out into the dot soup is a line going nowhere in
    // particular. Marking the last vertex with a ring in the route's own colour,
    // labelled with the incident, makes "this unit is going to that hazard"
    // readable without hovering anything. One ring per destination, not one per
    // unit, because three boats converging on one rescue is one place.
    const byDestination = new Map<string, { at: [number, number]; title: string; n: number; status: string; incidentId: string }>()
    for (const r of drawable) {
      const last = r.path[r.path.length - 1] as [number, number]
      const key = r.incidentId || `${last[0].toFixed(5)},${last[1].toFixed(5)}`
      const held = byDestination.get(key)
      if (held) {
        held.n += 1
        if (r.status === "on_site") held.status = "on_site"
      } else {
        byDestination.set(key, {
          at: last, title: r.incidentTitle, n: 1,
          status: r.status, incidentId: r.incidentId,
        })
      }
    }

    set("endpoints", fc(
      [...byDestination.values()].map((d) =>
        point(d.at[0], d.at[1], {
          incidentId: d.incidentId,
          colour: statusColour(d.status),
          label: d.n > 1 ? `${d.title} · ${d.n} units` : d.title,
          tip:
            `<div class="ip-title">${esc(d.title)}</div>` +
            `<div class="ip-sub">Destination of ${d.n} committed unit${d.n === 1 ? "" : "s"}</div>` +
            chip(d.status.replace(/_/g, " "), statusColour(d.status)) +
            activityBlock(activity?.get(d.incidentId), "What happened"),
        })
      )
    ))
  }, [ready, routes, activity])

  // Live road risk from the passability model, every minute (console only:
  // the endpoint is for staff, and residents get guidance, not raw risk).
  useEffect(() => {
    if (!ready || !window.location.pathname.startsWith("/admin")) return
    let alive = true
    const load = async () => {
      try {
        const r = await request<{ available: boolean; segments?: GeoJSON.FeatureCollection }>("/nav/risk", { toast: false })
        const src = map.current?.getSource("nav-risk") as mapboxgl.GeoJSONSource | undefined
        if (alive && r.available && r.segments && src) src.setData(r.segments)
      } catch { /* risk is an overlay; the map works without it */ }
    }
    void load()
    const id = setInterval(() => void load(), 60_000)
    return () => { alive = false; clearInterval(id) }
  }, [ready])

  // Unit trails and reroutes, every 10 s (console only).
  useEffect(() => {
    if (!ready || !window.location.pathname.startsWith("/admin")) return
    let alive = true
    const load = async () => {
      try {
        const region = getRegion() === "ncr" ? "ncr" : "pune"
        const r = await request<GeoJSON.FeatureCollection>("/units/trails", { toast: false, query: { minutes: 30, region } })
        for (const f of r.features) {
          const p = (f.properties ?? {}) as Record<string, string>
          p.tip = p.kind === "reroute"
            ? `<div class="ip-title">Rerouted · ${esc(p.unit)}</div><div class="ip-sub">${esc(new Date(p.at).toLocaleTimeString())}</div><div>${esc(p.reason)}</div>`
            : `<div class="ip-title">${esc(p.label)}</div><div class="ip-sub">trail, last 30 min · ${esc(String(p.status).replace(/_/g, " "))}</div>`
          f.properties = p
        }
        const src = map.current?.getSource("unit-trails") as mapboxgl.GeoJSONSource | undefined
        if (alive && src) src.setData(r)
      } catch { /* trails are an overlay */ }
    }
    void load()
    const id = setInterval(() => void load(), 10_000)
    return () => { alive = false; clearInterval(id) }
  }, [ready])

  useEffect(() => {
    if (!ready) return
    set("facilities", fc(facilities.map((f) => {
      const spare = f.capacity === null || f.capacity === 0 ? null
        : Math.max(0, f.capacity - (f.occupancy ?? 0))
      const colour = LIFELINE_COLOUR[f.kind] ?? "#0d9488"
      const stock = Object.entries(f.supplies ?? {}).filter(
        ([, v]) => typeof v === "number"
      ) as [string, number][]
      // What is actually on the shelf. A relief centre with no food is a
      // building, and that is a thing an operator must be able to see from the
      // map rather than from a report somebody files later.
      const lowest = stock.length
        ? stock.reduce((a, b) => (b[1] < a[1] ? b : a))
        : null

      const tip =
        `<div class="ip-title">${esc(f.name)}</div>` +
        `<div class="ip-sub">${esc(f.kindLabel ?? f.kind.replace(/_/g, " "))}</div>` +
        chip(
          f.status.replace(/_/g, " "),
          f.status === "full" || f.status === "closed" ? "#ef4444"
            : f.status === "limited" ? "#f59e0b" : colour
        ) +
        (f.capacity ? row("Capacity", f.capacity) : "") +
        (f.occupancy ? row("Occupied", f.occupancy) : "") +
        (spare !== null
          ? `<div class="ip-row"><span>Places free</span>` +
            `<span class="${spare > 0 ? "ip-ok" : "ip-warn"}">${spare}</span></div>`
          : "") +
        (f.servedPerHour ? row("Serving", `${f.servedPerHour}/hour`) : "") +
        (stock.length
          ? `<div class="ip-hr"></div><div class="ip-head">Stock on hand</div>` +
            stock
              .map(([line, qty]) =>
                `<div class="ip-row"><span>${esc(line.replace(/_/g, " "))}</span>` +
                `<span class="${qty <= 0 ? "ip-warn" : ""}">` +
                `${qty.toLocaleString()}</span></div>`)
              .join("")
          : "") +
        (lowest && lowest[1] <= 0
          ? `<div class="ip-act ip-warn"><span>Out of ` +
            `${esc(lowest[0].replace(/_/g, " "))}. The citizen agent has stopped ` +
            `sending anybody here for it.</span></div>`
          : "") +
        (f.acceptsCasualties === false && f.kind === "hospital"
          ? `<div class="ip-act ip-warn"><span>Not accepting casualties.</span></div>`
          : "") +
        activityBlock(activity?.get(f.id), "Reported by the crew")

      return point(f.location[0], f.location[1], {
        id: f.id, status: f.status, tip,
        icon: badges
          ? badgeName(lifelineIcon(f.kind), placeColour(f.kind))
          : imageName(lifelineIcon(f.kind), colour),
      })
    })))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, facilities, activity])

  useEffect(() => {
    if (!ready) return
    set("blocks", fc(blocks.map((b) =>
      point(b.location[0], b.location[1], {
        id: b.id,
        icon: badges ? badgeName("ui-block", MAP.block) : imageName("ui-block", "#dc2626"),
        pxAt22: b.radiusM ? metresToPxAt22(b.radiusM, b.location[1]) : 0,
        tip:
          `<div class="ip-title">Road blocked</div>` +
          `<div class="ip-sub">${esc(b.reason)}</div>` +
          chip("Routing around it", "#ef4444") +
          row("Reported by", b.reportedBy) +
          activityBlock(activity?.get(b.id), "Since"),
      })
    )))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, blocks, activity])

  useEffect(() => {
    if (!ready) return
    set("route", route && route.length > 1
      ? fc([{
          type: "Feature",
          geometry: { type: "LineString", coordinates: route },
          properties: {
            tip:
              `<div class="ip-title">${esc(routeLabel ?? "Recommended route")}</div>` +
              `<div class="ip-sub">Scored on hazard exposure first, distance second.</div>`,
          },
        }])
      : fc([]))
  }, [ready, route, routeLabel])

  /** How much the highlighted route can be trusted, said in the line itself:
   *  green on real roads, amber where it passes reported hazards, and dashed
   *  amber where it is not a road route at all. */
  useEffect(() => {
    const m = map.current
    if (!ready || !m?.getLayer("route-line")) return
    const colour = routeStatus === "uncertain" ? MAP.uncertain
      : routeStatus === "caution" ? MAP.caution : MAP.route
    m.setPaintProperty("route-line", "line-color", colour)
    m.setPaintProperty(
      "route-line", "line-dasharray",
      routeStatus === "uncertain" ? [1.4, 1.6] : (null as unknown as number[]),
    )
  }, [ready, routeStatus])

  /** The route drawing itself in, start to destination, when a new one
   *  arrives; then the travelled part dimmed as the person moves along it.
   *  Dashed (uncertain) lines skip both: trimming and dashing do not combine. */
  const routeSig = route && route.length > 1
    ? `${route.length}:${route[0].join(",")}:${route[route.length - 1].join(",")}`
    : ""
  const revealing = useRef(false)
  useEffect(() => {
    const m = map.current
    if (!ready || !m?.getLayer("route-line") || !routeSig || !badges) return
    if (routeStatus === "uncertain") return
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    if (reduce) return
    let frame = 0
    const start = performance.now()
    revealing.current = true
    const step = (now: number) => {
      // A frame's timestamp can precede `start`, which made this negative.
      const t = Math.max(0, Math.min(1, (now - start) / 750))
      const eased = 1 - Math.pow(1 - t, 3)
      try {
        m.setPaintProperty("route-line", "line-trim-offset", [eased, 1])
      } catch { /* style mid-reload */ }
      if (t < 1) frame = requestAnimationFrame(step)
      else revealing.current = false
    }
    frame = requestAnimationFrame(step)
    return () => {
      cancelAnimationFrame(frame)
      revealing.current = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, routeSig])

  useEffect(() => {
    const m = map.current
    if (!ready || !m?.getLayer("route-line") || !badges || revealing.current) return
    const p = routeStatus === "uncertain" ? 0 : Math.max(0, Math.min(1, routeProgress ?? 0))
    try {
      m.setPaintProperty("route-line", "line-trim-offset", [0, p])
    } catch { /* style mid-reload */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, routeProgress, routeStatus, routeSig])

  // ---- selection, camera, padding, light: the experience maps' camera work.

  const selLng = selection?.lng
  const selLat = selection?.lat
  const selColour = selection?.colour ?? MAP.you
  useEffect(() => {
    if (!ready) return
    set("selection", selLng !== undefined && selLat !== undefined
      ? fc([point(selLng, selLat, { colour: selColour })])
      : fc([]))
  }, [ready, selLng, selLat, selColour])

  const paddingKey = padding ? `${padding.top},${padding.right},${padding.bottom},${padding.left}` : ""
  const paddingRef = useRef(padding)
  useEffect(() => {
    paddingRef.current = padding
  })
  useEffect(() => {
    const m = map.current
    if (!ready || !m || !paddingRef.current) return
    // Never cut a camera move short: a padding change that lands mid-flight
    // (the sheet resizing as a selection opens) waits for it to finish.
    const apply = () => {
      if (paddingRef.current) m.easeTo({ padding: paddingRef.current, duration: 260 })
    }
    if (m.isMoving()) {
      m.once("moveend", apply)
      return () => {
        m.off("moveend", apply)
      }
    }
    apply()
  }, [ready, paddingKey])

  /** Smooth, never snapping: a fly for a long way, an ease for a short one. */
  const cameraKey = camera?.key
  const cameraRef = useRef(camera)
  useEffect(() => {
    cameraRef.current = camera
  })
  useEffect(() => {
    const m = map.current
    const c = cameraRef.current
    if (!ready || !m || !c || cameraKey === undefined) return
    const pad = paddingRef.current
    if (c.bounds) {
      m.fitBounds(c.bounds, {
        padding: {
          top: (pad?.top ?? 0) + 56, bottom: (pad?.bottom ?? 0) + 56,
          left: (pad?.left ?? 0) + 56, right: (pad?.right ?? 0) + 56,
        },
        maxZoom: c.zoom ?? 16.5,
        // An overview is flat and north-up, even straight out of navigation.
        pitch: 0,
        bearing: 0,
        duration: 900,
      })
      return
    }
    if (c.zoomBy) {
      m.easeTo({ zoom: m.getZoom() + c.zoomBy, duration: 250 })
      return
    }
    if (!c.center) return
    const here = m.getCenter()
    const far = Math.abs(here.lng - c.center[0]) + Math.abs(here.lat - c.center[1]) > 0.05
    const zoomTo = c.zoom ?? Math.max(m.getZoom(), 15)
    const withPad = pad ? { padding: pad } : {}
    const level = { pitch: 0, bearing: 0 }
    if (far) m.flyTo({ center: c.center, zoom: zoomTo, speed: 1.6, curve: 1.3, essential: true, ...level, ...withPad })
    else m.easeTo({ center: c.center, zoom: zoomTo, duration: 650, ...level, ...withPad })
  }, [ready, cameraKey])

  useEffect(() => {
    const m = map.current as unknown as {
      setConfigProperty?: (i: string, k: string, v: unknown) => void
    } | null
    if (!ready || !night || !m?.setConfigProperty) return
    try {
      m.setConfigProperty("basemap", "lightPreset", lightPreset)
    } catch { /* not a Standard style */ }
  }, [ready, night, lightPreset])

  useEffect(() => {
    const held = pulses.current
    return () => {
      for (const marker of held.values()) marker.remove()
      held.clear()
    }
  }, [])

  // Keyed on the coordinates rather than on the `me` object. Callers build that
  // object inline in their JSX, so it was a new value on every render and this
  // effect re-ran — and re-issued an `easeTo` — several times a second whether
  // or not the person had moved a millimetre.
  const meLng = me?.lng
  const meLat = me?.lat
  const navActive = Boolean(navigation)
  // Whole degrees: GPS heading jitters by fractions, and each change is a camera move.
  const navHeading = navigation?.heading == null ? null : Math.round(navigation.heading)
  const meLabel = me?.label
  const meAccuracy = me?.accuracyM ?? null
  useEffect(() => {
    if (!ready) return
    const here = meLng !== undefined && meLat !== undefined
    set("me", here
      ? fc([point(meLng, meLat, {
          icon: imageName("ui-me", "#8b5cf6"),
          pxAt22: meAccuracy ? metresToPxAt22(meAccuracy, meLat) : 0,
          tip: `<div class="ip-title">${esc(meLabel ?? "You")}</div>` +
               `<div class="ip-sub">${meLat.toFixed(5)}, ${meLng.toFixed(5)}</div>`,
        })])
      : fc([]))
    const m = map.current
    if (here && followMe && m) {
      if (navActive) {
        // Driving view: close, tilted, heading-up, with the person low on the
        // screen so most of the map shows what is ahead. Linear easing over
        // about one GPS interval makes successive fixes read as motion.
        const h = m.getContainer().clientHeight
        m.easeTo({
          center: [meLng, meLat],
          zoom: Math.max(17, Math.min(18, m.getZoom())),
          pitch: 55,
          bearing: navHeading ?? m.getBearing(),
          offset: [0, Math.round(h * 0.22)],
          duration: 900,
          easing: (t) => t,
          essential: true,
        })
      } else {
        m.easeTo({ center: [meLng, meLat], duration: 400 })
      }
    }
  }, [ready, meLng, meLat, meLabel, meAccuracy, followMe, recentreKey, navActive, navHeading])

  // Leaving navigation: flat, north up, zoomed out, centred on the person.
  const wasNavigating = useRef(false)
  useEffect(() => {
    const m = map.current
    if (!ready || !m) return
    if (wasNavigating.current && !navActive) {
      m.easeTo({
        pitch: 0,
        bearing: 0,
        zoom: Math.min(m.getZoom(), 14.3),
        ...(meLng !== undefined && meLat !== undefined ? { center: [meLng, meLat] as [number, number] } : {}),
        offset: [0, 0],
        duration: 1100,
        essential: true,
      })
    }
    wasNavigating.current = navActive
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only on the transition
  }, [ready, navActive])

  /** A touch or a scroll on the canvas is the person taking the camera.
   *
   *  Read from the DOM rather than from Mapbox's `dragstart` / `zoomstart`.
   *  Those fire for our own `easeTo` as well as for a human, so they need an
   *  `originalEvent` check to tell the two apart — and `zoomstart`'s event type
   *  does not declare that field, so the check does not typecheck. A
   *  `pointerdown` on the canvas has no such ambiguity: nothing this component
   *  does can produce one, so every one of them is a person.
   */
  const onUserMoveRef = useRef(onUserMove)
  useEffect(() => { onUserMoveRef.current = onUserMove }, [onUserMove])
  useEffect(() => {
    const canvas = ready ? map.current?.getCanvasContainer() : null
    if (!canvas) return
    const release = () => onUserMoveRef.current?.()
    canvas.addEventListener("pointerdown", release)
    canvas.addEventListener("wheel", release, { passive: true })
    return () => {
      canvas.removeEventListener("pointerdown", release)
      canvas.removeEventListener("wheel", release)
    }
  }, [ready])

  /** Keep the canvas the size of its box.
   *
   *  Mapbox measures the container once, at construction, and never again. Any
   *  layout change after that — going full screen, a panel collapsing, the
   *  window being dragged to another monitor — leaves the canvas at its old
   *  dimensions, painted into a corner of a box that is now much larger. That
   *  is the black L-shape around a full-screen map, and it is a missing
   *  `resize()` rather than anything to do with zoom or centre.
   *
   *  A ResizeObserver on the container catches every one of those causes,
   *  including the ones no event fires for. `requestAnimationFrame` defers the
   *  call by a frame so it measures the box after the browser has laid it out
   *  rather than during the transition into it.
   */
  useEffect(() => {
    // Deliberately not gated on `ready`. `ready` means the style finished
    // loading, and a map whose tiles are blocked or slow still has a canvas
    // sitting in a box that can change size — gating on it is how this came
    // back the first time it was fixed.
    if (!container.current) return
    const el = container.current
    let frame = 0
    const resize = () => {
      // Twice, on purpose. The immediate call handles the common case and
      // works in a background tab, where `requestAnimationFrame` is throttled
      // to never — a console left on a second monitor and brought forward is
      // exactly the case a deferred-only resize gets wrong. The deferred call
      // then measures again after the browser has finished laying the new box
      // out, which the immediate one is a frame too early for.
      map.current?.resize()
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => map.current?.resize())
    }
    const observer = new ResizeObserver(resize)
    observer.observe(el)
    // The observer fires on its own for the element, but a monitor change or a
    // devtools dock can move the page without changing this box's size.
    window.addEventListener("resize", resize)
    resize()
    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      window.removeEventListener("resize", resize)
    }
  }, [])

  if (failed) {
    return (
      <div className={`bg-muted/30 flex items-center justify-center rounded-lg border p-6 text-center text-sm ${className ?? "h-[520px]"}`}>
        <div>
          <p className="font-medium">The map could not load.</p>
          <p className="text-muted-foreground mt-1 max-w-md text-xs">{failed}</p>
        </div>
      </div>
    )
  }

  return (
    <div className={`relative overflow-hidden ${className ?? "h-[520px] w-full rounded-lg border"}`}>
      <div ref={container} className="h-full w-full" />
    </div>
  )
}
