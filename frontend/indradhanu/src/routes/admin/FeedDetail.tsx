import { useQuery } from "@tanstack/react-query"
import { Camera, ImageOff, Loader2, MapPin, Radio, ShieldCheck, Users } from "lucide-react"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Progress } from "@/components/ui/progress"

/** What actually came in behind one live-feed line.
 *
 *  The feed line is a summary; an officer deciding whether to believe it wants
 *  the evidence: the camera node's own description and sensor channels, the
 *  photo (or, when the photo stayed on the phone, what the vision model read
 *  in it), the node that sent it, the report it became and how much the trust
 *  model believed it, and the incident it opened or joined with the units on it.
 */

type Detail = {
  error?: string
  packet?: { id: number; packet_id: string; type: string; node_id: string | null; gateway_id: string | null
    hops: number | null; verified: boolean; outcome: string | null; received_at: string; body: Record<string, unknown> }
  sensed?: { detector?: string; confidence?: number; device?: string; description?: string; vlm_agreed?: boolean
    lat?: number; lon?: number; channels: { name: string; pct: number; class?: boolean }[] }
  node?: { id: string; kind: string; label: string | null; ward_id: string | null; last_seen: string
    meta: Record<string, unknown> }
  report?: { id: string; category: string; classified_as: string | null; classification_confidence: number | null
    note: string | null; source: string; trust_score: number | null; trust_breakdown: Record<string, unknown> | null
    verification_status: string | null; photo_url: string | null; photo_kept_on_device: boolean
    photo_evidence: Record<string, unknown> | null; assessed_severity: number | null
    severity_assessment: Record<string, unknown> | null; street: string | null; ward_name: string | null
    created_at: string; reporter_name: string | null }
  incident?: { id: string; title: string; category: string; severity: number | null; status: string
    report_count: number | null; ward_name: string | null
    needs: { capability_id: string; required: number; met: number }[]
    units: { resource_id: string; label: string; kind: string; status: string; eta_minutes: number | null }[] }
  media?: { id: number; source: string; caption: string | null; dataUrl: string; at: string }[]
}

