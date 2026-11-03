import { useEffect, useMemo, useRef, useState } from "react"
import mapboxgl from "mapbox-gl"
import "mapbox-gl/dist/mapbox-gl.css"
import { Link } from "react-router-dom"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { usePoll } from "./iotApi"

/** The 12-drone rescue swarm, live from its WebSocket feed (backend
 *  app/drone/swarm.py), placed on the city map: drones, survivors, Jev's
 *  decisions and what the feed changed in the response (incidents filed,
 *  road blocks for the router). */

const TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined

type Drone = { n: number; lng: number; lat: number; alt_m: number; heading: number
  state: string; battery: number; active: boolean }
type Survivor = { site: number; lng: number; lat: number; found: boolean; delivered: boolean
  incidentId: string | null }
type Swarm = {
  enabled: boolean; url: string; connected: boolean; lastMessageAgoS: number | null
  lastError: string | null; anchor: { lat: number; lon: number } | null; anchorSource: string
  scale: number; run: number; t: number; phase: string; mode: string
  survivors: Survivor[]; drones: Drone[]
  decisions: { drone: number; text: string; sector: number | null; p: number | null }[]
  actions: { at: number; text: string }[]; log: string[]
}

const STATE_COLOR: Record<string, string> = {
  TAKEOFF: "#94a3b8", EXPLORE: "#2a78d6", DELIVER: "#fab219", RELAY: "#8b5cf6",
  RETURN: "#14b8a6", COMPLETE: "#0ca30c",
}

function SwarmMap({ s }: { s: Swarm }) {
  const box = useRef<HTMLDivElement>(null)
  const map = useRef<mapboxgl.Map | null>(null)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState<string | null>(TOKEN ? null : "VITE_MAPBOX_TOKEN is not set")
  const centred = useRef(false)

  useEffect(() => {
    if (!box.current || map.current || !TOKEN) return
    mapboxgl.accessToken = TOKEN
    const dark = document.documentElement.classList.contains("dark")
    const m = new mapboxgl.Map({
      container: box.current, style: dark ? "mapbox://styles/mapbox/dark-v11" : "mapbox://styles/mapbox/streets-v12",
      center: [73.8567, 18.5204], zoom: 16, attributionControl: false, fadeDuration: 0, projection: "mercator",
    } as mapboxgl.MapOptions)
    map.current = m
    const ro = new ResizeObserver(() => m.resize())
    ro.observe(box.current)
    m.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right")
    m.on("load", () => {
      m.addSource("survivors", { type: "geojson", data: { type: "FeatureCollection", features: [] } })
      m.addSource("drones", { type: "geojson", data: { type: "FeatureCollection", features: [] } })
      m.addLayer({ id: "sv-ring", type: "circle", source: "survivors", paint: {
        "circle-radius": 18, "circle-color": ["case", ["get", "delivered"], "#0ca30c", ["get", "found"], "#d03b3b", "#64748b"],
        "circle-opacity": 0.18, "circle-stroke-width": 2,
        "circle-stroke-color": ["case", ["get", "delivered"], "#0ca30c", ["get", "found"], "#d03b3b", "#64748b"] } })
      m.addLayer({ id: "sv-label", type: "symbol", source: "survivors", layout: {
        "text-field": ["get", "label"], "text-size": 11, "text-offset": [0, 2.2], "text-allow-overlap": true },
        paint: { "text-color": dark ? "#e6edf7" : "#0f172a", "text-halo-color": dark ? "#0b1220" : "#fff", "text-halo-width": 1.2 } })
      m.addLayer({ id: "dr", type: "circle", source: "drones", paint: {
        "circle-radius": 6, "circle-color": ["get", "color"], "circle-stroke-width": 1.5, "circle-stroke-color": "#fff",
        "circle-opacity": ["case", ["get", "active"], 1, 0.35] } })
      m.addLayer({ id: "dr-label", type: "symbol", source: "drones", layout: {
        "text-field": ["get", "label"], "text-size": 10, "text-offset": [0, 1.2], "text-allow-overlap": true },
        paint: { "text-color": dark ? "#e6edf7" : "#0f172a", "text-halo-color": dark ? "#0b1220" : "#fff", "text-halo-width": 1 } })
      setReady(true)
    })
    m.on("error", (e) => {
      const err = (e as unknown as { error?: { status?: number } })?.error
      if (err?.status === 401) setFailed("Mapbox rejected the token (401).")
    })
    return () => { ro.disconnect(); m.remove(); map.current = null }
  }, [])

  useEffect(() => {
    const m = map.current
    if (!m || !ready) return
    ;(m.getSource("drones") as mapboxgl.GeoJSONSource).setData({
      type: "FeatureCollection",
      features: s.drones.map((d) => ({ type: "Feature", geometry: { type: "Point", coordinates: [d.lng, d.lat] },
        properties: { label: `D${d.n}`, color: STATE_COLOR[d.state] ?? "#94a3b8", active: d.active } })),
    })
    ;(m.getSource("survivors") as mapboxgl.GeoJSONSource).setData({
      type: "FeatureCollection",
      features: s.survivors.map((v) => ({ type: "Feature", geometry: { type: "Point", coordinates: [v.lng, v.lat] },
        properties: { found: v.found, delivered: v.delivered,
          label: `Site ${v.site}${v.delivered ? " · supplied" : v.found ? " · found" : ""}` } })),
    })
    if (!centred.current && s.anchor) {
      centred.current = true
      m.jumpTo({ center: [s.anchor.lon, s.anchor.lat], zoom: 17 })
    }
  }, [s, ready])

  return (
    <div className="relative h-[520px] w-full overflow-hidden rounded-lg border">
      <div ref={box} className="h-full w-full" />
      {failed && <div className="bg-muted/80 absolute inset-0 grid place-items-center p-6 text-sm">{failed}</div>}
      <div className="bg-background/90 absolute bottom-3 left-3 flex flex-wrap gap-x-3 gap-y-1 rounded-md border px-3 py-2 text-xs">
        {Object.entries(STATE_COLOR).map(([k, c]) => (
          <span key={k} className="flex items-center gap-1"><span className="size-2.5 rounded-full" style={{ background: c }} />{k.toLowerCase()}</span>
        ))}
        <span className="flex items-center gap-1"><span className="size-2.5 rounded-full border-2 border-red-500" />survivor found</span>
        <span className="flex items-center gap-1"><span className="size-2.5 rounded-full border-2 border-green-600" />supplied</span>
      </div>
    </div>
  )
}

