import { useRef, useState } from "react"
import { Drone, MapPin, Upload } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { localizeFrame, type DroneFix } from "./droneApi"

/** Where is this drone frame? Each camera frame is matched against reference
 *  imagery of the area and placed at the matching tile's coordinates. */
export default function DronePanel({ items, error }: { items: DroneFix[]; error: string | null }) {
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const latest = items[0]

  const pick = async (f: File | undefined) => {
    if (!f) return
    setBusy(true)
    setMsg("Matching the frame against the area's reference imagery…")
    try {
      const r = await localizeFrame(f)
      setMsg(r.accepted ? `Placed at ${r.lat?.toFixed(5)}, ${r.lon?.toFixed(5)}${r.place ? ` · ${r.place}` : ""}` : `Not placed: ${r.reason}`)
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
      if (input.current) input.current.value = ""
    }
  }

  return (
    <Card>
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2 text-base"><Drone className="size-4" /> Drone frame localization</CardTitle>
            <CardDescription>
              Each camera frame is matched against the area's reference imagery and placed on the map without GPS.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <input ref={input} type="file" accept="image/*" className="hidden" id="drone-frame"
              onChange={(e) => void pick(e.target.files?.[0])} />
            <Button size="sm" disabled={busy} onClick={() => input.current?.click()}>
              <Upload className="mr-1 size-4" />{busy ? "Locating…" : "Upload a frame"}
            </Button>
          </div>
        </div>
        {msg && <p className="text-muted-foreground text-sm">{msg}</p>}
        {error && !items.length && <p className="text-muted-foreground text-xs">Waiting for the first frame.</p>}
        {latest && (
          <div className="flex flex-wrap items-center gap-3 rounded-md border p-2">
            {latest.thumb && <img src={latest.thumb} alt="Latest drone frame" className="h-16 w-16 rounded object-cover" />}
            <div className="min-w-0 space-y-0.5 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{latest.drone}</span>
                <Badge variant={latest.accepted ? "default" : "secondary"}>{latest.accepted ? "located" : "no match"}</Badge>
                <span className="text-muted-foreground text-xs">{new Date(latest.at).toLocaleTimeString()}</span>
              </div>
              {latest.accepted ? (
                <div className="flex items-center gap-1 tabular-nums"><MapPin className="size-3.5" />
                  {latest.lat?.toFixed(6)}, {latest.lon?.toFixed(6)}
                  <span className="text-muted-foreground ml-2 text-xs">
                    {latest.inliers} matches · {latest.errorPx} px · {latest.processingMs} ms{latest.wardId ? ` · ${latest.wardId}` : ""}{latest.swarmTask ? (latest.swarmTask.ok ? ` · swarm assess drone sent${latest.swarmTask.incidentId ? ` (${latest.swarmTask.incidentId})` : ""}` : ` · swarm: ${latest.swarmTask.reason ?? "not tasked"}`) : ""}
                  </span>
                </div>
              ) : <div className="text-muted-foreground">{latest.reason}</div>}
              {latest.accepted && latest.place && (
                <div className="font-medium">{latest.place}
                  {latest.address && <span className="text-muted-foreground ml-2 text-xs font-normal">{latest.address}</span>}
                </div>
              )}
            </div>
          </div>
        )}
      </CardHeader>
    </Card>
  )
}
