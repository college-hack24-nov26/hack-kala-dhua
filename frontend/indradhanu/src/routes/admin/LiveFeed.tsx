import { useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  Bluetooth, Camera, Drone, HardHat, Heart, Inbox, Pause, Play, Smartphone, Zap,
} from "lucide-react"
import DronePanel from "./DronePanel"
import { useDroneFixes, type DroneFix } from "./droneApi"
import { useDemo } from "@/routes/demo/DemoProvider"
import type { RawReport } from "@/routes/demo/useDemo"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { PACKET_LABEL, hhmmss, useMeshStatus, type MeshPacket } from "./meshApi"
import { FeedDetail } from "./FeedDetail"

/** Everything arriving, as it arrives.
 *
 *  Two streams on one timeline. **Reports** are what the trust pipeline filed
 *  (from the app, the mesh, a camera, a crew) and what it did with each: opened
 *  an incident, merged into one, or held for a human. **Mesh packets** are the
 *  raw arrivals from gateway phones, including the ones that never became a
 *  report (heartbeats, acks, duplicates heard by two gateways, refusals), so an
 *  officer can see the mesh is alive even when nothing is on fire.
 */

type Channel = "all" | "app" | "mesh" | "camera" | "crew" | "drone"
type Item = {
  key: string
  at: number
  channel: Exclude<Channel, "all">
  kind: "report" | "packet" | "drone"
  title: string
  text: string
  where: string | null
  outcome: string
  tone: "open" | "merge" | "held" | "info" | "bad"
  incidentId: string | null
  meta: string[]
  packetId?: number | null
  reportId?: string | null
}

const channelOf = (source: string): Item["channel"] => {
  const s = source.toLowerCase()
  if (s.startsWith("mesh")) return "mesh"
  if (s.includes("sensor") || s.includes("camera") || s.includes("vlm") || s.includes("vision")) return "camera"
  if (s.includes("field") || s.includes("crew")) return "crew"
  return "app"
}

function fromReport(r: RawReport): Item {
  const outcome = r.opened
    ? "opened a new incident"
    : r.incidentId
      ? `merged into “${r.incidentTitle ?? "incident"}”`
      : r.status === "held" || r.status === "pending"
        ? "held for an officer"
        : r.status || "filed"
  const tone: Item["tone"] = r.opened ? "open" : r.incidentId ? "merge" : "held"
  const meta = [r.source]
  if (r.trust != null) meta.push(`trust ${Math.round(r.trust * 100)}%`)
  if (r.assessedSeverity) meta.push(`read as S${r.assessedSeverity}${r.severityRead ? ` (${r.severityRead.method})` : ""}`)
  if (r.hasPhoto) meta.push("photo")
  if (r.reporter) meta.push(r.reporter)
  return {
    key: `r:${r.id}`,
    at: Date.parse(r.createdAt),
    channel: channelOf(r.source),
    kind: "report",
    title: r.classifiedAs ?? r.category,
    text: r.text,
    where: r.wardName ?? r.street ?? r.wardId,
    outcome,
    tone,
    incidentId: r.incidentId,
    meta,
    reportId: r.id,
  }
}

function fromPacket(p: MeshPacket): Item {
  const b = p.body
  const text =
    (b.x as string) ??
    (p.type === "H" ? `alive, ${String(b.b ?? "?")}% battery` : p.type === "K" ? `ack ${String(b.i ?? "")}` : "")
  const channel: Item["channel"] = p.type === "S" ? "camera" : p.type === "F" ? "crew" : "mesh"
  const o = p.outcome ?? "received"
  const tone: Item["tone"] =
    o === "report" ? "open" : o === "linked" ? "merge" : o === "held" ? "held"
      : o === "refused" ? "bad" : "info"
  const meta = [
    p.verified ? "signed ✓" : "unsigned",
    p.gatewayId ? `via ${p.gatewayId}` : "",
  ].filter(Boolean)
  return {
    key: `p:${p.id || p.packetId}`,
    at: Date.parse(p.receivedAt),
    channel,
    kind: "packet",
    title: `${PACKET_LABEL[p.type] ?? p.type} from ${p.nodeId ?? "unknown node"}`,
    text,
    where: null,
    outcome:
      o === "report" ? "opened a report" : o === "linked" ? "linked to an incident"
        : o === "duplicate" ? "duplicate (already heard)" : o,
    tone,
    incidentId: null,
    meta,
    packetId: p.id || null,
  }
}

