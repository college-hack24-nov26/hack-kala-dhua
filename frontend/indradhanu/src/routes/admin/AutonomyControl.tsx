import { useEffect, useRef, useState } from "react"
import { OctagonX, Play } from "lucide-react"
import { request } from "@/api/httpClient"
import { Button } from "@/components/ui/button"

/** The emergency stop, on every admin screen.
 *
 *  Running: a small red button. Paused: a banner across the top saying who
 *  paused it, since when, and how many changes have been held, with Resume.
 *  Pausing stops everything automatic (re-planning, auto-issued decisions, the
 *  agent graph, the LLM Commander, the simulation); it never recalls crews. */

type Autonomy = {
  paused: boolean; by: string | null; reason: string | null
  since: number | null; held_replans: number; pausedForS: number
}

export function AutonomyControl() {
  const [s, setS] = useState<Autonomy | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirm, setConfirm] = useState(false)
  const [tick, setTick] = useState(0)
  const inFlight = useRef(false)

  useEffect(() => {
    let alive = true
    const poll = async () => {
      if (inFlight.current) return
      inFlight.current = true
      try {
        const next = await request<Autonomy>("/ops/autonomy")
        if (alive) setS(next)
      } catch {
        /* not signed in as staff, or the API is older: show nothing */
      } finally {
        inFlight.current = false
      }
    }
    const first = setTimeout(() => void poll(), 0)
    const id = setInterval(() => void poll(), 5000)
    return () => {
      alive = false
      clearTimeout(first)
      clearInterval(id)
    }
  }, [tick])

  async function set(paused: boolean) {
    setBusy(true)
    try {
      const next = await request<Autonomy>("/ops/autonomy", {
        method: "POST",
        body: { paused, reason: paused ? "Emergency stop from the console" : null },
        toast: paused
          ? { loading: "Stopping all automation…", success: "Emergency stop on. Crews already out carry on." }
          : { loading: "Resuming…", success: "Automation resumed; a catch-up re-plan is running." },
      })
      setS(next)
      setConfirm(false)
      setTick((n) => n + 1)
    } finally {
      setBusy(false)
    }
  }

  if (!s) return null

  if (s.paused) {
    const mins = Math.round((s.pausedForS || 0) / 60)
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-lg border-2 border-red-600 bg-red-600/10 p-2.5 text-sm">
        <OctagonX className="size-5 text-red-600" />
        <b className="text-red-700 dark:text-red-400">EMERGENCY STOP — automation paused</b>
        <span className="text-muted-foreground">
          by {s.by ?? "an officer"}{mins ? ` · ${mins} min` : ""} · {s.held_replans} change(s) held ·
          decisions wait for a person · crews already out carry on
        </span>
        <Button size="sm" className="ml-auto" disabled={busy} onClick={() => void set(false)}>
          <Play className="size-3.5" /> Resume operations
        </Button>
      </div>
    )
  }

  return (
    <div className="flex justify-end">
      {confirm ? (
        <div className="flex items-center gap-2 rounded-md border border-red-600/60 p-1.5 text-xs">
          <span>Pause all automation? Crews already out are not recalled.</span>
          <Button size="sm" variant="destructive" className="h-7" disabled={busy} onClick={() => void set(true)}>
            Stop everything
          </Button>
          <Button size="sm" variant="outline" className="h-7" onClick={() => setConfirm(false)}>Cancel</Button>
        </div>
      ) : (
        <Button size="sm" variant="outline" className="h-7 border-red-600/60 text-red-700 dark:text-red-400"
                onClick={() => setConfirm(true)}>
          <OctagonX className="size-3.5" /> Emergency stop
        </Button>
      )}
    </div>
  )
}