export default function DroneSwarm() {
  const [bump, setBump] = useState(0)
  const { data: s, error } = usePoll(() => request<Swarm>("/drone/swarm", { toast: false }), 1000, [bump])
  const [url, setUrl] = useState("")
  const active = useMemo(() => s?.drones.filter((d) => d.active).length ?? 0, [s])

  const save = async (body: Record<string, unknown>) => {
    try {
      await request("/drone/swarm", { method: "POST", body, toast: { success: "Swarm listener updated" } })
    } catch { /* toasted */ }
    setBump((b) => b + 1)
  }

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-muted-foreground text-sm">
            12-drone rescue swarm (PyBullet, decisions by Jev), live. Survivors it finds become incidents; obstructions it
            flies round over a street become road blocks the router avoids.
          </p>
        </div>
        {s && (
          <span className={`flex items-center gap-2 text-xs ${s.connected ? "text-emerald-600" : "text-red-500"}`}>
            <span className={`size-2 rounded-full ${s.connected ? "bg-emerald-500" : "bg-red-500"}`} />
            {s.connected ? `live · last message ${s.lastMessageAgoS ?? "—"}s ago` : s.enabled ? "not connected" : "listener off"}
          </span>
        )}
      </div>

      {error && <Card className="border-destructive/50 p-4 text-sm">Could not read the swarm status: {error}</Card>}
      {s && !s.connected && (
        <Card className="border-amber-500/50 p-4 text-sm">
          The feed is not answering{s.lastError ? ` (${s.lastError})` : ""}. The host's laptop may be off, or the tunnel
          has a new address: paste it below.
        </Card>
      )}

      {s && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <Kpi label="Phase" value={s.phase.split("|")[0].trim() || "—"} sub={`run ${s.run} · t = ${s.t.toFixed(0)} s · ${s.mode}`} />
            <Kpi label="Drones active" value={`${active} / ${s.drones.length}`} sub="in the air or on the pad" />
            <Kpi label="Survivors found" value={`${s.survivors.filter((v) => v.found).length} / ${s.survivors.length}`} sub="each becomes an incident" />
            <Kpi label="Supplies delivered" value={`${s.survivors.filter((v) => v.delivered).length}`} sub="payloads landed" />
            <Kpi label="Placed at" value={`×${s.scale}`} sub={s.anchorSource || "—"} />
          </div>

          <div className="grid gap-5 xl:grid-cols-[1fr_360px]">
            <SwarmMap s={s} />
            <div className="space-y-4">
              <Card>
                <CardHeader className="pb-2"><CardTitle className="text-sm">What the feed changed</CardTitle>
                  <CardDescription className="text-xs">Incidents filed, supplies, road blocks</CardDescription></CardHeader>
                <CardContent className="space-y-1.5 text-xs">
                  {s.survivors.filter((v) => v.incidentId).map((v) => (
                    <div key={v.site} className="flex justify-between gap-2">
                      <span>Site {v.site}: {v.delivered ? "supplied" : "found"}</span>
                      <Link className="text-primary underline" to="/admin/incidents">incident</Link>
                    </div>
                  ))}
                  {s.actions.length ? s.actions.slice(0, 10).map((a, i) => (
                    <div key={i} className="text-muted-foreground">
                      <span className="tabular-nums">{new Date(a.at * 1000).toLocaleTimeString()}</span> · {a.text}
                    </div>
                  )) : <div className="text-muted-foreground">Nothing yet this session.</div>}
                </CardContent>
              </Card>
              <Card>
                <CardHeader className="pb-2"><CardTitle className="text-sm">Jev decisions</CardTitle></CardHeader>
                <CardContent className="space-y-1 text-xs">
                  {s.decisions.length ? s.decisions.slice(0, 10).map((d, i) => (
                    <div key={i}><span className="font-medium">D{d.drone}</span> <span className="text-muted-foreground">{d.text}</span></div>
                  )) : <div className="text-muted-foreground">No decisions yet.</div>}
                </CardContent>
              </Card>
            </div>
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            <Card>
              <CardHeader className="pb-2"><CardTitle className="text-sm">Drones</CardTitle></CardHeader>
              <CardContent>
                <table className="w-full text-xs">
                  <thead className="text-muted-foreground text-left"><tr className="border-b">
                    <th className="py-1">Drone</th><th>State</th><th>Battery</th><th>Altitude</th><th>Heading</th></tr></thead>
                  <tbody>
                    {s.drones.map((d) => (
                      <tr key={d.n} className={`border-b last:border-0 ${d.active ? "" : "opacity-50"}`}>
                        <td className="py-1 font-medium">D{d.n}</td>
                        <td><Badge variant="outline" style={{ borderColor: STATE_COLOR[d.state], color: STATE_COLOR[d.state] }}>{d.state}</Badge></td>
                        <td className="tabular-nums">
                          <div className="flex items-center gap-2">
                            <div className="bg-muted h-1.5 w-16 overflow-hidden rounded-full">
                              <div className={`h-full ${d.battery < 0.25 ? "bg-red-500" : "bg-emerald-500"}`} style={{ width: `${Math.round(d.battery * 100)}%` }} />
                            </div>{Math.round(d.battery * 100)}%
                          </div>
                        </td>
                        <td className="tabular-nums">{d.alt_m.toFixed(0)} m</td>
                        <td className="tabular-nums">{d.heading.toFixed(0)}°</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2"><CardTitle className="text-sm">Feed log</CardTitle></CardHeader>
              <CardContent className="max-h-[360px] space-y-0.5 overflow-y-auto font-mono text-[11px]">
                {s.log.map((l, i) => <div key={i} className="text-muted-foreground">{l}</div>)}
              </CardContent>
            </Card>
          </div>

          <Card className="flex flex-wrap items-center gap-2 px-4 py-3 text-xs">
            <span className="font-medium">Feed</span>
            <span className="text-muted-foreground truncate">{s.url}</span>
            <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="wss://new-host.trycloudflare.com/ws"
              className="bg-background min-w-[260px] flex-1 rounded-md border px-2 py-1" />
            <button type="button" disabled={!/^wss?:\/\//.test(url)} onClick={() => { void save({ url: url.trim() }); setUrl("") }}
              className="bg-primary text-primary-foreground rounded-md px-2.5 py-1 disabled:opacity-50">Use this URL</button>
            <button type="button" onClick={() => void save({ enabled: !s.enabled })} className="hover:bg-muted rounded-md border px-2.5 py-1">
              {s.enabled ? "Turn listener off" : "Turn listener on"}
            </button>
          </Card>
        </>
      )}
    </div>
  )
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        <CardTitle className="truncate text-xl">{value}</CardTitle>
      </CardHeader>
      {sub && <p className="text-muted-foreground truncate px-6 pb-4 text-xs">{sub}</p>}
    </Card>
  )
}
