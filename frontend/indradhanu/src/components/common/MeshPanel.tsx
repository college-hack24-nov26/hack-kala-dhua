import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Bluetooth, Compass, Loader2, Radio, Send, TriangleAlert } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  bearing, distanceM, meshStatus, readMeshInbox, sendReportViaMesh,
  type MeshNotice, type MeshStatus,
} from "@/lib/mesh"

type Point = { lng: number; lat: number }
type Facility = { id: string; name: string; kind: string; location: [number, number] }

/** Mesh mode for the citizen app.
 *
 *  Shown only when the API cannot be reached. Online, nothing changes: the
 *  normal app, live routing and re-routing, the live map. Offline, this panel:
 *
 *   * finds bitchat on this phone and says whether it is usable;
 *   * sends the report text with the GPS fix into the mesh;
 *   * shows the alerts and road closures the control room broadcast, nearest
 *     first, filtered to this location;
 *   * checks the route saved while online against closures heard on the mesh,
 *     and when it is blocked, or there is none, gives a compass heading and a
 *     distance to the nearest known shelter — and says plainly that it is a
 *     heading, not a route, because no street router is reachable.
 */
export function MeshPanel({
  pos, text, onSent, facilities, savedRoute, savedDestination, onViewOnMap,
}: {
  pos: Point
  text: string
  onSent: () => void
  facilities: Facility[]
  savedRoute: number[][] | null
  savedDestination: string | null
  /** Focus the map on a notice that carries a position. */
  onViewOnMap?: (at: [number, number], label: string) => void
}) {
  const [status, setStatus] = useState<MeshStatus | null>(null)
  const [notices, setNotices] = useState<MeshNotice[]>([])
  const [sending, setSending] = useState(false)
  const [sent, setSent] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const since = useRef(0)
  const posRef = useRef(pos)
  posRef.current = pos

  const poll = useCallback(async () => {
    const s = await meshStatus()
    setStatus(s)
    if (!s.reachable || !s.enabled) return
    try {
      const { next, notices: fresh } = await readMeshInbox(since.current, posRef.current)
      since.current = next
      if (fresh.length) {
        setNotices((old) => [...fresh.reverse(), ...old].slice(0, 30))
      }
    } catch {
      /* inbox missing: an older bitchat build without the Indradhanu patch */
    }
  }, [])

  useEffect(() => {
    void poll()
    const id = setInterval(() => void poll(), 5000)
    return () => clearInterval(id)
  }, [poll])

  async function send() {
    if (!text.trim()) return
    setSending(true)
    setError(null)
    try {
      await sendReportViaMesh({ lat: pos.lat, lng: pos.lng, text })
      setSent(
        "Sent into the mesh from this phone. It reaches the control room when " +
        "any phone in the chain has signal; it is not confirmed yet."
      )
      onSent()
    } catch (e) {
      setError(
        (e instanceof Error ? e.message : String(e)) +
        ". Open bitchat, turn on Settings → VLM API, and allow this page to " +
        "access devices on your local network when asked."
      )
    } finally {
      setSending(false)
    }
  }

  const blocks = notices.filter((n) => n.kind === "block" && n.lat !== undefined)

  /** Is the route saved while online still usable, given closures heard since? */
  const routeBlocked = useMemo(() => {
    if (!savedRoute || savedRoute.length < 2) return false
    return blocks.some((b) =>
      savedRoute.some((c) => distanceM({ lng: c[0], lat: c[1] }, { lng: b.lng!, lat: b.lat! }) < (b.radiusM ?? 150))
    )
  }, [savedRoute, blocks])

  const shelter = useMemo(() => {
    const options = facilities
      .filter((f) => /shelter|camp|school|relief/i.test(f.kind))
      .map((f) => ({ ...f, d: distanceM(pos, { lng: f.location[0], lat: f.location[1] }) }))
      .sort((a, b) => a.d - b.d)
    return options[0] ?? null
  }, [facilities, pos])

  const usable = status?.reachable && status.enabled

  return (
    <div className="space-y-3 rounded-lg border border-sky-500/40 bg-sky-500/5 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Radio className="size-4 text-sky-600 dark:text-sky-400" />
        <span className="font-medium">Mesh mode</span>
        <span className="text-muted-foreground">
          The server is out of reach. Reports and alerts go phone to phone over
          Bluetooth through the bitchat app.
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Bluetooth className="size-3.5" />
        {status === null ? (
          <span className="text-muted-foreground">Looking for bitchat on this phone…</span>
        ) : !status.reachable ? (
          <span>
            bitchat not found on this phone. Install the Indradhanu build of
            bitchat and turn on <b>Settings → VLM API</b>.
          </span>
        ) : !status.enabled ? (
          <span>bitchat is running but its API is off: Settings → VLM API.</span>
        ) : (
          <span>
            bitchat connected · <b className="tabular-nums">{status.peers}</b> phone
            {status.peers === 1 ? "" : "s"} in range
            {status.peers === 0 && " — your report waits on this phone until one is"}
          </span>
        )}
      </div>

      <Button size="sm" className="w-full" disabled={!usable || !text.trim() || sending}
              onClick={() => void send()}>
        {sending ? <Loader2 className="size-3.5 animate-spin" /> : <Send className="size-3.5" />}
        Send what I typed through the mesh
      </Button>
      {sent && <p className="text-emerald-700 dark:text-emerald-400">{sent}</p>}
      {error && <p className="text-destructive">{error}</p>}

      {/* Where to go, without a router. */}
      <div className="space-y-1 rounded-md border bg-background/60 p-2">
        <div className="flex items-center gap-1.5 font-medium">
          <Compass className="size-3.5" /> Where to go
        </div>
        {savedRoute && !routeBlocked ? (
          <p>
            Keep to the route saved while you were online
            {savedDestination ? <> to <b>{savedDestination}</b></> : null}. No
            closure heard on the mesh crosses it.
          </p>
        ) : shelter ? (
          <p>
            {routeBlocked && <>A closure heard on the mesh crosses your saved route. </>}
            Nearest known shelter: <b>{shelter.name}</b>, about{" "}
            {shelter.d >= 1000 ? `${(shelter.d / 1000).toFixed(1)} km` : `${shelter.d} m`}{" "}
            to the {bearing(pos, { lng: shelter.location[0], lat: shelter.location[1] })}.
            This is a heading, not a route: no street router is reachable, so
            avoid the closures listed below.
          </p>
        ) : (
          <p className="text-muted-foreground">
            No shelter was saved on this phone before the signal went. Follow
            instructions broadcast below, or stay where you are if you are safe.
          </p>
        )}
      </div>

      {notices.length > 0 && (
        <ul className="space-y-1.5">
          {notices.map((n) => (
            <li key={n.seq} className="flex items-start gap-2">
              <TriangleAlert
                className={`mt-0.5 size-3.5 shrink-0 ${
                  n.kind === "alert" ? "text-destructive" : "text-amber-600"
                }`}
              />
              <span>
                <b className="uppercase">{n.kind === "block" ? "road closed" : n.kind}</b>{" "}
                {n.text}
                {n.distanceM !== undefined && (
                  <span className="text-muted-foreground">
                    {" "}· {n.distanceM >= 1000 ? `${(n.distanceM / 1000).toFixed(1)} km` : `${n.distanceM} m`} away
                  </span>
                )}
                {onViewOnMap && n.lat !== undefined && n.lng !== undefined && (
                  <button
                    type="button"
                    className="ml-1.5 text-sky-600 underline underline-offset-2 dark:text-sky-400"
                    onClick={() => onViewOnMap(
                      [n.lng!, n.lat!],
                      n.kind === "block" ? "Road closed (mesh)" : n.kind === "alert" ? "Alert area (mesh)" : "Mesh notice",
                    )}
                  >
                    View on map
                  </button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
