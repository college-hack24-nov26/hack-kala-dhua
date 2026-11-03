import { useEffect, useMemo, useRef, useState, type ReactNode } from "react"
import mapboxgl from "mapbox-gl"
import "mapbox-gl/dist/mapbox-gl.css"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { usePoll } from "./iotApi"

/** Two-way link with the agentic drone swarm (backend app/drone/swarm_link.py).
 *  What the drones report (filtered), what DisruptionOps tasks them with, and the
 *  reasoner's plans: observations -> reasoning -> actions -> rechecked results. */

const TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined

type Drone = { id: string; state: string; battery: number | null; lng: number | null; lat: number | null
  task_id: string | null; activity: string | null; active: boolean; link: boolean }
type Unit = { id: string; kind: string; state: string; incident_id: string | null; eta: number | null
  agency: string | null; lng: number | null; lat: number | null; capacity: number | null }
type Report = { text: string | null; people: number | null; injured: number | null
  hazards: { kind: string; level: string; trend?: string }[] | null; trend: string | null
  confidence: number | null; observed_at: number | null; best_approach: string | null }
type Incident = { id: string; title: string | null; category: string; status: string; people: number | null
  severity: number | null; lng: number | null; lat: number | null; evacuated: number | null
  control_mode: string | null; drones: string[]; units: string[]; dops_incident_id: string | null
  wait_reason: string | null; report: Report | null
  expecting: { plan_id: string; units: string[]; drones: number; rechecked: boolean; due_in_s: number } | null }
type LogItem = { seq: number; at: number; t: number | null; kind: string; from: string; incident_id: string | null
  text: string; level: "info" | "good" | "warn" | "alert"; data: Record<string, unknown> }
type Plan = { id: string; at: number; incident_id: string; title: string | null; trigger: string
  observations: string[]; reasoning: string[]; actions: { action: string; payload: unknown; why: string }[]
  results: { at: number; ok: boolean; text: string }[]; status: string; mode: string; verified: boolean
  summary: string; applied_by?: string }
type Link = {
  enabled: boolean; connected: boolean; url: string | null; last_error?: string; hint?: string
  run_id?: string; t?: number; scenario?: string; coordination?: string
  control?: { autonomy: string; reason: string | null }; auto?: boolean; take_command?: boolean
  can_task?: boolean | null; anchor?: { lat: number; lon: number }
  drones?: Drone[]; units?: Unit[]; incidents?: Incident[]; log?: LogItem[]; plans?: Plan[]
  stats?: Record<string, number>
  ground?: { owner: "disruptionops" | "swarm"; sent: number; units: number; last_ok: number | null; error: string | null }
}

const DRONE_COLOR: Record<string, string> = {
  ready: "#94a3b8", patrolling: "#2a78d6", enroute: "#fab219", observing: "#8b5cf6", returning: "#14b8a6",
  charging: "#64748b", holding: "#f97316", lost_link: "#d03b3b", crashed: "#7f1d1d", emergency_landed: "#d03b3b",
}
const LEVEL_CLS: Record<string, string> = {
  info: "text-muted-foreground", good: "text-emerald-600", warn: "text-amber-600", alert: "text-red-600 font-medium",
}
const LOG_FILTERS = { all: () => true, alerts: (l: LogItem) => l.level === "alert" || l.kind === "decline",
  situation: (l: LogItem) => l.kind === "situation", decisions: (l: LogItem) =>
    ["plan", "applied", "recheck", "replan", "verified", "task", "rejected", "queued", "agent"].includes(l.kind) } as const
type LogFilter = keyof typeof LOG_FILTERS

const post = async (path: string, body: unknown, success = "Sent to the swarm") => {
  try { return await request<Record<string, unknown>>(path, { method: "POST", body, toast: { success } }) } catch { return null }
}
const task = (body: Record<string, unknown>, success?: string) => post("/drone/link/task", body, success)

