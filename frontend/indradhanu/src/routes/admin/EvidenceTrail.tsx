import { useEffect, useMemo, useRef, useState } from "react"
import { useSearchParams } from "react-router-dom"
import mapboxgl from "mapbox-gl"
import { Box, Map as MapIcon, Search, X } from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import type { RawReport } from "@/routes/demo/useDemo"
import { Pill, Segmented } from "@/components/common/MacControls"
import { setSource, square, useMapbox } from "@/components/map/useMapbox"
import { cn } from "@/lib/utils"
import { REGIONS } from "./zones"
import { GROUP, GROUP_OF, pretty, reasonOf, time } from "./zoneLog"

/** The evidence trail: every report on the map, joined to the incident it
 *  opened or was merged into, and on to the units sent. Hover a report for what
 *  it said and what the system made of it; click an incident for its whole
 *  chain - reports, the agents' decisions with their reasons, the units. In 3D
 *  a report floats at the height of when it arrived, so the story of a place
 *  builds upward in time order. */

type Outcome = "opened" | "merged" | "held" | "dismissed" | "pending"
const OUT: Record<Outcome, { label: string; colour: string; dot: string }> = {
  opened: { label: "Opened an incident", colour: "#3b6fe0", dot: "bg-[#3b6fe0]" },
  merged: { label: "Merged into one", colour: "#34a37a", dot: "bg-[#34a37a]" },
  held: { label: "Held for a human", colour: "#eaa21a", dot: "bg-[#eaa21a]" },
  dismissed: { label: "Found false", colour: "#9ca3af", dot: "bg-[#9ca3af]" },
  pending: { label: "No incident yet", colour: "#a78bfa", dot: "bg-[#a78bfa]" },
}
const SEV = { 5: "#d03b3b", 4: "#ec835a", 3: "#fab219", 2: "#94a3b8", 1: "#cbd5e1" } as Record<number, string>
type Window = "1h" | "6h" | "24h" | "all"
const WINDOW_MIN: Record<Window, number> = { "1h": 60, "6h": 360, "24h": 1440, all: Infinity }

const outcomeOf = (r: RawReport): Outcome =>
  r.verdict === "false" ? "dismissed"
  : r.opened ? "opened"
  : r.incidentId ? "merged"
  : /quarantin|reject|held|pending/.test(r.status) || r.category === "unknown_report" ? "held"
  : "pending"

const esc = (s: unknown) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string)

