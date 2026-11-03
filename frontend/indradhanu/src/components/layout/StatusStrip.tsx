import { useEffect, useState } from "react"
import { request } from "@/api/httpClient"
import { cn } from "@/lib/utils"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"

const STATE_COLOR: Record<string, string> = {
  live: "bg-sev-1",
  cached: "bg-sev-2",
  down: "bg-sev-5",
}

/** One feed the backend polls, as `GET /api/v1/status` returns it. */
type Feed = { id: string; label: string; state: string; detail: string }
type SystemStatus = { feeds: Feed[] }

/** `@/hooks/useApi` was removed in an earlier cleanup and this file kept
 *  importing it, so nothing here has compiled since. The hook was three lines;
 *  it lives here now rather than in a shared module with one caller. */
function useSystemStatus() {
  const [data, setData] = useState<SystemStatus | null>(null)
  useEffect(() => {
    let alive = true
    const poll = () =>
      request<SystemStatus>("/status")
        .then((d) => { if (alive) setData(d) })
        // A strip that cannot say what the feeds are doing shows nothing,
        // rather than a row claiming everything is down.
        .catch(() => { if (alive) setData(null) })
    void poll()
    const id = setInterval(poll, 30000)
    return () => { alive = false; clearInterval(id) }
  }, [])
  return { data }
}

export function StatusStrip() {
  const { data } = useSystemStatus()
  if (!data) return null

  return (
    <div className="space-y-1 px-2 py-1">
      {data.feeds.map((f: Feed) => (
        <Tooltip key={f.id}>
          <TooltipTrigger asChild>
            <div className="flex items-center gap-2 text-xs">
              <span
                className={cn(
                  "size-1.5 shrink-0 rounded-full",
                  STATE_COLOR[f.state]
                )}
              />
              <span className="truncate text-muted-foreground">{f.label}</span>
              <span className="ml-auto shrink-0 text-[10px] text-muted-foreground/70 uppercase">
                {f.state}
              </span>
            </div>
          </TooltipTrigger>
          <TooltipContent side="right" className="max-w-56">
            <p className="font-medium">{f.label}</p>
            <p className="text-xs">{f.detail}</p>
          </TooltipContent>
        </Tooltip>
      ))}
    </div>
  )
}
