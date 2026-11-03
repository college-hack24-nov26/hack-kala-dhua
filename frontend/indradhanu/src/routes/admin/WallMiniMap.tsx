import { memo, useMemo } from "react"
import type { DemoState } from "@/routes/demo/useDemo"
import { km, zoomFor, type Region, type Zone } from "./zones"

/** A wall screen without WebGL.
 *
 *  Each live Mapbox map costs tens of megabytes (its own GL context, workers,
 *  tile and glyph caches, icon atlas). A wall of them was most of the page's
 *  memory and what made clicks lag. A screen is one static basemap image from
 *  Mapbox's Static Images API (fetched once, then served from the browser
 *  cache) with the live picture drawn over it as SVG:
 *
 *    city screen   every ward shaded by flood risk, open-incident counts per
 *                  ward, every unit (hollow = free, filled = working),
 *                  hospitals / shelters / relief points, live routes, blocks
 *    zone screen   the ward and its neighbours, the incidents with labels,
 *                  units working them and free units nearby, routes in,
 *                  facilities nearby with names, road blocks
 *
 *  Expanding a screen opens one real, interactive map.
 */

const TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined
const SEV: Record<number, string> = { 5: "#d03b3b", 4: "#ec835a", 3: "#fab219", 2: "#94a3b8", 1: "#cbd5e1" }
const RISK: Record<number, string> = { 5: "#d03b3b", 4: "#ec835a", 3: "#fab219", 2: "#38bdf8", 1: "#64748b" }
const FAC: Record<string, string> = {
  hospital: "#ef4444", medical_camp: "#f472b6", shelter: "#22c55e", relief_centre: "#10b981",
  food_kitchen: "#eab308", water_point: "#38bdf8", school: "#a3e635",
}
const STEP = 40            // image sizes snap to this, so resizing reuses cached images
const MAX = 1280           // Static Images API limit

/** Web Mercator at Mapbox GL zoom (512 px tiles). */
function px(lng: number, lat: number, zoom: number): [number, number] {
  const world = 512 * 2 ** zoom
  const s = Math.sin((Math.max(-85, Math.min(85, lat)) * Math.PI) / 180)
  return [((lng + 180) / 360) * world, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * world]
}

/** Centre and zoom that fit these points in W×H. */
function fit(points: [number, number][], W: number, H: number): { center: [number, number]; zoom: number } | null {
  const ok = points.filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]))
  if (ok.length < 2) return null
  const xs = ok.map((p) => px(p[0], p[1], 0))
  const minX = Math.min(...xs.map((p) => p[0])), maxX = Math.max(...xs.map((p) => p[0]))
  const minY = Math.min(...xs.map((p) => p[1])), maxY = Math.max(...xs.map((p) => p[1]))
  const dx = Math.max(maxX - minX, 1e-6), dy = Math.max(maxY - minY, 1e-6)
  const zoom = Math.max(3, Math.min(15, Math.log2(Math.min((W * 0.86) / dx, (H * 0.8) / dy))))
  const lngs = ok.map((p) => p[0]), lats = ok.map((p) => p[1])
  return {
    center: [(Math.min(...lngs) + Math.max(...lngs)) / 2, (Math.min(...lats) + Math.max(...lats)) / 2],
    zoom,
  }
}

const short = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

