import { useEffect, useState } from "react"
import { Camera, Plane, Radio, RefreshCw, Smartphone, Truck } from "lucide-react"
import { Link } from "react-router-dom"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

/** "What do our edge devices know about this place right now?"
 *
 *  Backed by GET /incidents/{id}/evidence (app/incidents/evidence.py): edge-AI
 *  cameras near the incident with their latest VLM description and frame,
 *  reports with photos, the drone swarm's tasks nearby, field sensors and the
 *  units on it. The same sweep is written to the event log as `evidence.swept`
 *  whenever the incident opens, worsens or gets another report.
 */

type Cam = {
  id: string; kind: string; label: string | null; distance_m: number; last_seen: string | null
  latest?: { caption?: string; alert?: string; kind?: string | null; at?: string; demo_position?: boolean } | null
}
type Rep = { id: string; linked: boolean; source: string; edge: string; note: string; at: string | null; distance_m: number; photo_url: string | null }
type Media = { id: number; url: string; caption: string | null; source: string }
type DroneTask = { swarm_incident_id: string; title: string | null; status: string; drones: string[] | null; units: string[] | null
  distance_m: number; same_incident: boolean; report?: { text?: string | null } | null }
type Belief = {
  p: number; status: "confirmed" | "likely" | "uncertain" | "doubtful"; dispatch: boolean; prior: number
  byModality: Record<string, number>; reasons: string[]; nextStep: string | null
  witnesses: { modality: string; witness: string; sign: number; weight: number; count: number; note: string }[]
  distress: { witness: string; reports: number }[]
}
type Evidence = {
  summary: string; radius_m: number
  cameras: Cam[]; reports: Rep[]; media: Media[]
  drones: { linked: boolean; incidents: DroneTask[]; localized: { drone: string; distance_m: number; place?: string }[] }
  sensors: { id: string; label: string | null; distance_m: number; overall?: number | null }[]
  units: { id: string; kind: string; label: string; status: string; eta_minutes: number | null }[]
}

const ago = (iso: string | null | undefined) => {
  if (!iso) return ""
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000)
  return s < 90 ? `${Math.round(s)} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`
}

