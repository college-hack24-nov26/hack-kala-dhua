import { useCallback, useEffect, useState } from "react"
import { Bot, ChevronDown, ChevronRight, Loader2 } from "lucide-react"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"

type Step = {
  n: number; thought?: string; tool?: string; args?: Record<string, unknown>
  result?: unknown; error?: string
}
type Episode = {
  trigger: { kind?: string; summary?: string }
  steps: Step[]; outcome: string; engine: string; seconds: number
  result: Record<string, unknown> | null
}

const TONE: Record<string, string> = {
  proposed: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  no_action: "bg-muted text-muted-foreground",
  running: "bg-sky-500/15 text-sky-700 dark:text-sky-300",
  skipped: "bg-muted text-muted-foreground",
  budget: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  error: "bg-destructive/15 text-destructive",
}

/** "Agent mind": what the Incident Commander was woken by, each thought and
 *  tool call it chose, and how it ended. This is the part that is agentic in
 *  the strict sense — the sequence below was not written by us, it was chosen
 *  from what each tool returned — so it is shown step by step, not summarised. */
export function CommanderPanel() {
  const [episodes, setEpisodes] = useState<Episode[]>([])
  const [open, setOpen] = useState<number | null>(0)
  const [waking, setWaking] = useState(false)

  const load = useCallback(() => {
    request<{ episodes: Episode[] }>("/copilot/commander", { toast: false })
      .then((r) => setEpisodes(r.episodes ?? []))
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [load])

  async function wake() {
    setWaking(true)
    try {
      await request("/copilot/commander/wake", {
        method: "POST",
        body: { summary: "Officer asked for a review of the current situation", cityId: "pune" },
        toast: { loading: "Commander is looking…", success: "Episode finished." },
      })
      setOpen(0)
      load()
    } catch { /* toast already said */ } finally {
      setWaking(false)
    }
  }

  return (
    <div className="space-y-2 rounded-lg border p-2">
      <div className="flex items-center gap-2">
        <Bot className="size-4" />
        <span className="text-xs font-semibold">Incident Commander</span>
        <Button size="sm" variant="ghost" className="ml-auto h-6 px-2 text-[11px]"
                disabled={waking} onClick={() => void wake()}>
          {waking ? <Loader2 className="size-3 animate-spin" /> : "Wake it"}
        </Button>
      </div>
      <p className="text-muted-foreground text-[11px] leading-snug">
        Woken by serious incidents, camera alerts and blocked crews. Chooses its
        own tool calls (read and simulate only), then proposes one action to the
        policy gate or says why nothing is needed. At most 6 steps.
      </p>
      {episodes.length === 0 && (
        <p className="text-muted-foreground text-[11px]">No episodes yet.</p>
      )}
      {episodes.map((e, i) => (
        <div key={i} className="rounded-md border">
          <button type="button" className="flex w-full items-start gap-1.5 p-2 text-left text-[11px]"
                  onClick={() => setOpen(open === i ? null : i)}>
            {open === i ? <ChevronDown className="mt-0.5 size-3" /> : <ChevronRight className="mt-0.5 size-3" />}
            <span className="min-w-0 flex-1">
              <span className="font-medium">{e.trigger.kind ?? "event"}</span>{" "}
              <span className="text-muted-foreground">{e.trigger.summary}</span>
            </span>
            <Badge className={`px-1.5 py-0 text-[10px] ${TONE[e.outcome] ?? ""}`}>
              {e.outcome.replace(/_/g, " ")}
            </Badge>
          </button>
          {open === i && (
            <ol className="space-y-1.5 border-t p-2 text-[11px]">
              {e.steps.map((s) => (
                <li key={s.n} className="space-y-0.5">
                  <div>
                    <span className="text-muted-foreground tabular-nums">{s.n}.</span>{" "}
                    {s.thought && <span className="italic">{s.thought} </span>}
                    {s.tool && <code className="bg-muted rounded px-1">{s.tool}</code>}
                  </div>
                  {s.result !== undefined && (
                    <p className="text-muted-foreground line-clamp-3 break-all pl-3">
                      → {typeof s.result === "string" ? s.result : JSON.stringify(s.result)}
                    </p>
                  )}
                  {s.error && <p className="text-destructive pl-3">{s.error}</p>}
                </li>
              ))}
              {e.result && (
                <li className="border-t pt-1.5">
                  <span className="font-medium">Ended: </span>
                  {String(e.result.reason ?? e.result.summary ?? JSON.stringify(e.result))}
                  <span className="text-muted-foreground"> · {e.engine} · {e.seconds}s</span>
                </li>
              )}
            </ol>
          )}
        </div>
      ))}
    </div>
  )
}