function WallMiniMapImpl({ state, zone, region, w, h }: {
  state: DemoState; zone: Zone | null; region?: Region | null; w: number; h: number
}) {
  const W = Math.min(MAX, Math.max(STEP, Math.round(w / STEP) * STEP))
  const H = Math.min(MAX, Math.max(STEP, Math.round(h / STEP) * STEP))
  const big = W >= 400 && H >= 280

  // City screen frames the wards on the wall's region; a zone screen frames
  // its incidents. Rounded so small data changes don't refetch the basemap.
  const view = useMemo(() => {
    if (zone) return { center: zone.center, zoom: zoomFor(zone) - 0.4 }
    const f = fit(state.wards.map((x) => x.centroid), W, H)
    if (f) return { center: [+f.center[0].toFixed(3), +f.center[1].toFixed(3)] as [number, number], zoom: Math.round(f.zoom * 10) / 10 }
    if (region) return { center: region.center, zoom: region.zoom }
    return { center: [77.375, 28.662] as [number, number], zoom: 11.5 }
  }, [zone, state.wards, region, W, H])
  const { center, zoom } = view
  const dark = typeof document !== "undefined" && document.documentElement.classList.contains("dark")

  const src = TOKEN
    ? `https://api.mapbox.com/styles/v1/mapbox/${dark ? "dark-v11" : "light-v11"}/static/` +
      `${center[0].toFixed(4)},${center[1].toFixed(4)},${zoom.toFixed(2)},0/${W}x${H}` +
      `?attribution=false&logo=false&access_token=${TOKEN}`
    : null

  const layer = useMemo(() => {
    const [cx, cy] = px(center[0], center[1], zoom)
    const at = (loc: [number, number]) => {
      const [x, y] = px(loc[0], loc[1], zoom)
      return [x - cx + W / 2, y - cy + H / 2] as const
    }
    const inView = ([x, y]: readonly [number, number]) => x > -10 && y > -10 && x < W + 10 && y < H + 10
    const path = (ring: [number, number][]) =>
      ring.map((p, k) => {
        const [x, y] = at(p)
        return `${k ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`
      }).join(" ")

    const open = state.incidents.filter((i) => !/resolved|closed|cancel/i.test(i.status))
    const incidents = zone ? zone.incidents : open
    const ids = new Set(incidents.map((i) => i.id))
    const reach = zone ? Math.max(2.5, zone.radiusKm * 2.2) : Infinity
    const near = (loc: [number, number]) => !zone || km(zone.center, loc) <= reach

    const perWard = new Map<string, number>()
    for (const i of open) perWard.set(i.wardId, (perWard.get(i.wardId) ?? 0) + 1)

    const wards = state.wards
      .filter((x) => x.boundary && x.boundary.length >= 3)
      .map((x) => ({
        id: x.id,
        d: path(x.boundary as [number, number][]) + " Z",
        mine: zone ? x.id === zone.id : false,
        risk: x.severity ?? 0,
        label: at(x.centroid),
        name: x.name,
        n: perWard.get(x.id) ?? 0,
      }))
      .filter((x) => zone ? x.mine || inView(x.label) : true)

    return {
      wards,
      incidents: incidents
        .map((i) => ({ id: i.id, p: at(i.location), sev: i.severity, un: !i.unitsEnRoute, title: i.title }))
        .filter((d) => inView(d.p)),
      units: state.resources
        .filter((r) => (r.incidentId && ids.has(r.incidentId)) || near(r.location))
        .map((r) => ({ id: r.id, p: at(r.location), busy: !!r.incidentId, mine: !!r.incidentId && ids.has(r.incidentId), out: /unavailable|out_of_service|broken/i.test(r.status) }))
        .filter((d) => inView(d.p)),
      facilities: state.facilities
        .filter((f) => near(f.location))
        .map((f) => ({ id: f.id, p: at(f.location), kind: f.kind, name: f.name }))
        .filter((d) => inView(d.p)),
      routes: state.routes
        .filter((r) => ids.has(r.incidentId) && r.path && r.path.length >= 2)
        .map((r) => ({ id: r.id, d: path(r.path) })),
      blocks: state.roadBlocks.map((b) => ({ id: b.id, p: at(b.location) })).filter((d) => inView(d.p)),
    }
  }, [state, zone, center, zoom, W, H])

  const free = layer.units.filter((u) => !u.busy && !u.out).length
  const working = layer.units.filter((u) => u.busy).length
  const fs = Math.max(9, Math.min(12, W / 48))

  return (
    <div className="relative h-full w-full bg-muted">
      {src && (
        <img
          src={src}
          alt=""
          loading="lazy"
          decoding="async"
          draggable={false}
          className="absolute inset-0 h-full w-full object-cover"
        />
      )}
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid slice"
        className="pointer-events-none absolute inset-0 h-full w-full"
        fontFamily="ui-sans-serif, system-ui"
      >
        {layer.wards.map((x) => (
          <path
            key={x.id}
            d={x.d}
            fill={x.mine ? "rgba(239,68,68,0.10)" : x.risk ? `${RISK[x.risk] ?? "#64748b"}${zone ? "14" : "2e"}` : "transparent"}
            stroke={x.mine ? "#ef4444" : "rgba(148,163,184,0.55)"}
            strokeWidth={x.mine ? 1.8 : 0.8}
            strokeDasharray={x.mine ? "5 3" : undefined}
          />
        ))}
        {layer.routes.map((r) => (
          <path key={r.id} d={r.d} fill="none" stroke="#0ea5e9" strokeOpacity={0.85} strokeWidth={2} strokeDasharray="6 3" />
        ))}
        {layer.facilities.map((f) => (
          <g key={f.id} transform={`translate(${f.p[0]},${f.p[1]})`}>
            {f.kind === "hospital" ? (
              <>
                <rect x={-5} y={-5} width={10} height={10} rx={2} fill="white" stroke="#ef4444" strokeWidth={1.2} />
                <path d="M-3,0 H3 M0,-3 V3" stroke="#ef4444" strokeWidth={2} />
              </>
            ) : (
              <rect x={-3.5} y={-3.5} width={7} height={7} rx={1.5} fill={FAC[f.kind] ?? "#14b8a6"} stroke="white" strokeWidth={1} />
            )}
            {zone && big && (
              <text x={7} y={3.5} fontSize={fs - 1} fill="white" stroke="black" strokeWidth={2.5} paintOrder="stroke">
                {short(f.name, 26)}
              </text>
            )}
          </g>
        ))}
        {layer.blocks.map((b) => (
          <g key={b.id} transform={`translate(${b.p[0]},${b.p[1]})`} stroke="#b91c1c" strokeWidth={2.5}>
            <line x1={-4} y1={-4} x2={4} y2={4} />
            <line x1={-4} y1={4} x2={4} y2={-4} />
          </g>
        ))}
        {layer.units.map((u) => (
          <circle
            key={u.id}
            cx={u.p[0]}
            cy={u.p[1]}
            r={u.mine ? 4 : 3}
            fill={u.out ? "#71717a" : u.busy ? "#0ea5e9" : "rgba(14,165,233,0.15)"}
            stroke={u.busy ? "white" : "#38bdf8"}
            strokeWidth={1.1}
          />
        ))}
        {layer.incidents.map((i) => (
          <g key={i.id}>
            {i.un && <circle cx={i.p[0]} cy={i.p[1]} r={10} fill="none" stroke="#f59e0b" strokeWidth={1.5} strokeDasharray="3 2" />}
            <circle cx={i.p[0]} cy={i.p[1]} r={i.sev >= 4 ? 6.5 : 5} fill={SEV[i.sev] ?? "#94a3b8"} stroke="white" strokeWidth={1.5} />
          </g>
        ))}
        {zone && big && layer.incidents.slice(0, 4).map((i) => (
          <text key={`t-${i.id}`} x={i.p[0] + 9} y={i.p[1] - 7} fontSize={fs} fontWeight={600} fill="white" stroke="black" strokeWidth={3} paintOrder="stroke">
            {short(i.title.split(" — ")[0], 28)}
          </text>
        ))}
        {!zone && layer.wards.filter((x) => x.n > 0).map((x) => (
          <g key={`n-${x.id}`} transform={`translate(${x.label[0]},${x.label[1] - 14})`}>
            <rect x={-9} y={-8} width={18} height={14} rx={7} fill="rgba(0,0,0,0.75)" stroke="#f59e0b" strokeWidth={1} />
            <text textAnchor="middle" y={3} fontSize={10} fontWeight={700} fill="#fde68a">{x.n}</text>
          </g>
        ))}
        {!zone && big && layer.wards.map((x) => (
          <text key={`w-${x.id}`} x={x.label[0]} y={x.label[1] + 14} textAnchor="middle" fontSize={fs - 2}
                fill="rgba(226,232,240,0.9)" stroke="black" strokeWidth={2.5} paintOrder="stroke">
            {short(x.name, 18)}
          </text>
        ))}
      </svg>
      <div className="pointer-events-none absolute bottom-1.5 right-1.5 z-10 flex gap-1.5 rounded-md border bg-card/95 px-1.5 py-0.5 text-[10px] tabular-nums text-foreground shadow-sm">
        <span title="units working"><span className="mr-0.5 inline-block size-2 rounded-full bg-sky-500" />{working}</span>
        <span title="units free nearby"><span className="mr-0.5 inline-block size-2 rounded-full border border-sky-400" />{free}</span>
        <span title="hospitals, shelters, relief points"><span className="mr-0.5 inline-block size-2 rounded-sm bg-emerald-500" />{layer.facilities.length}</span>
        {layer.blocks.length > 0 && <span className="text-red-400" title="road blocks">✕{layer.blocks.length}</span>}
      </div>
      {!TOKEN && (
        <div className="absolute inset-0 flex items-center justify-center text-xs text-muted-foreground">
          VITE_MAPBOX_TOKEN not set
        </div>
      )}
    </div>
  )
}

/** Only redraws when its inputs change; the wall passes a throttled state. */
export const WallMiniMap = memo(WallMiniMapImpl)
