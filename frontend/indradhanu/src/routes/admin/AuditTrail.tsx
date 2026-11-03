import { useEffect, useMemo, useState } from "react"
import { useSearchParams } from "react-router-dom"
import {
  Activity, Camera, ChevronRight, FileJson, Film, Image as ImageIcon, Link2, Plane, Radio, RefreshCw, Satellite, Search,
  ShieldCheck, Smartphone, Truck,
} from "lucide-react"
import { apiBaseUrl, request } from "@/api/httpClient"
import { accessToken } from "@/lib/supabase"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"

/** Audit trail: every agent decision, and everything it was based on.
 *
 *  Backed by /audit/decisions and /audit/decisions/{id} (backend app/audit/basis.py). Click a decision and a
 *  drawer opens with the actual material: the camera frames and clips, the citizen and crew photos, the
 *  satellite images (Sentinel-1 radar backscatter and the Copernicus GFM layers, with the pixel values read
 *  from them), the drone observations, the LoRa sensor channels, the fused belief at the time and now, the units
 *  sent with their routes and the planner's reason, and the causal chain back to the first report.
 *  Media bytes are fetched with the staff token only when the drawer is opened.
 */

type Decision = {
  id: number; occurred_at: string; kind: string; actor: string; agent: string; subject_type: string | null
  subject_id: string | null; title: string; text: string; hasMedia: boolean; incidentId: string | null
  payload: Record<string, unknown>; causation_id: number | null
}
type MediaItem = {
  mediaId?: number | null; url?: string | null; type: "image" | "video"; sensor?: string | null; level?: string | null
  caption?: string | null; at?: string | null; extracted?: Record<string, unknown> | null; source?: string | null
}
type BeliefNow = {
  p: number; status: string; dispatch: boolean; prior: number; byModality: Record<string, number>; reasons: string[]
  nextStep: string | null; witnesses: { modality: string; witness: string; sign: number; weight: number; count: number; note: string }[]
}
type IncidentBasis = {
  id: string; title: string; category: string; status: string; severity: number; confidence: number | null; report_count: number
  street: string | null; lat: number | null; lng: number | null; missing?: boolean
  belief: { atDecision: { occurred_at: string; payload: { p?: number; before?: number; status?: string; reasons?: string[] } } | null; now: BeliefNow | null }
  reports: { id: string; source: string; modality: string; reporter_name: string | null; note: string | null; created_at: string
    trust_score: number | null; verification_status: string | null; outcome: string | null }[]
  cameras: { eventId: number; camera: string; at: string; distance_m: number; caption: string | null; hazard: string | null
    alert: string | null; demoPosition?: boolean }[]
  drones: { events: Decision[]; localized: { eventId: number; at: string; drone?: string; place?: string; inliers?: number }[] }
  satellite: { eventId: number; at: string; status?: string; acquired_at?: string; age_hours?: number; values?: Record<string, unknown>
    item_id?: string; sar_item?: string; source?: string }[]
  readings: Record<string, unknown>[]
  units: { assignment_id: string; resource_id: string; kind: string; label: string; status: string; eta_minutes: number | null
    progress: number | null; created_at: string; route_engine: string | null; route: { coordinates: number[][] } | null; reason: string | null }[]
  needs: { capability_id: string; required: number; met: number | null }[]
  sweep: { occurred_at: string; payload: { summary?: string } } | null
  media: MediaItem[]
}
type Basis = { decision: Decision; chain: Decision[]; effects: Decision[]; incidents: IncidentBasis[]; media: MediaItem[] }

const GROUPS: { id: string; label: string; icon: typeof Activity }[] = [
  { id: "", label: "All", icon: Activity }, { id: "planner", label: "Planner", icon: Truck },
  { id: "belief", label: "Belief", icon: ShieldCheck }, { id: "triage", label: "Triage", icon: Smartphone },
  { id: "satellite", label: "Satellite", icon: Satellite }, { id: "drones", label: "Drones", icon: Plane },
  { id: "cameras", label: "Cameras", icon: Camera }, { id: "gate", label: "Approvals", icon: ShieldCheck },
  { id: "agencies", label: "Agencies", icon: Link2 },
]