export default function EvidenceTrail() {
  const { state, region: regionPick } = useDemo()
  const [params, setParams] = useSearchParams()
  const ward = params.get("ward")
  const region = REGIONS.find((r) => r.id === (regionPick === "all" ? "pune" : regionPick)) ?? REGIONS[1]
  const [three, setThree] = useState(true)
  const [win, setWin] = useState<Window>("6h")
  const [outcomes, setOutcomes] = useState<Outcome[]>([])
  const [sources, setSources] = useState<string[]>([])
  const [q, setQ] = useState("")
  const [selected, setSelected] = useState<string | null>(null)
  const { ref, map, ready, failed } = useMapbox({ center: region.center, zoom: region.zoom + 1.2, pitch: 58 })
  const popup = useRef<mapboxgl.Popup | null>(null)

  const now = useMemo(() => {
    const ts = state.reports.map((r) => Date.parse(r.createdAt)).filter(Number.isFinite)
    return ts.length ? Math.max(...ts) : Date.now()
  }, [state.reports])
  const incidents = useMemo(() => new Map(state.incidents.map((i) => [i.id, i])), [state.incidents])
  const allSources = useMemo(() => [...new Set(state.reports.map((r) => r.source))].sort(), [state.reports])

  const base = useMemo(() => state.reports.filter((r) => {
    const t = Date.parse(r.createdAt)
    if (Number.isFinite(t) && now - t > WINDOW_MIN[win] * 60000) return false
    if (ward && r.wardId !== ward) return false
    if (sources.length && !sources.includes(r.source)) return false
    if (q.trim() && !`${r.text} ${r.wardName ?? ""} ${r.street ?? ""} ${r.incidentTitle ?? ""}`.toLowerCase().includes(q.trim().toLowerCase())) return false
    return true
  }), [state.reports, now, win, ward, sources, q])
  const reports = useMemo(() => base.filter((r) => !outcomes.length || outcomes.includes(outcomeOf(r))), [base, outcomes])
  const count = (o: Outcome) => base.filter((r) => outcomeOf(r) === o).length

  const t0 = useMemo(() => {
    const ts = reports.map((r) => Date.parse(r.createdAt)).filter(Number.isFinite)
    return ts.length ? Math.min(...ts) : now
  }, [reports, now])
  const span = Math.max(1, (now - t0) / 60000)
  const K = 520 / span          // metres of height per minute

  // the picture
  useEffect(() => {
    const m = map.current
    if (!m || !ready) return
    const minutes = (r: RawReport) => Math.max(0, (Date.parse(r.createdAt) - t0) / 60000) || 0
    const linked = reports.filter((r) => r.incidentId && incidents.has(r.incidentId))
    const incIds = new Set(linked.map((r) => r.incidentId as string))
    const lastMin = new Map<string, number>()
    for (const r of linked) lastMin.set(r.incidentId as string, Math.max(lastMin.get(r.incidentId as string) ?? 0, minutes(r)))
    const rp = (r: RawReport) => ({
      id: r.id, outcome: outcomeOf(r), colour: OUT[outcomeOf(r)].colour, trust: r.trust ?? 0.5,
      text: r.text, source: r.source, cls: r.classifiedAs ?? r.category, at: r.createdAt,
      link: r.linkReason ?? "", verdict: r.verdict ?? "", incident: r.incidentId ?? "", incidentTitle: r.incidentTitle ?? "",
      ward: r.wardName ?? "", status: r.status, sel: r.incidentId === selected ? 1 : 0,
      h0: minutes(r) * K, h1: minutes(r) * K + 26,
    })
    setSource(m, "ev-reports", { type: "FeatureCollection", features: reports.map((r) => ({
      type: "Feature", geometry: { type: "Point", coordinates: r.location }, properties: rp(r) })) })
    setSource(m, "ev-cols", { type: "FeatureCollection", features: three ? reports.map((r) => ({
      type: "Feature", geometry: square(r.location, 16), properties: rp(r) })) : [] })
    setSource(m, "ev-links", { type: "FeatureCollection", features: linked.map((r) => ({
      type: "Feature", geometry: { type: "LineString", coordinates: [r.location, incidents.get(r.incidentId as string)!.location] },
      properties: { colour: OUT[outcomeOf(r)].colour, sel: r.incidentId === selected ? 1 : 0 } })) })
    const inc = [...incIds].map((id) => incidents.get(id)!)
    const units = state.resources.filter((u) => u.incidentId && incIds.has(u.incidentId))
    setSource(m, "ev-incidents", { type: "FeatureCollection", features: inc.map((i) => ({
      type: "Feature", geometry: { type: "Point", coordinates: i.location },
      properties: { id: i.id, title: i.title, sev: i.severity, colour: SEV[i.severity] ?? "#94a3b8", reports: i.reportCount,
                    units: units.filter((u) => u.incidentId === i.id).length, sel: i.id === selected ? 1 : 0 } })) })
    setSource(m, "ev-poles", { type: "FeatureCollection", features: three ? inc.map((i) => ({
      type: "Feature", geometry: square(i.location, 9),
      properties: { colour: SEV[i.severity] ?? "#94a3b8", h: (lastMin.get(i.id) ?? 0) * K + 40 } })) : [] })
    setSource(m, "ev-units", { type: "FeatureCollection", features: units.map((u) => ({
      type: "Feature", geometry: { type: "LineString", coordinates: [incidents.get(u.incidentId as string)!.location, u.location] },
      properties: {} })) })

    if (!m.getLayer("ev-links")) {
      m.addLayer({ id: "ev-units", type: "line", source: "ev-units",
        paint: { "line-color": "#3b6fe0", "line-width": 1.5, "line-dasharray": [2, 2], "line-opacity": 0.6 } })
      m.addLayer({ id: "ev-links", type: "line", source: "ev-links",
        paint: { "line-color": ["get", "colour"], "line-width": ["case", ["==", ["get", "sel"], 1], 3, 1.4],
                 "line-opacity": ["case", ["==", ["get", "sel"], 1], 0.95, 0.45] } })
      m.addLayer({ id: "ev-poles", type: "fill-extrusion", source: "ev-poles",
        paint: { "fill-extrusion-color": ["get", "colour"], "fill-extrusion-height": ["get", "h"], "fill-extrusion-opacity": 0.35 } })
      m.addLayer({ id: "ev-cols", type: "fill-extrusion", source: "ev-cols",
        paint: { "fill-extrusion-color": ["get", "colour"], "fill-extrusion-base": ["get", "h0"], "fill-extrusion-height": ["get", "h1"],
                 "fill-extrusion-opacity": 0.9 } })
      m.addLayer({ id: "ev-reports", type: "circle", source: "ev-reports", paint: {
        "circle-radius": ["interpolate", ["linear"], ["get", "trust"], 0, 3, 1, 7],
        "circle-color": ["get", "colour"], "circle-opacity": 0.9,
        "circle-stroke-color": ["case", ["==", ["get", "sel"], 1], "#111827", "#ffffff"],
        "circle-stroke-width": ["case", ["==", ["get", "sel"], 1], 2, 1],
      } })
      m.addLayer({ id: "ev-incidents", type: "circle", source: "ev-incidents", paint: {
        "circle-radius": ["case", ["==", ["get", "sel"], 1], 13, 10],
        "circle-color": ["get", "colour"], "circle-stroke-color": "#ffffff", "circle-stroke-width": 2.5,
      } })
      m.addLayer({ id: "ev-inc-count", type: "symbol", source: "ev-incidents",
        layout: { "text-field": ["to-string", ["get", "reports"]], "text-size": 11, "text-font": ["DIN Pro Bold", "Arial Unicode MS Bold"], "text-allow-overlap": true },
        paint: { "text-color": "#ffffff" } })

      const pop = () => (popup.current ??= new mapboxgl.Popup({ closeButton: false, closeOnClick: false, offset: 12, className: "indra-pop", maxWidth: "320px" }))
      const showReport = (e: mapboxgl.MapLayerMouseEvent) => {
        const p = e.features?.[0]?.properties as Record<string, string | number> | undefined
        if (!p) return
        m.getCanvas().style.cursor = "pointer"
        const o = OUT[p.outcome as Outcome]
        pop().setLngLat(e.lngLat).setHTML(
          `<div class="ip-title">${esc(String(p.text).slice(0, 180))}</div>` +
          `<div class="ip-sub">${esc(p.source)} · ${esc(time(String(p.at)))}${p.ward ? ` · ${esc(p.ward)}` : ""}</div>` +
          `<div class="ip-chip" style="background:${o.colour}22;color:${o.colour}">${esc(o.label)}</div>` +
          `<div class="ip-hr"></div>` +
          `<div class="ip-row"><span>Read as</span><span>${esc(pretty(String(p.cls)))}</span></div>` +
          `<div class="ip-row"><span>Trust</span><span>${Math.round(Number(p.trust) * 100)}%</span></div>` +
          (p.incidentTitle ? `<div class="ip-row"><span>Incident</span><span>${esc(p.incidentTitle)}</span></div>` : "") +
          (p.verdict ? `<div class="ip-row"><span>Verdict</span><span class="${p.verdict === "false" ? "ip-warn" : "ip-ok"}">${esc(p.verdict)}</span></div>` : "") +
          (p.link ? `<div class="ip-hr"></div><div class="ip-sub">${esc(p.link)}</div>` : ""),
        ).addTo(m)
      }
      const showIncident = (e: mapboxgl.MapLayerMouseEvent) => {
        const p = e.features?.[0]?.properties as Record<string, string | number> | undefined
        if (!p) return
        m.getCanvas().style.cursor = "pointer"
        pop().setLngLat(e.lngLat).setHTML(
          `<div class="ip-title">${esc(p.title)}</div><div class="ip-sub">S${esc(p.sev)} · ${esc(p.reports)} reports · ${esc(p.units)} unit(s)</div>` +
          `<div class="ip-sub" style="margin-top:4px">Click for the whole chain</div>`,
        ).addTo(m)
      }
      const hide = () => { m.getCanvas().style.cursor = ""; popup.current?.remove() }
      for (const id of ["ev-reports", "ev-cols"]) {
        m.on("mousemove", id, showReport)
        m.on("mouseleave", id, hide)
        m.on("click", id, (e) => {
          const inc = e.features?.[0]?.properties?.incident
          if (inc) setSelected(String(inc))
        })
      }
      m.on("mousemove", "ev-incidents", showIncident)
      m.on("mouseleave", "ev-incidents", hide)
      m.on("click", "ev-incidents", (e) => setSelected(String(e.features?.[0]?.properties?.id ?? "")))
    }
  }, [reports, incidents, state.resources, selected, three, K, t0, ready, map])

  useEffect(() => {
    const m = map.current
    if (m && ready) m.easeTo({ pitch: three ? 58 : 0, duration: 600 })
  }, [three, ready, map])

  // fly to the chosen incident's evidence
  useEffect(() => {
    const m = map.current
    if (!m || !ready || !selected) return
    const pts = reports.filter((r) => r.incidentId === selected).map((r) => r.location)
    const i = incidents.get(selected)
    if (i) pts.push(i.location)
    if (!pts.length) return
    const b = new mapboxgl.LngLatBounds(pts[0], pts[0])
    for (const p of pts) b.extend(p)
    m.fitBounds(b, { padding: 120, maxZoom: 15.5, duration: 800 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, ready])

  const chosen = selected ? incidents.get(selected) ?? null : null
  const chain = useMemo(() => {
    if (!selected) return null
    const reps = state.reports.filter((r) => r.incidentId === selected)
      .sort((a, b) => Date.parse(a.createdAt) - Date.parse(b.createdAt))
    const evs = state.events.filter((e) => {
      const p = e.payload ?? {}
      return e.subjectId === selected || p.incident_id === selected || p.to_incident === selected || p.from_incident === selected
    }).sort((a, b) => Date.parse(a.occurredAt) - Date.parse(b.occurredAt))
    const units = state.resources.filter((u) => u.incidentId === selected)
    return { reps, evs, units }
  }, [selected, state.reports, state.events, state.resources])

  const topIncidents = useMemo(() => {
    const n = new Map<string, number>()
    for (const r of reports) if (r.incidentId && incidents.has(r.incidentId)) n.set(r.incidentId, (n.get(r.incidentId) ?? 0) + 1)
    return [...n].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([id, k]) => ({ i: incidents.get(id)!, k }))
  }, [reports, incidents])

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <p className="max-w-3xl text-sm text-muted-foreground">
        Every report, joined to the incident it opened or was merged into and on to the units sent. Hover a report for what it said
        and what the system made of it; click an incident for its whole chain. In 3D each report sits at the height of when it arrived.
      </p>

      <div className="grid gap-5 xl:grid-cols-[1fr_400px]">
        <div className="relative h-[72vh] min-h-[540px] overflow-hidden rounded-2xl border shadow-card">
          <div className="absolute inset-0"><div ref={ref} className="h-full w-full" /></div>
          {failed && (
            <div className="absolute inset-0 grid place-items-center bg-muted/80 p-6 text-center text-sm text-muted-foreground">{failed}</div>
          )}
          <div className="pointer-events-none absolute inset-x-0 top-0 flex flex-wrap items-start gap-2 p-3">
            <div className="pointer-events-auto flex flex-wrap items-center gap-2 rounded-2xl border bg-card/95 p-2 shadow-md backdrop-blur">
              <Segmented size="sm" value={win} onChange={setWin} ariaLabel="Time window"
                options={[{ value: "1h", label: "1 h" }, { value: "6h", label: "6 h" }, { value: "24h", label: "24 h" }, { value: "all", label: "All" }]} />
              <Segmented size="sm" value={three ? "3d" : "2d"} onChange={(v) => setThree(v === "3d")} ariaLabel="View"
                options={[{ value: "2d", label: "2D", icon: MapIcon }, { value: "3d", label: "Time as height", icon: Box }]} />
            </div>
          </div>
          <div className="pointer-events-none absolute bottom-3 left-3 space-y-1 rounded-xl border bg-card/95 px-3 py-2 text-[11px] shadow-sm backdrop-blur">
            {(Object.keys(OUT) as Outcome[]).map((o) => (
              <div key={o} className="flex items-center gap-1.5"><span className={cn("size-2.5 rounded-full", OUT[o].dot)} /> {OUT[o].label}</div>
            ))}
            <div className="pt-1 text-muted-foreground">Dot size = trust · big circle = incident (number = reports)</div>
            {three && <div className="text-muted-foreground">Height = when it arrived ({Math.round(span)} min span)</div>}
          </div>
        </div>

        <aside className="space-y-5">
          <section className="space-y-4 rounded-2xl border bg-card p-5 shadow-card">
            <label className="relative block">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search report text, ward, street"
                     className="h-9 w-full rounded-[10px] border border-input bg-muted/50 pr-3 pl-9 text-sm outline-none focus:border-primary focus:bg-card" />
            </label>
            {ward && (
              <button onClick={() => { params.delete("ward"); setParams(params) }}
                      className="inline-flex items-center gap-1.5 rounded-full bg-accent px-3 py-1 text-xs font-medium text-accent-foreground">
                {state.wards.find((w) => w.id === ward)?.name ?? ward} only <X className="size-3" />
              </button>
            )}
            <div className="flex flex-wrap gap-1.5">
              {(Object.keys(OUT) as Outcome[]).map((o) => (
                <Pill key={o} on={outcomes.includes(o)} dot={OUT[o].dot} count={count(o)}
                      onClick={() => setOutcomes((c) => (c.includes(o) ? c.filter((x) => x !== o) : [...c, o]))}>
                  {OUT[o].label}
                </Pill>
              ))}
            </div>
            {allSources.length > 1 && (
              <div className="flex flex-wrap gap-1.5">
                <span className="self-center text-xs text-muted-foreground">Source</span>
                {allSources.map((s) => (
                  <Pill key={s} on={sources.includes(s)} count={base.filter((r) => r.source === s).length}
                        onClick={() => setSources((c) => (c.includes(s) ? c.filter((x) => x !== s) : [...c, s]))}>
                    {pretty(s)}
                  </Pill>
                ))}
              </div>
            )}
            <p className="text-xs text-muted-foreground tabular-nums">{reports.length} report{reports.length === 1 ? "" : "s"} on the map</p>
          </section>

          {chosen && chain ? (
            <section className="rounded-2xl border bg-card shadow-card">
              <div className="flex items-start gap-3 border-b px-5 py-4">
                <span className="mt-1 size-3 shrink-0 rounded-full" style={{ background: SEV[chosen.severity] }} />
                <div className="min-w-0 flex-1">
                  <h3 className="text-sm font-semibold">{chosen.title}</h3>
                  <p className="text-xs text-muted-foreground">S{chosen.severity} · {chain.reps.length} reports · {chain.units.length} unit(s) · {pretty(chosen.status)}</p>
                </div>
                <button onClick={() => setSelected(null)} className="grid size-7 place-items-center rounded-lg hover:bg-muted" aria-label="Close">
                  <X className="size-4" />
                </button>
              </div>
              <ol className="max-h-[56vh] space-y-4 overflow-y-auto border-l-0 p-5">
                {chain.reps.map((r) => {
                  const o = OUT[outcomeOf(r)]
                  return (
                    <li key={r.id} className="relative pl-5">
                      <span className="absolute top-1.5 left-0 size-2.5 rounded-full" style={{ background: o.colour }} />
                      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                        <span className="tabular-nums">{time(r.createdAt)}</span> · {pretty(r.source)} · trust {Math.round((r.trust ?? 0) * 100)}%
                        {r.opened && <span className="rounded bg-[#3b6fe0]/10 px-1.5 text-[10px] font-medium text-[#3b6fe0]">opened it</span>}
                        {r.verdict && <span className={cn("rounded px-1.5 text-[10px] font-medium", r.verdict === "false" ? "bg-zinc-100 text-zinc-600" : "bg-emerald-50 text-emerald-700")}>{r.verdict}</span>}
                      </div>
                      <p className="mt-0.5 text-sm">{r.text}</p>
                      {r.linkReason && <p className="mt-1 text-xs text-muted-foreground">{r.linkReason}</p>}
                    </li>
                  )
                })}
                {chain.evs.map((e) => {
                  const g = GROUP[GROUP_OF(e.kind)]
                  return (
                    <li key={e.id} className="relative pl-5">
                      <span className={cn("absolute top-0.5 left-[-3px] grid size-4 place-items-center rounded", g.tone)}><g.icon className="size-2.5" /></span>
                      <div className="text-[11px] text-muted-foreground"><span className="tabular-nums">{time(e.occurredAt)}</span> · {pretty(e.kind)} · {e.actor}</div>
                      <p className="mt-0.5 text-sm">{e.text}</p>
                      {reasonOf(e) && reasonOf(e) !== e.text && <p className="mt-1 rounded-lg bg-muted/70 px-2.5 py-1.5 text-xs text-muted-foreground">{reasonOf(e)}</p>}
                    </li>
                  )
                })}
                {chain.units.length > 0 && (
                  <li className="pl-5 text-xs text-muted-foreground">
                    On it now: {chain.units.map((u) => u.label).join(", ")}
                  </li>
                )}
              </ol>
            </section>
          ) : (
            <section className="rounded-2xl border bg-card shadow-card">
              <h3 className="border-b px-5 py-4 text-sm font-semibold">Incidents by evidence</h3>
              {topIncidents.length ? (
                <ul className="divide-y">
                  {topIncidents.map(({ i, k }) => (
                    <li key={i.id}>
                      <button onClick={() => setSelected(i.id)} className="flex w-full items-center gap-3 px-5 py-3 text-left hover:bg-muted/50">
                        <span className="size-2.5 shrink-0 rounded-full" style={{ background: SEV[i.severity] }} />
                        <span className="min-w-0 flex-1 truncate text-sm">{i.title}</span>
                        <span className="text-xs tabular-nums text-muted-foreground">{k} report{k === 1 ? "" : "s"}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="p-5 text-sm text-muted-foreground">No report in this window is linked to an open incident.</p>
              )}
            </section>
          )}
        </aside>
      </div>
    </div>
  )
}