function LinkMap({ s, sel, onSelect, onPick, pick }: { s: Link; sel: string | null; onSelect: (id: string) => void
  onPick: (p: { lng: number; lat: number }) => void; pick: { lng: number; lat: number } | null }) {
  const box = useRef<HTMLDivElement>(null)
  const map = useRef<mapboxgl.Map | null>(null)
  const [ready, setReady] = useState(false)
  const centred = useRef(false)
  const cb = useRef({ onSelect, onPick })
  cb.current = { onSelect, onPick }
  const pickMarker = useRef<mapboxgl.Marker | null>(null)

  useEffect(() => {
    if (!box.current || map.current || !TOKEN) return
    mapboxgl.accessToken = TOKEN
    const dark = document.documentElement.classList.contains("dark")
    const m = new mapboxgl.Map({ container: box.current, style: dark ? "mapbox://styles/mapbox/dark-v11" : "mapbox://styles/mapbox/streets-v12",
      center: [73.7624, 18.6527], zoom: 13, attributionControl: false } as mapboxgl.MapOptions)
    map.current = m
    const ro = new ResizeObserver(() => m.resize()); ro.observe(box.current)
    m.addControl(new mapboxgl.NavigationControl({ showCompass: false }), "top-right")
    const text = dark ? "#e6edf7" : "#0f172a", halo = dark ? "#0b1220" : "#fff"
    m.on("load", () => {
      for (const id of ["incidents", "units", "drones"]) m.addSource(id, { type: "geojson", data: { type: "FeatureCollection", features: [] } })
      m.addLayer({ id: "inc", type: "circle", source: "incidents", paint: {
        "circle-radius": ["case", ["get", "sel"], 16, 12], "circle-opacity": 0.2,
        "circle-color": ["match", ["get", "status"], "resolved", "#0ca30c", "reported", "#d03b3b", "#f97316"],
        "circle-stroke-width": ["case", ["get", "sel"], 3, 1.5],
        "circle-stroke-color": ["match", ["get", "status"], "resolved", "#0ca30c", "reported", "#d03b3b", "#f97316"] } })
      m.addLayer({ id: "inc-l", type: "symbol", source: "incidents", layout: { "text-field": ["get", "label"], "text-size": 11,
        "text-offset": [0, 1.9], "text-allow-overlap": true }, paint: { "text-color": text, "text-halo-color": halo, "text-halo-width": 1.2 } })
      m.addLayer({ id: "unit", type: "circle", source: "units", paint: { "circle-radius": 4, "circle-color": "#0f766e",
        "circle-opacity": ["case", ["get", "busy"], 1, 0.4], "circle-stroke-width": 1, "circle-stroke-color": "#fff" } })
      m.addLayer({ id: "dr", type: "circle", source: "drones", paint: { "circle-radius": 6, "circle-color": ["get", "color"],
        "circle-stroke-width": 1.5, "circle-stroke-color": "#fff", "circle-opacity": ["case", ["get", "active"], 1, 0.35] } })
      m.addLayer({ id: "dr-l", type: "symbol", source: "drones", layout: { "text-field": ["get", "id"], "text-size": 10,
        "text-offset": [0, 1.2], "text-allow-overlap": true }, paint: { "text-color": text, "text-halo-color": halo, "text-halo-width": 1 } })
      m.on("click", "inc", (e) => { const id = e.features?.[0]?.properties?.id; if (id) cb.current.onSelect(String(id)) })
      m.on("click", (e) => {
        if (m.queryRenderedFeatures(e.point, { layers: ["inc"] }).length) return
        cb.current.onPick({ lng: e.lngLat.lng, lat: e.lngLat.lat })
      })
      setReady(true)
    })
    return () => { ro.disconnect(); m.remove(); map.current = null }
  }, [])

  useEffect(() => {
    const m = map.current
    if (!m || !ready) return
    const pt = (lng: number | null, lat: number | null) => ({ type: "Point" as const, coordinates: [lng ?? 0, lat ?? 0] })
    ;(m.getSource("drones") as mapboxgl.GeoJSONSource).setData({ type: "FeatureCollection", features: (s.drones ?? [])
      .filter((d) => d.lng != null).map((d) => ({ type: "Feature", geometry: pt(d.lng, d.lat),
        properties: { id: d.id, color: DRONE_COLOR[d.state] ?? "#94a3b8", active: d.active } })) })
    ;(m.getSource("units") as mapboxgl.GeoJSONSource).setData({ type: "FeatureCollection", features: (s.units ?? [])
      .filter((u) => u.lng != null).map((u) => ({ type: "Feature", geometry: pt(u.lng, u.lat), properties: { id: u.id, busy: !!u.incident_id } })) })
    ;(m.getSource("incidents") as mapboxgl.GeoJSONSource).setData({ type: "FeatureCollection", features: (s.incidents ?? [])
      .filter((i) => i.lng != null).map((i) => ({ type: "Feature", geometry: pt(i.lng, i.lat),
        properties: { id: i.id, status: i.status, sel: i.id === sel, label: `${i.id} ${i.category}` } })) })
    if (!centred.current && s.anchor) { centred.current = true; m.jumpTo({ center: [s.anchor.lon, s.anchor.lat], zoom: 13 }) }
  }, [s, ready, sel])

  useEffect(() => {
    const m = map.current
    if (!m) return
    pickMarker.current?.remove()
    pickMarker.current = pick ? new mapboxgl.Marker({ color: "#2a78d6" }).setLngLat([pick.lng, pick.lat]).addTo(m) : null
  }, [pick])

  return (
    <div className="relative h-[520px] w-full overflow-hidden rounded-lg border">
      <div ref={box} className="h-full w-full" />
      {!TOKEN && <div className="bg-muted/80 absolute inset-0 grid place-items-center p-6 text-sm">VITE_MAPBOX_TOKEN is not set</div>}
      <div className="bg-background/90 absolute bottom-3 left-3 flex flex-wrap gap-x-3 gap-y-1 rounded-md border px-3 py-2 text-xs">
        {["patrolling", "enroute", "observing", "returning", "lost_link"].map((k) => (
          <span key={k} className="flex items-center gap-1"><span className="size-2.5 rounded-full" style={{ background: DRONE_COLOR[k] }} />{k.replace("_", " ")}</span>))}
        <span className="flex items-center gap-1"><span className="size-2.5 rounded-full bg-teal-700" />ground unit</span>
        <span className="text-muted-foreground">click the map to set a new task point</span>
      </div>
    </div>
  )
}