const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" }) : "")
const pct = (p?: number | null) => (p == null ? "–" : `${Math.round(p * 100)}%`)
const tone = (k: string) =>
  k.startsWith("assignment") || k === "plan.generated" ? "bg-sky-500/15 text-sky-700 dark:text-sky-300"
    : k.startsWith("incident.belief") || k === "evidence.swept" ? "bg-violet-500/15 text-violet-700 dark:text-violet-300"
      : k.startsWith("satellite") ? "bg-amber-500/15 text-amber-700 dark:text-amber-300"
        : k.includes("drone") ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
          : k.startsWith("camera") ? "bg-rose-500/15 text-rose-700 dark:text-rose-300"
            : "bg-muted text-muted-foreground"

/** Fetch a stored media item with the staff bearer and hand back an object URL. */
function useMediaUrl(item: MediaItem): { src: string | null; failed: boolean } {
  const [src, setSrc] = useState<string | null>(item.mediaId ? null : item.url ?? null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!item.mediaId) return
    let url: string | null = null
    let live = true
    ;(async () => {
      try {
        const token = await accessToken()
        const res = await fetch(`${apiBaseUrl}/audit/media/${item.mediaId}`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        })
        if (!res.ok) throw new Error(String(res.status))
        url = URL.createObjectURL(await res.blob())
        if (live) setSrc(url)
      } catch {
        if (live) setFailed(true)
      }
    })()
    return () => { live = false; if (url) URL.revokeObjectURL(url) }
  }, [item.mediaId])
  return { src, failed }
}

function KV({ data }: { data?: Record<string, unknown> | null }) {
  if (!data) return null
  const rows = Object.entries(data).filter(([, v]) => v !== null && v !== undefined && v !== "")
  if (!rows.length) return null
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="font-mono break-all">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
        </div>
      ))}
    </dl>
  )
}

function MediaCard({ item, onOpen }: { item: MediaItem; onOpen: (m: MediaItem, src: string) => void }) {
  const { src, failed } = useMediaUrl(item)
  const Icon = item.type === "video" ? Film : (item.sensor ?? "").toLowerCase().includes("sentinel") || (item.sensor ?? "").toLowerCase().includes("gfm") ? Satellite : ImageIcon
  return (
    <Card className="overflow-hidden gap-0 py-0">
      <button type="button" className="relative block aspect-video w-full bg-muted" onClick={() => src && onOpen(item, src)} disabled={!src}>
        {src ? (
          item.type === "video"
            ? <video src={src} className="h-full w-full object-cover" muted playsInline preload="metadata" />
            : <img src={src} alt={item.caption ?? item.sensor ?? "evidence"} className="h-full w-full object-contain" style={{ imageRendering: (item.sensor ?? "").match(/mask|GFM|likelihood|exclusion/i) ? "pixelated" : "auto" }} />
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-muted-foreground">{failed ? "Could not load" : "Loading…"}</div>
        )}
        <span className="absolute left-2 top-2 inline-flex items-center gap-1 rounded bg-black/60 px-1.5 py-0.5 text-[10px] text-white">
          <Icon className="size-3" /> {item.type}
        </span>
      </button>
      <div className="space-y-1.5 p-3">
        <div className="flex flex-wrap gap-1">
          {item.sensor && <Badge variant="secondary" className="text-[10px]">{item.sensor}</Badge>}
          {item.level && <Badge variant="outline" className="text-[10px]">{item.level}</Badge>}
        </div>
        {item.caption && <p className="line-clamp-3 text-xs">{item.caption}</p>}
        {item.at && <p className="text-[10px] text-muted-foreground">{when(item.at)}</p>}
        <KV data={item.extracted} />
      </div>
    </Card>
  )
}

function BeliefBar({ label, p, sub }: { label: string; p?: number | null; sub?: string }) {
  const v = Math.max(0, Math.min(1, p ?? 0))
  const color = v >= 0.8 ? "bg-emerald-500" : v >= 0.55 ? "bg-sky-500" : v >= 0.3 ? "bg-amber-500" : "bg-rose-500"
  return (
    <div>
      <div className="flex justify-between text-xs"><span>{label}</span><span className="font-mono">{pct(p)}{sub ? ` · ${sub}` : ""}</span></div>
      <div className="mt-1 h-2 rounded bg-muted"><div className={`h-2 rounded ${color}`} style={{ width: `${v * 100}%` }} /></div>
    </div>
  )
}

