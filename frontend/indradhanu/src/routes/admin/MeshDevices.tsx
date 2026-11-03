import { useMemo, useState } from "react"
import {
  BatteryMedium, Bluetooth, Camera, Check, Copy, KeyRound, Radio, ShieldAlert,
  ShieldCheck, Smartphone, Users,
} from "lucide-react"
import { apiBaseUrl } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { PACKET_LABEL, ago, hhmmss, useMeshStatus, type MeshNode } from "./meshApi"

/** The offline mesh as the control room sees it.
 *
 *  **Gateways** are bitchat phones with internet and the Command Centre Link
 *  switched on. Each one heartbeats every ~10 s, so it shows here as soon as it
 *  is linked, before it has carried a single report. Everything a phone with no
 *  signal sends reaches us through one of these.
 *
 *  **Cameras and phones** are the nodes we have heard *through* a gateway: a
 *  VLM camera's signed hazard packets, a resident's SOS.
 */

const live = (n: MeshNode) => n.ageS < 45
const stale = (n: MeshNode) => n.ageS >= 45 && n.ageS < 300

function Dot({ n }: { n: MeshNode }) {
  const cls = live(n) ? "bg-emerald-500" : stale(n) ? "bg-amber-500" : "bg-zinc-400"
  return (
    <span className="relative inline-flex size-2.5">
      {live(n) && <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-400 opacity-60" />}
      <span className={`relative inline-flex size-2.5 rounded-full ${cls}`} />
    </span>
  )
}

export default function MeshDevices() {
  const { data, error } = useMeshStatus(3000)
  const [copied, setCopied] = useState(false)

  const base = useMemo(() => {
    try {
      const u = new URL(apiBaseUrl, window.location.origin)
      return u.origin
    } catch {
      return apiBaseUrl
    }
  }, [])

  const gateways = data?.nodes.filter((n) => n.kind === "gateway") ?? []
  const cameras = data?.nodes.filter((n) => n.kind === "camera" || n.kind === "sensor") ?? []
  const phones = data?.nodes.filter((n) => n.kind === "phone") ?? []
  const linked = gateways.filter(live).length

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(base)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      /* clipboard blocked: the URL is on screen anyway */
    }
  }

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      {error && (
        <Card className="border-destructive/50 p-4 text-sm">
          Could not read the mesh status: {error}
        </Card>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Card className={linked ? "border-emerald-500/50" : "border-amber-500/50"}>
          <CardHeader className="pb-2">
            <CardDescription>Gateway phones linked now</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{linked}</CardTitle>
          </CardHeader>
          <p className="text-muted-foreground px-6 pb-4 text-xs">
            {gateways.length} ever seen · heartbeat within 45 s counts as linked
          </p>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Cameras / VLM nodes heard</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{cameras.length}</CardTitle>
          </CardHeader>
          <p className="text-muted-foreground px-6 pb-4 text-xs">{phones.length} resident phones heard</p>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Outbox, last 24 h</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{data?.outbox.pending ?? 0} pending</CardTitle>
          </CardHeader>
          <p className="text-muted-foreground px-6 pb-4 text-xs">
            {data?.outbox.sent ?? 0} sent · {data?.outbox.acked ?? 0} acked · {data?.outbox.expired ?? 0} expired
          </p>
        </Card>
        <Card className={data && (!data.enabled || !data.signing) ? "border-destructive/50" : undefined}>
          <CardHeader className="pb-2">
            <CardDescription>Security</CardDescription>
            <CardTitle className="flex items-center gap-2 text-base">
              {data?.enabled ? <KeyRound className="size-4 text-emerald-600" /> : <ShieldAlert className="text-destructive size-4" />}
              {data?.enabled ? "Gateway key set" : "MESH_GATEWAY_KEY missing"}
            </CardTitle>
          </CardHeader>
          <p className="flex items-center gap-1.5 px-6 pb-4 text-xs">
            {data?.signing ? <ShieldCheck className="size-3.5 text-emerald-600" /> : <ShieldAlert className="text-destructive size-3.5" />}
            {data?.signing ? "Packets signed (MESH_HMAC_KEY)" : "MESH_HMAC_KEY missing: packets unverified"}
          </p>
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-[1fr_380px]">
        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Radio className="size-4" /> Gateway phones
              </CardTitle>
              <CardDescription>
                bitchat phones with signal that carry the mesh to this control room and broadcast its alerts back.
              </CardDescription>
            </CardHeader>
            <CardContent>
              {gateways.length ? (
                <ul className="divide-y">
                  {gateways.map((g) => (
                    <li key={g.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2.5 text-sm">
                      <div className="flex min-w-48 flex-1 items-center gap-2">
                        <Dot n={g} />
                        <Smartphone className="size-4" />
                        <div>
                          <div className="font-medium">{g.label ?? "bitchat phone"}</div>
                          <div className="text-muted-foreground font-mono text-[11px]">{g.id}</div>
                        </div>
                      </div>
                      <span className="inline-flex items-center gap-1 text-xs">
                        <Users className="size-3.5" /> {g.meta.peers ?? "—"} in mesh range
                      </span>
                      <span className="text-xs">{g.meta.queued ?? 0} queued</span>
                      {g.meta.battery != null && (
                        <span className="inline-flex items-center gap-1 text-xs">
                          <BatteryMedium className="size-3.5" /> {g.meta.battery}%
                        </span>
                      )}
                      <span className="text-muted-foreground text-xs">
                        v{g.meta.appVersion ?? g.meta.app_version ?? "?"}
                      </span>
                      {g.meta.listen === false && <Badge variant="outline">not broadcasting alerts</Badge>}
                      <span className={`text-xs ${live(g) ? "text-emerald-700 dark:text-emerald-400" : "text-muted-foreground"}`}>
                        {live(g) ? "linked" : `last seen ${ago(g.ageS)}`}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-muted-foreground py-4 text-sm">
                  No gateway phone has linked yet. Follow the steps on the right on any phone with internet.
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Camera className="size-4" /> Cameras and phones heard through the mesh
              </CardTitle>
            </CardHeader>
            <CardContent>
              {cameras.length + phones.length ? (
                <ul className="divide-y">
                  {[...cameras, ...phones].map((n) => (
                    <li key={n.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2 text-sm">
                      <Dot n={n} />
                      {n.kind === "phone" ? <Bluetooth className="size-4" /> : <Camera className="size-4" />}
                      <span className="font-medium">{n.label ?? n.id}</span>
                      <Badge variant="outline" className="text-xs">{n.kind}</Badge>
                      {n.lat != null && n.lon != null && (
                        <span className="text-muted-foreground font-mono text-xs">
                          {n.lat.toFixed(4)}, {n.lon.toFixed(4)}
                        </span>
                      )}
                      <span className="text-muted-foreground ml-auto text-xs">{ago(n.ageS)}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-muted-foreground py-4 text-sm">
                  Nothing heard yet. A VLM camera appears here after its first hazard packet reaches a gateway.
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Latest packets</CardTitle>
            </CardHeader>
            <CardContent>
              <ul className="space-y-1 text-xs">
                {(data?.recent ?? []).slice(0, 25).map((p) => (
                  <li key={p.id || p.packetId} className="flex flex-wrap gap-x-2">
                    <span className="text-muted-foreground w-16 tabular-nums">{hhmmss(p.receivedAt)}</span>
                    <span className="w-28">{PACKET_LABEL[p.type] ?? p.type}</span>
                    <span className="font-mono">{p.nodeId ?? "?"}</span>
                    <span className="text-muted-foreground">via {p.gatewayId ?? "?"}</span>
                    <span>→ {p.outcome ?? "received"}</span>
                    {!p.verified && <span className="text-amber-600">unsigned</span>}
                  </li>
                ))}
                {!data?.recent.length && <li className="text-muted-foreground">No packets yet.</li>}
              </ul>
            </CardContent>
          </Card>
        </div>

        <Card className="h-fit">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Link a bitchat phone to this control room</CardTitle>
            <CardDescription>Once per phone. Only the phone that has internet needs it.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            <ol className="list-decimal space-y-2 pl-4">
              <li>Open bitchat → menu → <b>Command Centre Link</b>.</li>
              <li>Turn on <b>Enable sync on reconnect</b>.</li>
              <li>
                Command centre API:
                <div className="mt-1 flex items-center gap-1">
                  <code className="bg-muted flex-1 truncate rounded px-2 py-1 text-xs">{base}</code>
                  <Button size="icon" variant="outline" className="size-7" onClick={() => void copy()} aria-label="Copy URL">
                    {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                  </Button>
                </div>
              </li>
              <li>Gateway key: the <code>MESH_GATEWAY_KEY</code> set on the server.</li>
              <li>City id <code>pune</code>, and turn on <b>Listen to command centre</b>.</li>
              <li>
                Tap <b>Sync now</b>. It should say <i>Linked to command centre</i>, and the phone
                appears on the left within a few seconds.
              </li>
            </ol>
            <div className="text-muted-foreground rounded-md border p-2 text-xs">
              The VLM camera does not need this: it sends to the teammate's phone over Wi-Fi, that phone
              broadcasts on the mesh, and whichever phone here is linked carries it in.
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