export default function DroneLink() {
  const [bump, setBump] = useState(0)
  const { data: s, error } = usePoll(() => request<Link>("/drone/link", { toast: false }), 1500, [bump])
  const [sel, setSel] = useState<string | null>(null)
  const [pick, setPick] = useState<{ lng: number; lat: number } | null>(null)
  const [filter, setFilter] = useState<LogFilter>("all")
  const [cfg, setCfg] = useState({ url: "", token: "" })
  const [nt, setNt] = useState({ kind: "assess", category: "flood", people: 3, note: "" })
  const [role, setRole] = useState("assess")
  const [unitId, setUnitId] = useState("")
  const [upd, setUpd] = useState({ people: "", severity: "" })
  const refresh = () => setBump((b) => b + 1)

  const inc = useMemo(() => s?.incidents?.find((i) => i.id === sel) ?? null, [s, sel])
  const open = useMemo(() => (s?.incidents ?? []).filter((i) => i.status !== "resolved"), [s])
  const freeUnits = useMemo(() => (s?.units ?? []).filter((u) => u.state === "available" && !u.incident_id), [s])
  const log = useMemo(() => (s?.log ?? []).filter(LOG_FILTERS[filter]).filter((l) => !sel || l.incident_id === sel || !l.incident_id), [s, filter, sel])
  const plans = useMemo(() => (s?.plans ?? []).filter((p) => !sel || p.incident_id === sel).filter((p) => p.status !== "no_change"), [s, sel])
  const busyDrones = (s?.drones ?? []).filter((d) => d.task_id).length
  const canTask = !!s?.connected && s?.can_task !== false

  return (
    <div className="space-y-6 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <p className="text-muted-foreground max-w-3xl text-sm">
          Autonomous. The LangGraph agent's <span className="font-medium">aerial</span> node tasks the swarm on every cycle
          (verify reports, eyes on severe incidents, supply drops, stand-down on resolve); the swarm reasoner reads every drone
          report, re-plans ground units, rechecks the result and re-plans again when it falls short. Officers watch here and can
          override. Simulation only.
        </p>
        {s && (
          <span className={`flex items-center gap-2 text-xs ${s.connected ? "text-emerald-600" : "text-red-500"}`}>
            <span className={`size-2 rounded-full ${s.connected ? "bg-emerald-500" : "bg-red-500"}`} />
            {s.connected ? `linked · ${s.scenario ?? ""} · t = ${(s.t ?? 0).toFixed(0)} s` : s.enabled ? "not connected" : "link off"}
          </span>
        )}
      </div>

      {error && <Card className="border-destructive/50 p-4 text-sm">Could not read the swarm link: {error}</Card>}
      {s && (!s.connected || s.can_task === false) && (
        <Card className="border-amber-500/50 p-4 text-sm">
          {s.can_task === false ? "Connected, but the swarm treats our token as view-only: logging works, tasking does not. Paste the swarm's integration token below."
            : `The swarm is not answering${s.last_error ? ` (${s.last_error})` : ""}. ${s.hint ?? "Check the URL below; a Render free instance may be waking up."}`}
        </Card>
      )}

      <Card className="flex flex-wrap items-center gap-2 px-4 py-3 text-xs">
        <span className="font-medium">Swarm</span>
        <span className="text-muted-foreground max-w-[340px] truncate">{s?.url ?? "—"}</span>
        <input value={cfg.url} onChange={(e) => setCfg({ ...cfg, url: e.target.value })} placeholder="wss://drone-1-oj8n.onrender.com/ops/ws"
          className="bg-background min-w-[220px] flex-1 rounded-md border px-2 py-1" />
        <input value={cfg.token} onChange={(e) => setCfg({ ...cfg, token: e.target.value })} placeholder="integration token" type="password"
          className="bg-background w-[200px] rounded-md border px-2 py-1" />
        <button type="button" disabled={!cfg.url && !cfg.token} className="bg-primary text-primary-foreground rounded-md px-2.5 py-1 disabled:opacity-50"
          onClick={async () => { await post("/drone/link/config", { ...(cfg.url ? { url: cfg.url.trim() } : {}), ...(cfg.token ? { token: cfg.token.trim() } : {}) }, "Swarm link restarted"); setCfg({ url: "", token: "" }); refresh() }}>
          Connect</button>
        {s?.enabled && <>
          <button type="button" className="hover:bg-muted rounded-md border px-2.5 py-1"
            onClick={async () => { await post("/drone/link/config", { auto: !s.auto }, s.auto ? "Plans now wait for approval" : "Plans now apply automatically"); refresh() }}>
            Plans: {s.auto ? "auto-apply" : "officer approves"}</button>
          <button type="button" disabled={!canTask} className="hover:bg-muted rounded-md border px-2.5 py-1 disabled:opacity-50"
            onClick={async () => { await task({ action: "orchestrator", value: !s.take_command }); refresh() }}>
            {s.take_command ? "DisruptionOps dispatches" : "Swarm stand-in dispatches"}</button>
          {s.ground && (
            <span title={s.ground.error ?? "Real units, positions and assignments are pushed to the swarm; drone needs go to the planner."}
              className={`rounded-md border px-2 py-1 ${s.ground.error ? "border-amber-500 text-amber-700 dark:text-amber-400" : ""}`}>
              Ground units: {s.ground.owner === "disruptionops"
                ? `DisruptionOps (${s.ground.units} real unit${s.ground.units === 1 ? "" : "s"} in the swarm${s.ground.error ? ", last push refused" : ""})`
                : "swarm simulation"}
            </span>
          )}
          <button type="button" disabled={!canTask} className="hover:bg-muted rounded-md border px-2.5 py-1 disabled:opacity-50"
            onClick={async () => { await task({ action: "autonomy", paused: s.control?.autonomy !== "paused", reason: "officer from DisruptionOps" }); refresh() }}>
            {s.control?.autonomy === "paused" ? "Resume swarm" : "Pause swarm"}</button>
        </>}
      </Card>

      {s?.connected && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <Kpi label="Drones" value={`${busyDrones} / ${s.drones?.length ?? 0}`} sub="on a task / fleet" />
            <Kpi label="Open incidents" value={String(open.length)} sub={`${s.incidents?.length ?? 0} this run`} />
            <Kpi label="Drone messages" value={`${s.stats?.logged ?? 0}`} sub={`logged of ${s.stats?.messages_seen ?? 0} received`} />
            <Kpi label="Plans" value={`${s.stats?.applied ?? 0} / ${s.stats?.plans ?? 0}`} sub="applied / made" />
            <Kpi label="Rechecks · replans" value={`${s.stats?.rechecks ?? 0} · ${s.stats?.replans ?? 0}`} sub="results verified by the drones" />
          </div>

          <div className="grid gap-5 xl:grid-cols-[1fr_400px]">
            <LinkMap s={s} sel={sel} onSelect={setSel} onPick={setPick} pick={pick} />
            <Card className="max-h-[520px] overflow-y-auto">
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">{inc ? `${inc.id} · ${inc.title ?? inc.category}` : "Swarm incidents"}</CardTitle>
                <CardDescription className="text-xs">{inc ? `${inc.status} · severity ${inc.severity ?? "—"} · ${inc.people ?? "?"} people${inc.control_mode === "orchestrated" ? " · DisruptionOps-led" : ""}`
                  : "Tasked automatically. Pick one to see what the drones report; overrides below."}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-3 text-xs">
                {!inc && (
                  <>
                    <div className="space-y-1">
                      {open.map((i) => (
                        <button key={i.id} type="button" onClick={() => setSel(i.id)} className="hover:bg-muted flex w-full justify-between rounded px-2 py-1 text-left">
                          <span><span className="font-medium">{i.id}</span> {i.category} · {i.status}</span>
                          <span className="text-muted-foreground">{i.drones.join(",") || "no drone"} · {i.units.length} unit(s)</span>
                        </button>))}
                    </div>
                    <details className="rounded-md border p-2" open={!!pick}>
                      <summary className="cursor-pointer font-medium">Manual override: send drones to a point {pick ? `(${pick.lat.toFixed(5)}, ${pick.lng.toFixed(5)})` : "(click the map)"}</summary>
                      <div className="mt-2 space-y-2">
                      <div className="flex flex-wrap gap-2">
                        <select value={nt.kind} onChange={(e) => setNt({ ...nt, kind: e.target.value })} className="bg-background rounded border px-1 py-0.5">
                          <option value="assess">assess</option><option value="sos">sos</option><option value="supply">supply drop</option></select>
                        <select value={nt.category} onChange={(e) => setNt({ ...nt, category: e.target.value })} className="bg-background rounded border px-1 py-0.5">
                          {["flood", "trapped", "medical", "fire", "blocked_route"].map((c) => <option key={c}>{c}</option>)}</select>
                        <input type="number" min={1} value={nt.people} onChange={(e) => setNt({ ...nt, people: Number(e.target.value) })} className="bg-background w-16 rounded border px-1 py-0.5" />
                        <span className="text-muted-foreground self-center">people</span>
                      </div>
                      <input value={nt.note} onChange={(e) => setNt({ ...nt, note: e.target.value })} placeholder="what to look for (optional)" className="bg-background w-full rounded border px-2 py-1" />
                      <button type="button" disabled={!pick || !canTask} className="bg-primary text-primary-foreground rounded-md px-2.5 py-1 disabled:opacity-50"
                        onClick={async () => { if (!pick) return; await task({ action: "new_task", ...pick, ...nt }); setPick(null); refresh() }}>Send drones</button>
                      </div>
                    </details>
                  </>
                )}
                {inc && (
                  <>
                    <button type="button" onClick={() => setSel(null)} className="text-primary underline">← all incidents</button>
                    {inc.report ? (
                      <div className="space-y-1 rounded-md border p-2">
                        <div className="font-medium">Latest drone report</div>
                        {inc.report.text && <div>{inc.report.text}</div>}
                        <div className="text-muted-foreground">people {inc.report.people ?? "?"} · injured {inc.report.injured ?? 0} · trend {inc.report.trend ?? "—"} · confidence {inc.report.confidence?.toFixed(2) ?? "—"}</div>
                        {inc.report.best_approach && <div className="text-muted-foreground">approach: {inc.report.best_approach}</div>}
                      </div>) : <div className="text-muted-foreground">No drone report yet.</div>}
                    <div>Drones: <span className="font-medium">{inc.drones.join(", ") || "none"}</span> · Units: <span className="font-medium">{inc.units.join(", ") || "none"}</span></div>
                    {inc.wait_reason && <div className="text-amber-600">Waiting: {inc.wait_reason}</div>}
                    {inc.expecting && <div className="text-amber-600">Checking result: {inc.expecting.units.join(", ") || `${inc.expecting.drones} drone(s)`} ·{" "}
                      {inc.expecting.rechecked ? "rechecked, re-plan in" : "recheck in"} {Math.max(0, inc.expecting.due_in_s)} s</div>}
                    <details className="rounded-md border p-2">
                    <summary className="cursor-pointer font-medium">Manual override</summary>
                    <div className="mt-2 space-y-2">
                    <div className="flex flex-wrap gap-1.5">
                      <Btn disabled={!canTask} onClick={async () => { await task({ action: "recheck", incident_id: inc.id }, "Asked the drones for a fresh report"); refresh() }}>Recheck</Btn>
                      <Btn disabled={!canTask} onClick={async () => { await task({ action: "replan", incident_id: inc.id }, "Re-planned"); refresh() }}>Re-plan now</Btn>
                      <Btn disabled={!canTask} onClick={async () => { await task({ action: "close", incident_id: inc.id }, "Closed"); refresh() }}>Close</Btn>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <select value={role} onChange={(e) => setRole(e.target.value)} className="bg-background rounded border px-1 py-0.5">
                        {["assess", "overwatch", "supply", "relay", "search"].map((r) => <option key={r}>{r}</option>)}</select>
                      <Btn disabled={!canTask} onClick={async () => { await task({ action: "assign_drone", incident_id: inc.id, role, count: 1, ...(role === "supply" ? { payload: ["water", "food", "first_aid"] } : {}) }); refresh() }}>+ Drone</Btn>
                      {inc.drones.map((d) => <Btn key={d} disabled={!canTask} onClick={async () => { await task({ action: "recall", drone_id: d }, `${d} recalled`); refresh() }}>Recall {d}</Btn>)}
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <select value={unitId} onChange={(e) => setUnitId(e.target.value)} className="bg-background max-w-[220px] rounded border px-1 py-0.5">
                        <option value="">free ground unit…</option>
                        {freeUnits.map((u) => <option key={u.id} value={u.id}>{u.id} · {u.kind}</option>)}</select>
                      <Btn disabled={!canTask || !unitId} onClick={async () => { const u = freeUnits.find((x) => x.id === unitId); await task({ action: "assign_unit", incident_id: inc.id, resource_id: unitId, kind: u?.kind }); setUnitId(""); refresh() }}>Assign</Btn>
                      {inc.units.map((u) => <Btn key={u} disabled={!canTask} onClick={async () => { await task({ action: "unassign", incident_id: inc.id, resource_ids: [u] }, `${u} released`); refresh() }}>Release {u}</Btn>)}
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <input value={upd.people} onChange={(e) => setUpd({ ...upd, people: e.target.value })} placeholder="people" className="bg-background w-16 rounded border px-1 py-0.5" />
                      <input value={upd.severity} onChange={(e) => setUpd({ ...upd, severity: e.target.value })} placeholder="sev 1-5" className="bg-background w-16 rounded border px-1 py-0.5" />
                      <Btn disabled={!canTask || (!upd.people && !upd.severity)} onClick={async () => { await task({ action: "update", incident_id: inc.id, ...(upd.people ? { people: Number(upd.people) } : {}), ...(upd.severity ? { severity: Number(upd.severity) } : {}) }, "Incident updated"); setUpd({ people: "", severity: "" }); refresh() }}>Update</Btn>
                    </div>
                    </div>
                    </details>
                  </>
                )}
              </CardContent>
            </Card>
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            <Card>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <CardTitle className="text-sm">Drone log{sel ? ` · ${sel}` : ""}</CardTitle>
                <div className="flex gap-1 text-xs">{(Object.keys(LOG_FILTERS) as LogFilter[]).map((f) => (
                  <button key={f} type="button" onClick={() => setFilter(f)} className={`rounded px-2 py-0.5 ${filter === f ? "bg-primary text-primary-foreground" : "hover:bg-muted border"}`}>{f}</button>))}</div>
              </CardHeader>
              <CardContent className="max-h-[460px] space-y-1 overflow-y-auto text-xs">
                {log.length ? log.map((l) => (
                  <div key={l.seq} className={`${LEVEL_CLS[l.level]} leading-snug`}>
                    <span className="tabular-nums opacity-70">{new Date(l.at * 1000).toLocaleTimeString()}</span>{" "}
                    <Badge variant="outline" className="px-1 py-0 text-[10px]">{l.kind}</Badge>{" "}
                    {l.incident_id && <button type="button" className="underline" onClick={() => setSel(l.incident_id)}>{l.incident_id}</button>}{" "}
                    <span className="opacity-70">{l.from}:</span> {l.text}
                  </div>)) : <div className="text-muted-foreground">Nothing logged yet.</div>}
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2"><CardTitle className="text-sm">Plans and reasoning{sel ? ` · ${sel}` : ""}</CardTitle>
                <CardDescription className="text-xs">Each plan: what the drones saw, why, what was done, and whether the drones confirmed it.</CardDescription></CardHeader>
              <CardContent className="max-h-[460px] space-y-3 overflow-y-auto text-xs">
                {plans.length ? plans.map((p) => (
                  <div key={p.id} className="space-y-1 rounded-md border p-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span><span className="font-medium">{p.id}</span> · <button type="button" className="underline" onClick={() => setSel(p.incident_id)}>{p.incident_id}</button> · {new Date(p.at * 1000).toLocaleTimeString()}</span>
                      <span className="flex items-center gap-1">
                        <Badge variant={p.status === "applied" ? "default" : "outline"}>{p.status}{p.applied_by ? ` · ${p.applied_by}` : ""}</Badge>
                        {p.verified && <Badge className="bg-emerald-600">verified</Badge>}
                      </span>
                    </div>
                    <div className="text-muted-foreground">Trigger: {p.trigger}</div>
                    <ul className="list-disc pl-4">{p.observations.map((o, i) => <li key={i}>{o}</li>)}</ul>
                    <div className="font-medium">Reasoning</div>
                    <ul className="list-disc pl-4">{p.reasoning.map((o, i) => <li key={i}>{o}</li>)}</ul>
                    <div className="font-medium">Actions</div>
                    <ul className="list-disc pl-4">{p.actions.map((a, i) => <li key={i}><span className="font-mono">{a.action}</span> {a.why}</li>)}</ul>
                    {p.results.length > 0 && <><div className="font-medium">Results</div>
                      <ul className="pl-4">{p.results.map((r, i) => <li key={i} className={r.ok ? "text-emerald-600" : "text-red-600"}>{r.ok ? "✓" : "✗"} {r.text}</li>)}</ul></>}
                    {p.status === "proposed" && (
                      <div className="flex gap-2 pt-1">
                        <Btn disabled={!canTask} onClick={async () => { await post(`/drone/link/plans/${p.id}/approve`, {}, "Plan applied"); refresh() }}>Approve</Btn>
                        <Btn onClick={async () => { await post(`/drone/link/plans/${p.id}/reject`, {}, "Plan rejected"); refresh() }}>Reject</Btn>
                      </div>)}
                  </div>)) : <div className="text-muted-foreground">No plans yet. They appear when a drone report changes what is needed.</div>}
              </CardContent>
            </Card>
          </div>
        </>
      )}
    </div>
  )
}

function Btn({ children, onClick, disabled }: { children: ReactNode; onClick: () => void; disabled?: boolean }) {
  return <button type="button" disabled={disabled} onClick={onClick} className="hover:bg-muted rounded-md border px-2 py-0.5 disabled:opacity-50">{children}</button>
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card>
      <CardHeader className="pb-2"><CardDescription>{label}</CardDescription><CardTitle className="truncate text-xl">{value}</CardTitle></CardHeader>
      {sub && <p className="text-muted-foreground truncate px-6 pb-4 text-xs">{sub}</p>}
    </Card>
  )
}