function ChainRow({ d, current }: { d: Decision; current?: boolean }) {
  return (
    <li className={`relative border-l pl-4 pb-3 ${current ? "border-primary" : ""}`}>
      <span className={`absolute -left-1.5 top-1 size-3 rounded-full border ${current ? "bg-primary" : "bg-background"}`} />
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className={`rounded px-1.5 py-0.5 ${tone(d.kind)}`}>{d.kind}</span>
        <span className="text-muted-foreground">{d.actor}</span>
        <span className="text-muted-foreground">#{d.id} · {when(d.occurred_at)}</span>
      </div>
      {d.text && <p className="mt-1 text-xs">{d.text}</p>}
    </li>
  )
}

function IncidentSection({ inc }: { inc: IncidentBasis }) {
  if (inc.missing) return <p className="text-sm text-muted-foreground">Incident {inc.id} no longer exists.</p>
  const then = inc.belief.atDecision?.payload
  const now = inc.belief.now
  const channels = ["mq2", "mq135", "temp_c", "tilt_deg", "gyro_dps", "vib_g", "mic", "piezo", "pir", "human", "structural", "environmental", "overall"]
  return (
    <div className="space-y-4">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-semibold">{inc.title}</h3>
          <Badge variant="outline">{inc.category}</Badge><Badge variant="secondary">{inc.status}</Badge>
          <Badge variant="outline">severity {inc.severity}</Badge>
        </div>
        <p className="text-xs text-muted-foreground">{inc.street ?? ""} {inc.lat != null ? `· ${inc.lat.toFixed(5)}, ${inc.lng?.toFixed(5)}` : ""} · {inc.report_count} reports</p>
      </div>

      <Card><CardHeader className="pb-2"><CardTitle className="text-sm">Belief that this is real</CardTitle>
        <CardDescription>Log-odds fusion of independent witnesses (docs/TRUST_FUSION.md). Below 55% (30% for life safety) only a field check is sent.</CardDescription></CardHeader>
        <CardContent className="space-y-3">
          <BeliefBar label="At the time of this decision" p={then?.p} sub={then?.status} />
          <BeliefBar label="Now" p={now?.p} sub={now ? `${now.status}${now.dispatch ? " · dispatch" : " · field check only"}` : undefined} />
          {now && (
            <>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {Object.entries(now.byModality).map(([m, w]) => (
                  <div key={m} className="rounded border p-2 text-xs"><div className="text-muted-foreground">{m}</div>
                    <div className={`font-mono ${w >= 0 ? "text-emerald-600" : "text-rose-600"}`}>{w >= 0 ? "+" : ""}{w.toFixed(2)}</div></div>
                ))}
              </div>
              <ul className="list-disc pl-5 text-xs">{(now.reasons ?? []).map((r, i) => <li key={i}>{r}</li>)}</ul>
              {now.nextStep && <p className="text-xs"><b>Next step:</b> {now.nextStep}</p>}
              <details className="text-xs"><summary className="cursor-pointer text-muted-foreground">Every witness ({now.witnesses.length})</summary>
                <table className="mt-2 w-full"><thead><tr className="text-left text-muted-foreground"><th>Modality</th><th>Witness</th><th>Says</th><th>Weight</th><th>Items</th><th>Note</th></tr></thead>
                  <tbody>{now.witnesses.map((w, i) => (
                    <tr key={i} className="border-t"><td>{w.modality}</td><td className="font-mono">{w.witness}</td><td>{w.sign > 0 ? "real" : "not real"}</td>
                      <td className="font-mono">{w.weight.toFixed(2)}</td><td>{w.count}</td><td>{w.note}</td></tr>))}</tbody></table>
              </details>
            </>
          )}
        </CardContent></Card>

      {inc.satellite.length > 0 && (
        <Card><CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-sm"><Satellite className="size-4" /> Satellite passes</CardTitle>
          <CardDescription>Copernicus Global Flood Monitoring on Sentinel-1 C-band SAR (VV, 20 m). Radar sees through cloud; GFM flags open water.</CardDescription></CardHeader>
          <CardContent className="space-y-2">
            {inc.satellite.map((s) => (
              <div key={s.eventId} className="rounded border p-2 text-xs">
                <div className="flex flex-wrap gap-2"><Badge>{s.status}</Badge><span>acquired {when(s.acquired_at)}</span>
                  {s.age_hours != null && <span className="text-muted-foreground">({Math.round(s.age_hours)} h before the check)</span>}</div>
                <div className="mt-1"><KV data={{ ...(s.values ?? {}), scene: s.item_id, radar: s.sar_item }} /></div>
              </div>
            ))}
          </CardContent></Card>
      )}

      <Card><CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-sm"><Smartphone className="size-4" /> Reports ({inc.reports.length})</CardTitle></CardHeader>
        <CardContent className="overflow-x-auto"><table className="w-full text-xs"><thead><tr className="text-left text-muted-foreground"><th>When</th><th>Modality</th><th>Who</th><th>Said</th><th>Trust</th><th>State</th></tr></thead>
          <tbody>{inc.reports.map((r) => (
            <tr key={r.id} className="border-t align-top"><td className="whitespace-nowrap">{when(r.created_at)}</td><td>{r.modality}</td><td>{r.reporter_name ?? r.source}</td>
              <td className="max-w-xs">{r.note}</td><td className="font-mono">{pct(r.trust_score)}</td><td>{r.outcome ?? r.verification_status}</td></tr>))}</tbody></table></CardContent></Card>

      {inc.cameras.length > 0 && (
        <Card><CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-sm"><Camera className="size-4" /> Edge-AI camera frames nearby ({inc.cameras.length})</CardTitle>
          <CardDescription>What the on-device VLM said, frame by frame. Images and clips are in the Evidence tab.</CardDescription></CardHeader>
          <CardContent className="space-y-1.5">{inc.cameras.map((c) => (
            <div key={c.eventId} className="flex flex-wrap items-start gap-2 border-t pt-1.5 text-xs">
              <span className="font-mono">{c.camera}</span><span className="text-muted-foreground">{when(c.at)} · {c.distance_m} m</span>
              {c.hazard && <Badge variant="outline">{c.hazard}</Badge>}{c.alert && <Badge variant="secondary">{c.alert}</Badge>}
              {c.demoPosition && <Badge variant="outline">demo position</Badge>}
              <span className="basis-full">{c.caption}</span></div>))}</CardContent></Card>
      )}

      {(inc.drones.events.length > 0 || inc.drones.localized.length > 0) && (
        <Card><CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-sm"><Plane className="size-4" /> Drone swarm</CardTitle></CardHeader>
          <CardContent><ol className="ml-1">{inc.drones.events.map((d) => <ChainRow key={d.id} d={d} />)}</ol>
            {inc.drones.localized.map((l) => <p key={l.eventId} className="text-xs">Map-matched {l.drone} at {l.place ?? "?"} ({l.inliers ?? "?"} inliers) · {when(l.at)}</p>)}
          </CardContent></Card>
      )}

      {inc.readings.length > 0 && (
        <Card><CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-sm"><Radio className="size-4" /> LoRa sensor readings ({inc.readings.length})</CardTitle>
          <CardDescription>Raw channels from nodes within 600 m, from 30 min before the decision.</CardDescription></CardHeader>
          <CardContent className="overflow-x-auto"><table className="w-full text-[11px]"><thead><tr className="text-left text-muted-foreground"><th>Node</th><th>At</th>{channels.map((c) => <th key={c} className="px-1">{c}</th>)}</tr></thead>
            <tbody>{inc.readings.map((r, i) => (
              <tr key={i} className="border-t font-mono"><td>{String(r.label ?? r.node_id)}{r.simulated ? " (sim)" : ""}</td><td className="whitespace-nowrap">{when(String(r.observed_at))}</td>
                {channels.map((c) => <td key={c} className="px-1">{r[c] == null ? "" : typeof r[c] === "number" ? Number(r[c]).toFixed(2) : String(r[c])}</td>)}</tr>))}</tbody></table></CardContent></Card>
      )}

      <Card><CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-sm"><Truck className="size-4" /> Units sent and why</CardTitle></CardHeader>
        <CardContent className="space-y-2">
          {inc.needs.length > 0 && <p className="text-xs">Needs: {inc.needs.map((n) => `${n.capability_id} ×${n.required}${n.met != null ? ` (met ${n.met})` : ""}`).join(", ")}</p>}
          {inc.units.length === 0 && <p className="text-xs text-muted-foreground">No unit has been assigned to this incident.</p>}
          {inc.units.map((u) => (
            <div key={u.assignment_id} className="rounded border p-2 text-xs">
              <div className="flex flex-wrap gap-2"><b>{u.label}</b><Badge variant="outline">{u.kind}</Badge><Badge variant="secondary">{u.status}</Badge>
                {u.eta_minutes != null && <span>ETA {Math.round(u.eta_minutes)} min</span>}{u.route_engine && <span className="text-muted-foreground">route: {u.route_engine}{u.route ? `, ${u.route.coordinates.length} points` : ""}</span>}</div>
              {u.reason && <p className="mt-1">{u.reason}</p>}
            </div>))}
          {inc.sweep?.payload?.summary && <p className="text-xs text-muted-foreground">Evidence sweep before this: {inc.sweep.payload.summary}</p>}
        </CardContent></Card>
    </div>
  )
}