export default function EdgeEvidence({ incidentId }: { incidentId: string }) {
  const [ev, setEv] = useState<Evidence | null>(null)
  const [belief, setBelief] = useState<Belief | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function load() {
    setBusy(true)
    try {
      const [e, b] = await Promise.all([
        request<Evidence>(`/incidents/${incidentId}/evidence`, { toast: false }),
        request<Belief>(`/incidents/${incidentId}/belief`, { toast: false }).catch(() => null),
      ])
      setEv(e)
      setBelief(b)
      setErr(null)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => {
    setEv(null)
    void load()
    const t = window.setInterval(() => void load(), 15000)
    return () => window.clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incidentId])

  const photos = [
    ...(ev?.media ?? []).map((m) => ({ key: `m${m.id}`, url: m.url, caption: m.caption ?? m.source })),
    ...(ev?.reports ?? []).filter((r) => r.photo_url).map((r) => ({ key: `r${r.id}`, url: r.photo_url!, caption: r.note })),
  ].filter((p, i, all) => all.findIndex((q) => q.url === p.url) === i).slice(0, 6)

  return (
    <Card>
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div>
            <CardTitle className="text-sm">1b · What our edge devices see here</CardTitle>
            <CardDescription>
              {ev ? ev.summary : err ? `Could not sweep the area: ${err}` : "Sweeping cameras, drones, reports and sensors nearby…"}
            </CardDescription>
          </div>
          <div className="flex items-center gap-1">
          <Button asChild size="sm" variant="outline" className="h-8">
            <Link to={`/admin/audit?incident=${incidentId}`}>Audit trail</Link>
          </Button>
          <Button size="sm" variant="ghost" onClick={() => void load()} disabled={busy} aria-label="Sweep again">
            <RefreshCw className={`size-3.5 ${busy ? "animate-spin" : ""}`} />
          </Button>
          </div>
        </div>
      </CardHeader>
      {belief && (
        <CardContent className="space-y-2 border-b pb-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">Belief {Math.round(belief.p * 100)}%</span>
            <Badge variant={belief.status === "confirmed" ? "default" : belief.status === "doubtful" ? "destructive" : "outline"}>{belief.status}</Badge>
            <Badge variant="outline">{belief.dispatch ? "units may be dispatched" : "verify first"}</Badge>
            {Object.entries(belief.byModality).map(([m, v]) => (
              <span key={m} className={`rounded px-1.5 py-0.5 text-[11px] ${v >= 0 ? "bg-emerald-500/15" : "bg-red-500/15"}`}>
                {m.replace(/_/g, " ")} {v >= 0 ? "+" : ""}{v.toFixed(2)}
              </span>
            ))}
          </div>
          {belief.reasons.map((r, k) => <p key={k} className="text-muted-foreground text-xs">{r}</p>)}
          {belief.nextStep && <p className="text-xs"><b>Next:</b> {belief.nextStep}</p>}
          {belief.distress.length > 0 && (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              Distress: {belief.distress.map((d) => `${d.reports} reports from one person`).join(", ")} — urgency, not spam.
            </p>
          )}
        </CardContent>
      )}
      {ev && (
        <CardContent className="space-y-3 text-sm">
          {photos.length > 0 && (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {photos.map((p) => (
                <figure key={p.key} className="bg-muted overflow-hidden rounded-md">
                  <img src={p.url} alt={p.caption ?? "edge frame"} className="h-28 w-full object-cover" />
                  <figcaption className="text-muted-foreground line-clamp-2 px-1.5 py-1 text-[11px]">{p.caption}</figcaption>
                </figure>
              ))}
            </div>
          )}
          {ev.cameras.map((c) => (
            <div key={c.id} className="flex items-start gap-2">
              <Camera className="mt-0.5 size-4 shrink-0" />
              <div className="flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{c.label ?? c.id}</span>
                  <span className="text-muted-foreground text-xs">{c.kind} · {c.distance_m} m · seen {ago(c.last_seen)}</span>
                  {c.latest?.kind && <Badge variant="destructive" className="text-xs">{c.latest.kind}</Badge>}
                  {c.latest?.demo_position && <Badge variant="outline" className="text-xs">demo position</Badge>}
                </div>
                {c.latest?.caption && <p className="text-muted-foreground text-xs">“{c.latest.caption}” · {ago(c.latest.at)}</p>}
              </div>
            </div>
          ))}
          {ev.drones.incidents.map((d) => (
            <div key={d.swarm_incident_id} className="flex items-start gap-2">
              <Plane className="mt-0.5 size-4 shrink-0" />
              <div className="flex-1 text-xs">
                <span className="font-medium">Swarm {d.swarm_incident_id}</span>
                {d.same_incident ? " (this incident)" : ` · ${d.distance_m} m`} · {d.status} · drones {d.drones?.join(", ") || "none yet"}
                {d.report?.text && <p className="text-muted-foreground">{d.report.text}</p>}
              </div>
            </div>
          ))}
          {!ev.drones.linked && <p className="text-muted-foreground text-xs"><Plane className="mr-1 inline size-3" />Drone swarm not linked.</p>}
          {ev.reports.filter((r) => !r.linked).slice(0, 5).map((r) => (
            <div key={r.id} className="flex items-start gap-2 text-xs">
              <Smartphone className="mt-0.5 size-3.5 shrink-0" />
              <span><b>{r.edge}</b> report {r.distance_m} m away, {ago(r.at)}, not merged: <span className="text-muted-foreground">{r.note}</span></span>
            </div>
          ))}
          {ev.sensors.map((s) => (
            <div key={s.id} className="flex items-center gap-2 text-xs">
              <Radio className="size-3.5" /> {s.label ?? s.id} · {s.distance_m} m{s.overall != null ? ` · score ${s.overall}` : ""}
            </div>
          ))}
          {ev.units.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <Truck className="size-3.5" /> {ev.units.map((u) => `${u.label} (${u.status})`).join(", ")}
            </div>
          )}
        </CardContent>
      )}
    </Card>
  )
}