function fromDrone(d: DroneFix): Item {
  return {
    key: `d:${d.id}`,
    at: Date.parse(d.at),
    channel: "drone",
    kind: "drone",
    title: `Frame from ${d.drone}`,
    text: d.accepted
      ? `Localized from imagery at ${d.lat?.toFixed(6)}, ${d.lon?.toFixed(6)}${d.place ? ` — ${d.place}` : ""}`
      : `Could not be placed: ${d.reason ?? "no confident match"}`,
    where: d.place ?? d.wardId,
    outcome: d.accepted ? "position fixed" : "no match",
    tone: d.accepted ? "merge" : "info",
    incidentId: null,
    meta: [
      d.inliers != null ? `${d.inliers} feature matches` : "",
      d.errorPx != null ? `${d.errorPx} px error` : "",
      d.processingMs != null ? `${d.processingMs} ms` : "",
      d.mode ?? "",
    ].filter(Boolean),
  }
}

const ICON: Record<Item["channel"], typeof Inbox> = {
  app: Smartphone, mesh: Bluetooth, camera: Camera, crew: HardHat, drone: Drone,
}
const TONE: Record<Item["tone"], string> = {
  open: "border-l-red-500",
  merge: "border-l-amber-500",
  held: "border-l-sky-500",
  info: "border-l-muted-foreground/30",
  bad: "border-l-zinc-500",
}
const OUTCOME_BADGE: Record<Item["tone"], string> = {
  open: "bg-red-500/15 text-red-700 dark:text-red-300",
  merge: "bg-amber-500/15 text-amber-800 dark:text-amber-300",
  held: "bg-sky-500/15 text-sky-800 dark:text-sky-300",
  info: "bg-muted text-muted-foreground",
  bad: "bg-muted text-muted-foreground line-through",
}