function BasisDrawer({ id, onClose }: { id: number | null; onClose: () => void }) {
  const [b, setB] = useState<Basis | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [zoom, setZoom] = useState<{ m: MediaItem; src: string } | null>(null)
  useEffect(() => {
    if (id == null) return
    setB(null); setErr(null)
    request<Basis>(`/audit/decisions/${id}`, { toast: false }).then(setB).catch((e) => setErr(String(e?.message ?? e)))
  }, [id])
  const d = b?.decision
  return (
    <Sheet open={id != null} onOpenChange={(o) => !o && onClose()}>
      <SheetContent side="right" className="w-full overflow-y-auto data-[side=right]:sm:max-w-4xl">
        <SheetHeader>
          <SheetTitle className="flex flex-wrap items-center gap-2">
            {d ? <><span className={`rounded px-2 py-0.5 text-sm ${tone(d.kind)}`}>{d.kind}</span><span className="text-base">{d.agent || d.actor}</span></> : "Loading decision…"}
          </SheetTitle>
          <SheetDescription>{d ? `#${d.id} · ${when(d.occurred_at)} · ${d.subject_type ?? ""} ${d.subject_id ?? ""}` : err ?? ""}</SheetDescription>
        </SheetHeader>
        {d && b && (
          <div className="space-y-4 px-4 pb-8">
            {d.text && <Card><CardContent className="pt-4 text-sm">{d.text}</CardContent></Card>}
            <Tabs defaultValue={b.media.length ? "evidence" : "basis"}>
              <TabsList className="flex-wrap">
                <TabsTrigger value="evidence">Evidence ({b.media.length})</TabsTrigger>
                <TabsTrigger value="basis">Basis ({b.incidents.length})</TabsTrigger>
                <TabsTrigger value="chain">Chain ({b.chain.length + b.effects.length})</TabsTrigger>
                <TabsTrigger value="raw"><FileJson className="mr-1 size-3" />Raw</TabsTrigger>
              </TabsList>
              <TabsContent value="evidence" className="mt-3">
                {b.media.length === 0 ? <p className="text-sm text-muted-foreground">No images or clips were attached to this decision or its incident.</p> : (
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {b.media.map((m, i) => <MediaCard key={`${m.mediaId ?? m.url}-${i}`} item={m} onOpen={(mm, src) => setZoom({ m: mm, src })} />)}
                  </div>
                )}
              </TabsContent>
              <TabsContent value="basis" className="mt-3 space-y-6">
                {b.incidents.length === 0 ? <p className="text-sm text-muted-foreground">This decision is not tied to an incident.</p>
                  : b.incidents.map((inc) => <IncidentSection key={inc.id} inc={inc} />)}
              </TabsContent>
              <TabsContent value="chain" className="mt-3">
                <p className="mb-2 text-xs text-muted-foreground">What led to it (root first), then what it caused.</p>
                <ol className="ml-1">{b.chain.map((c) => <ChainRow key={c.id} d={c} current={c.id === d.id} />)}</ol>
                {b.effects.length > 0 && <><p className="mb-2 mt-4 text-xs font-medium">Caused</p><ol className="ml-1">{b.effects.map((c) => <ChainRow key={c.id} d={c} />)}</ol></>}
              </TabsContent>
              <TabsContent value="raw" className="mt-3">
                <pre className="max-h-[60vh] overflow-auto rounded bg-muted p-3 text-[11px]">{JSON.stringify(d.payload, null, 2)}</pre>
              </TabsContent>
            </Tabs>
          </div>
        )}
        <Dialog open={zoom != null} onOpenChange={(o) => !o && setZoom(null)}>
          <DialogContent className="max-w-5xl sm:max-w-5xl">
            <DialogTitle className="text-sm">{zoom?.m.sensor} {zoom?.m.level ? `· ${zoom.m.level}` : ""}</DialogTitle>
            {zoom && (zoom.m.type === "video"
              ? <video src={zoom.src} controls autoPlay className="max-h-[75vh] w-full" />
              : <img src={zoom.src} alt="" className="max-h-[75vh] w-full object-contain" style={{ imageRendering: "pixelated" }} />)}
            {zoom?.m.caption && <p className="text-xs">{zoom.m.caption}</p>}
            <KV data={zoom?.m.extracted} />
          </DialogContent>
        </Dialog>
      </SheetContent>
    </Sheet>
  )
}