const pretty = (s?: string | null) => (s ?? "").replace(/_/g, " ")
const pct = (v?: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`)

export function FeedDetail({ packetId, reportId }: { packetId?: number | null; reportId?: string | null }) {
  const q = useQuery({
    queryKey: ["feed-detail", packetId ?? null, reportId ?? null],
    queryFn: () => request<Detail>("/mesh/detail", {
      toast: false, query: packetId != null ? { packet: packetId } : { report: reportId ?? undefined },
    }),
    staleTime: 15_000,
  })
  if (q.isLoading) return <div className="text-muted-foreground flex items-center gap-2 p-3 text-xs"><Loader2 className="size-3.5 animate-spin" /> Loading what came in…</div>
  if (q.error || !q.data || q.data.error) return <div className="text-muted-foreground p-3 text-xs">Details unavailable{q.data?.error ? `: ${q.data.error}` : ""}.</div>
  const d = q.data
  const ev = (d.report?.photo_evidence ?? null) as Record<string, any> | null
  const photos = [...(d.media ?? []).map((m) => ({ src: m.dataUrl, caption: m.caption, source: m.source })),
    ...(d.report?.photo_url ? [{ src: d.report.photo_url, caption: null, source: "report" }] : [])]

  return (
    <div className="bg-muted/30 grid gap-3 rounded-md border p-3 text-xs md:grid-cols-2">
      {/* what was sensed */}
      {d.sensed && (
        <section className="space-y-2">
          <h4 className="flex items-center gap-1.5 text-sm font-medium"><Camera className="size-4" /> What the node sent</h4>
          {d.sensed.description && <p className="text-sm leading-snug">{d.sensed.description}</p>}
          <div className="flex flex-wrap gap-1.5">
            {d.sensed.detector && <Badge variant="outline">detector: {pretty(d.sensed.detector)}</Badge>}
            {d.sensed.confidence != null && <Badge variant="outline">confidence {pct(d.sensed.confidence)}</Badge>}
            {d.sensed.vlm_agreed && <Badge>VLM agreed</Badge>}
            {d.sensed.device && <Badge variant="secondary">{d.sensed.device}</Badge>}
          </div>
          {d.sensed.channels.length > 0 && (
            <div className="space-y-1">
              {d.sensed.channels.map((c) => (
                <div key={c.name} className="flex items-center gap-2">
                  <span className={`w-24 shrink-0 ${c.class ? "font-medium" : "text-muted-foreground"}`}>{c.name}</span>
                  <Progress value={c.pct} className="h-1.5" />
                  <span className="w-9 text-right tabular-nums">{c.pct}%</span>
                </div>
              ))}
            </div>
          )}
          {d.sensed.lat != null && d.sensed.lon != null && (
            <div className="text-muted-foreground flex items-center gap-1"><MapPin className="size-3.5" />
              {d.sensed.lat.toFixed(5)}, {d.sensed.lon.toFixed(5)}{d.node?.ward_id ? ` · ${d.node.ward_id}` : ""}</div>
          )}
        </section>
      )}

      {/* photos or the VLM's reading of them */}
      <section className="space-y-2">
        <h4 className="flex items-center gap-1.5 text-sm font-medium"><Camera className="size-4" /> Photo</h4>
        {photos.length > 0 ? (
          <div className="grid grid-cols-2 gap-2">
            {photos.map((p, i) => (
              <figure key={i} className="overflow-hidden rounded border">
                <img src={p.src} alt={p.caption ?? "evidence photo"} className="h-36 w-full object-cover" />
                {p.caption && <figcaption className="p-1 text-[11px]">{p.caption}</figcaption>}
              </figure>
            ))}
          </div>
        ) : (
          <div className="text-muted-foreground flex items-start gap-1.5">
            <ImageOff className="mt-0.5 size-3.5 shrink-0" />
            {d.report?.photo_kept_on_device
              ? "The photo stayed on the reporter's phone; below is what the vision model read in it."
              : d.packet ? "No image with this packet (LoRa carries text only; a camera sends a frame over HTTPS)."
                : "No photo with this report."}
          </div>
        )}
        {ev && (
          <div className="space-y-1 rounded border p-2">
            <div className="font-medium">Vision model reading{ev.model ? ` (${ev.model})` : ""}</div>
            {ev.caption && <p className="text-sm">{String(ev.caption)}</p>}
            <div className="flex flex-wrap gap-1.5">
              {Array.isArray(ev.hazards) && ev.hazards.map((h: string) => <Badge key={h} variant="outline">{pretty(h)}</Badge>)}
              {ev.water?.present && <Badge variant="outline">water {pretty(ev.water.depthBand)}{ev.water.moving ? ", moving" : ""}</Badge>}
              {ev.people?.visible != null && <Badge variant="outline">{ev.people.visible} people{ev.people.inWater ? `, ${ev.people.inWater} in water` : ""}</Badge>}
              {ev.people?.apparentlyTrapped && <Badge variant="destructive">someone trapped</Badge>}
              {ev.agreement != null && <Badge variant="secondary">agrees with text {pct(Number(ev.agreement))}</Badge>}
            </div>
          </div>
        )}
      </section>

      {/* the report and the trust in it */}
      {d.report && (
        <section className="space-y-1.5">
          <h4 className="flex items-center gap-1.5 text-sm font-medium"><ShieldCheck className="size-4" /> The report</h4>
          {d.report.note && <p className="text-sm leading-snug">“{d.report.note}”</p>}
          <div className="flex flex-wrap gap-1.5">
            <Badge variant="outline">{pretty(d.report.classified_as ?? d.report.category)}{d.report.classification_confidence != null ? ` ${pct(d.report.classification_confidence)}` : ""}</Badge>
            <Badge variant="outline">trust {pct(d.report.trust_score)}</Badge>
            {d.report.verification_status && <Badge variant="secondary">{pretty(d.report.verification_status)}</Badge>}
            {d.report.assessed_severity != null && <Badge>S{d.report.assessed_severity}</Badge>}
            <Badge variant="outline">{pretty(d.report.source)}</Badge>
          </div>
          {d.report.trust_breakdown && (
            <div className="text-muted-foreground">
              {Object.entries(d.report.trust_breakdown).filter(([, v]) => typeof v === "number").slice(0, 6)
                .map(([k, v]) => `${pretty(k)} ${Number(v).toFixed(2)}`).join(" · ")}
            </div>
          )}
          <div className="text-muted-foreground">{[d.report.street, d.report.ward_name].filter(Boolean).join(", ")}</div>
        </section>
      )}

      {/* the incident and who is on it */}
      {d.incident && (
        <section className="space-y-1.5">
          <h4 className="flex items-center gap-1.5 text-sm font-medium"><Users className="size-4" /> Incident: {d.incident.title}</h4>
          <div className="flex flex-wrap gap-1.5">
            {d.incident.severity != null && <Badge>S{d.incident.severity}</Badge>}
            <Badge variant="secondary">{pretty(d.incident.status)}</Badge>
            {d.incident.report_count != null && <Badge variant="outline">{d.incident.report_count} reports</Badge>}
            {d.incident.needs.map((n) => (
              <Badge key={n.capability_id} variant={n.met >= n.required ? "default" : "destructive"}>
                {pretty(n.capability_id)} {n.met}/{n.required}
              </Badge>
            ))}
          </div>
          {d.incident.units.length ? (
            <ul className="space-y-0.5">
              {d.incident.units.map((u) => (
                <li key={u.resource_id}>{u.label} · {pretty(u.status)}{u.eta_minutes != null ? ` · ${u.eta_minutes} min` : ""}</li>
              ))}
            </ul>
          ) : <div className="text-muted-foreground">Nobody on the way yet.</div>}
        </section>
      )}

      {/* node and raw packet */}
      {d.packet && (
        <section className="space-y-1.5 md:col-span-2">
          <h4 className="flex items-center gap-1.5 text-sm font-medium"><Radio className="size-4" /> Node and packet</h4>
          <div className="text-muted-foreground">
            {d.node ? `${d.node.label ?? d.node.id} (${d.node.kind})` : d.packet.node_id} · via {d.packet.gateway_id ?? "?"}
            {d.packet.hops != null ? ` · ${d.packet.hops} hops` : ""} · {d.packet.verified ? "signature verified" : "unsigned"}
            {typeof d.node?.meta?.battery === "number" ? ` · battery ${d.node.meta.battery}%` : ""}
          </div>
          <details>
            <summary className="cursor-pointer">Raw packet</summary>
            <pre className="bg-background mt-1 max-h-48 overflow-auto rounded border p-2 text-[11px]">{JSON.stringify(d.packet.body, null, 2)}</pre>
          </details>
        </section>
      )}
    </div>
  )
}