export default function LiveFeed() {
  const { state } = useDemo()
  const { data: mesh, error: meshError } = useMeshStatus(2000)
  const drone = useDroneFixes(3000)
  const navigate = useNavigate()
  const [channel, setChannel] = useState<Channel>("all")
  const [showPackets, setShowPackets] = useState(true)
  const [paused, setPaused] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const [snapshot, setSnapshot] = useState<Item[]>([])
  const [open, setOpen] = useState<string | null>(null)

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  const all = useMemo(() => {
    const items = state.reports.map(fromReport)
    if (showPackets && mesh) items.push(...mesh.recent.map(fromPacket))
    items.push(...drone.items.map(fromDrone))
    return items
      .filter((it) => Number.isFinite(it.at))
      .sort((a, b) => b.at - a.at)
  }, [state.reports, mesh, showPackets, drone.items])

  const shown = (paused ? snapshot : all).filter(
    (it) => channel === "all" || it.channel === channel,
  )

  // Arrivals per second over the last minute, by when they reached us.
  const buckets = useMemo(() => {
    const out = new Array(60).fill(0) as number[]
    for (const it of all) {
      const age = Math.floor((now - it.at) / 1000)
      if (age >= 0 && age < 60) out[59 - age] += 1
    }
    return out
  }, [all, now])
  const lastMinute = buckets.reduce((a, b) => a + b, 0)
  const peak = Math.max(1, ...buckets)
  const counts = useMemo(() => {
    const c = { app: 0, mesh: 0, camera: 0, crew: 0, drone: 0 }
    for (const it of all) c[it.channel] += 1
    return c
  }, [all])
  const opened = state.reports.filter((r) => r.opened).length
  const gateways = mesh?.nodes.filter((n) => n.kind === "gateway" && n.ageS < 60).length ?? 0

  const CHANNELS: { id: Channel; label: string; n?: number }[] = [
    { id: "all", label: "All", n: all.length },
    { id: "app", label: "App", n: counts.app },
    { id: "mesh", label: "Mesh", n: counts.mesh },
    { id: "camera", label: "Camera / VLM", n: counts.camera },
    { id: "crew", label: "Crews", n: counts.crew },
    { id: "drone", label: "Drones", n: counts.drone },
  ]

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <DronePanel items={drone.items} error={drone.error} />
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Arrivals, last 60 s</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{lastMinute}</CardTitle>
          </CardHeader>
          <div className="flex h-10 items-end gap-px px-6 pb-4" aria-label="arrivals per second">
            {buckets.map((n, i) => (
              <div
                key={i}
                className={`flex-1 rounded-sm ${n ? "bg-primary" : "bg-muted"}`}
                style={{ height: `${n ? Math.max(15, (n / peak) * 100) : 8}%` }}
              />
            ))}
          </div>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Reports on screen</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{state.reports.length}</CardTitle>
          </CardHeader>
          <p className="text-muted-foreground px-6 pb-4 text-xs">
            {opened} opened an incident · the rest merged or held
          </p>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Mesh packets (latest)</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{mesh?.recent.length ?? "—"}</CardTitle>
          </CardHeader>
          <p className="text-muted-foreground px-6 pb-4 text-xs">
            {meshError ? "mesh status unavailable" : `${mesh?.signing ? "signed packets" : "signing OFF"}`}
          </p>
        </Card>
        <Card className={gateways ? "" : "border-amber-500/50"}>
          <CardHeader className="pb-2">
            <CardDescription>Gateway phones linked</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{gateways}</CardTitle>
          </CardHeader>
          <button
            className="text-primary px-6 pb-4 text-left text-xs underline-offset-2 hover:underline"
            onClick={() => navigate("/admin/mesh")}
          >
            {gateways ? "See devices" : "No phone linked. How to link one →"}
          </button>
        </Card>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {CHANNELS.map((c) => (
          <Button
            key={c.id}
            size="sm"
            variant={channel === c.id ? "default" : "outline"}
            onClick={() => setChannel(c.id)}
          >
            {c.label}
            <span className="tabular-nums opacity-70">{c.n ?? 0}</span>
          </Button>
        ))}
        <div className="ml-auto flex items-center gap-2">
          <Button size="sm" variant="outline" onClick={() => setShowPackets((v) => !v)}>
            <Heart className="size-3.5" />
            {showPackets ? "Hide raw packets" : "Show raw packets"}
          </Button>
          <Button size="sm" variant={paused ? "default" : "outline"} onClick={() => {
            if (!paused) setSnapshot(all)
            setPaused((v) => !v)
          }}>
            {paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
            {paused ? "Resume" : "Pause"}
          </Button>
        </div>
      </div>

      {shown.length === 0 ? (
        <Card className="text-muted-foreground flex flex-col items-center gap-2 p-10 text-center text-sm">
          <Inbox className="size-8" />
          Nothing yet on this channel. Reports appear here the second they reach the
          control room, from the app, a mesh gateway phone, a camera or a crew.
        </Card>
      ) : (
        <ul className="space-y-1.5">
          {shown.slice(0, 200).map((it) => {
            const Icon = ICON[it.channel]
            const fresh = now - it.at < 5000
            return (
              <li key={it.key} className="space-y-1.5">
              <div
                role="button"
                tabIndex={0}
                onClick={() => (it.packetId || it.reportId) && setOpen(open === it.key ? null : it.key)}
                onKeyDown={(e) => { if (e.key === "Enter" && (it.packetId || it.reportId)) setOpen(open === it.key ? null : it.key) }}
                className={`bg-card flex gap-3 rounded-md border border-l-4 p-2.5 text-sm transition-colors ${TONE[it.tone]} ${
                  fresh ? "bg-primary/10" : ""
                } ${it.packetId || it.reportId ? "cursor-pointer hover:bg-muted/40" : ""}`}
              >
                <div className="text-muted-foreground w-16 shrink-0 pt-0.5 text-xs tabular-nums">
                  {hhmmss(new Date(it.at).toISOString())}
                </div>
                <Icon className="text-muted-foreground mt-0.5 size-4 shrink-0" />
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-medium">{it.title}</span>
                    {it.where && <span className="text-muted-foreground text-xs">{it.where}</span>}
                    {it.kind === "packet" && (
                      <Badge variant="outline" className="h-5 px-1.5 text-[10px] font-normal">raw packet</Badge>
                    )}
                    {fresh && <Zap className="size-3.5 text-amber-500" aria-label="just arrived" />}
                  </div>
                  {it.text && <p className="text-muted-foreground line-clamp-2 text-xs">{it.text}</p>}
                  <div className="flex flex-wrap items-center gap-1.5 text-xs">
                    <span className={`rounded px-1.5 py-0.5 ${OUTCOME_BADGE[it.tone]}`}>{it.outcome}</span>
                    {it.meta.map((m) => (
                      <span key={m} className="text-muted-foreground">· {m}</span>
                    ))}
                    {it.incidentId && (
                      <button
                        className="text-primary ml-1 underline-offset-2 hover:underline"
                        onClick={(e) => { e.stopPropagation(); navigate(`/admin/response?incident=${it.incidentId}`) }}
                      >
                        what we are doing →
                      </button>
                    )}
                    {(it.packetId || it.reportId) && (
                      <span className="text-primary ml-auto">{open === it.key ? "hide details" : "details"}</span>
                    )}
                  </div>
                </div>
              </div>
              {open === it.key && <FeedDetail packetId={it.packetId ?? null} reportId={it.packetId ? null : it.reportId ?? null} />}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