export default function AuditTrail() {
  const [params, setParams] = useSearchParams()
  const [items, setItems] = useState<Decision[]>([])
  const [group, setGroup] = useState(params.get("group") ?? "")
  const [q, setQ] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const open = params.get("event") ? Number(params.get("event")) : null
  const incident = params.get("incident") ?? undefined

  async function load(more = false) {
    setBusy(true)
    try {
      const before = more && items.length ? items[items.length - 1].id : undefined
      const r = await request<{ decisions: Decision[] }>("/audit/decisions", { toast: false, query: { group: group || undefined, q: q || undefined, incident_id: incident, before } })
      setItems(more ? [...items, ...r.decisions] : r.decisions); setErr(null)
    } catch (e) { setErr(String((e as Error).message)) } finally { setBusy(false) }
  }
  useEffect(() => { void load() }, [group, incident]) // eslint-disable-line react-hooks/exhaustive-deps

  const byDay = useMemo(() => {
    const m = new Map<string, Decision[]>()
    for (const d of items) { const k = new Date(d.occurred_at).toDateString(); m.set(k, [...(m.get(k) ?? []), d]) }
    return [...m.entries()]
  }, [items])
  const setOpen = (id: number | null) => { const p = new URLSearchParams(params); if (id == null) p.delete("event"); else p.set("event", String(id)); setParams(p) }

  return (
    <div className="space-y-4 p-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Audit trail</h1>
          <p className="text-sm text-muted-foreground">Every agent decision. Open one to see the photos, clips, satellite images, readings and reasoning it was based on.</p>
          {incident && <p className="text-xs">Filtered to incident <span className="font-mono">{incident}</span> <button className="underline" onClick={() => { const p = new URLSearchParams(params); p.delete("incident"); setParams(p) }}>clear</button></p>}
        </div>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); void load() }}>
          <div className="relative"><Search className="absolute left-2 top-2.5 size-4 text-muted-foreground" />
            <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search kind, agent, text" className="h-9 w-64 pl-8" /></div>
          <Button type="submit" variant="outline" size="sm" className="h-9" disabled={busy}><RefreshCw className={`size-4 ${busy ? "animate-spin" : ""}`} /></Button>
        </form>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {GROUPS.map((g) => (
          <Button key={g.id} size="sm" variant={group === g.id ? "default" : "outline"} className="h-8 gap-1.5" onClick={() => setGroup(g.id)}>
            <g.icon className="size-3.5" />{g.label}
          </Button>
        ))}
      </div>
      {err && <p className="text-sm text-destructive">{err}</p>}
      {byDay.map(([day, ds]) => (
        <div key={day}>
          <p className="mb-1 text-xs font-medium text-muted-foreground">{day}</p>
          <div className="divide-y rounded-lg border">
            {ds.map((d) => (
              <button key={d.id} type="button" onClick={() => setOpen(d.id)} className="flex w-full items-start gap-3 px-3 py-2.5 text-left hover:bg-muted/50">
                <span className="w-16 shrink-0 pt-0.5 font-mono text-xs text-muted-foreground">{new Date(d.occurred_at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span>
                <span className={`shrink-0 rounded px-1.5 py-0.5 text-xs ${tone(d.kind)}`}>{d.kind}</span>
                <span className="min-w-0 flex-1">
                  <span className="text-xs text-muted-foreground">{d.actor}{d.incidentId ? ` · incident ${d.incidentId.slice(0, 8)}` : ""}</span>
                  <span className="block truncate text-sm">{d.text || d.subject_id}</span>
                </span>
                {d.hasMedia && <ImageIcon className="mt-1 size-4 shrink-0 text-muted-foreground" />}
                <ChevronRight className="mt-1 size-4 shrink-0 text-muted-foreground" />
              </button>
            ))}
          </div>
        </div>
      ))}
      {items.length > 0 && <Button variant="outline" size="sm" onClick={() => void load(true)} disabled={busy}>Older</Button>}
      {!busy && !items.length && !err && <p className="text-sm text-muted-foreground">No agent decisions yet.</p>}
      <BasisDrawer id={open} onClose={() => setOpen(null)} />
    </div>
  )
}
