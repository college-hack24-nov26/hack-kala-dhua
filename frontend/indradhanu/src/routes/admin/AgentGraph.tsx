import { useEffect, useRef, useState } from "react"
import { Check, GitBranch, Loader2, Play, TriangleAlert, X } from "lucide-react"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

/** The LangGraph agent graph: every cycle the event router ran, step by step,
 *  and the plans paused for an officer, with approve / reject. */

type Step = { node: string; text: string; at: number; source?: string; domain?: string }
type Run = {
  run_id: string; city_id: string; trigger: string; started_at: number
  status: "running" | "waiting" | "done" | "failed"; outcome: string | null
  trace: Step[]; pending: Record<string, unknown> | null
  finished_at: number | null; error: string | null
}
type Status = {
  available: boolean; enabled: boolean; importError: string | null
  checkpointer: string; approvalSeverity: number
  guardrails?: Record<string, number>
  contracts?: { node: string; reads: string[]; writes: string[]; agent: string | null }[]
  memoryAccess?: Record<string, { read: string[]; write: string[] }>
  waiting: Run[]; runs: Run[]
}
type Memory = {
  ledger: { at: number; agent: string; op: string; namespace: string; detail: string; runId: string | null }[]
  orders: { id: string; content: string; created_by?: string }[]
}

const NODES = [
  ["triage", "Triage"], ["command", "Commander (S5)"], ["sense", "Sense ×5 (parallel)"],
  ["assess", "Assess needs"], ["domain", "Domain planners (parallel)"], ["optimise", "CP-SAT (dry run)"],
  ["validate", "Validate (retry ≤3)"], ["policy_gate", "Policy gate"], ["human_approval", "Officer approval"],
  ["dispatch", "Dispatch"], ["observe", "Observe"],
] as const

const clock = (s: number | null) =>
  s ? new Date(s * 1000).toLocaleTimeString(undefined, { hour12: false }) : "—"

const OUTCOME: Record<string, string> = {
  dispatched: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  unchanged: "bg-muted text-muted-foreground",
  rejected: "bg-zinc-500/15",
  failed: "bg-red-500/15 text-red-700 dark:text-red-300",
}

export default function AgentGraph() {
  const [data, setData] = useState<Status | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [mem, setMem] = useState<Memory | null>(null)
  const [order, setOrder] = useState("")
  const inFlight = useRef(false)

  // Polled; after an action the next poll (≤2.5 s) shows the result.
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let alive = true
    const poll = async () => {
      if (inFlight.current) return
      inFlight.current = true
      try {
        const [s, m] = await Promise.all([
          request<Status>("/agent-graph"),
          request<Memory>("/agent-graph/memory").catch(() => null),
        ])
        if (alive) {
          setData(s)
          if (m) setMem(m)
          setError(null)
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      } finally {
        inFlight.current = false
      }
    }
    const first = setTimeout(() => void poll(), 0)
    const id = setInterval(() => void poll(), 2500)
    return () => {
      alive = false
      clearTimeout(first)
      clearInterval(id)
    }
  }, [refresh])

  async function runNow() {
    setBusy("run")
    try {
      await request("/agent-graph/run", { method: "POST", body: {}, toast: { loading: "Running the agent graph…", success: "Cycle finished." } })
      setRefresh((n) => n + 1)
    } finally {
      setBusy(null)
    }
  }

  async function answer(id: string, approved: boolean) {
    setBusy(id)
    try {
      await request(`/agent-graph/runs/${id}/resume`, {
        method: "POST", body: { approved },
        toast: { loading: approved ? "Approving…" : "Rejecting…", success: approved ? "Approved; dispatching." : "Rejected; current assignments stand." },
      })
      setRefresh((n) => n + 1)
    } finally {
      setBusy(null)
    }
  }

  async function addOrder() {
    if (order.trim().length < 3) return
    setBusy("order")
    try {
      await request("/agent-graph/orders", {
        method: "POST", body: { text: order.trim() },
        toast: { loading: "Saving standing order…", success: "Standing order saved; every agent will read it." },
      })
      setOrder("")
      setRefresh((n) => n + 1)
    } finally {
      setBusy(null)
    }
  }

  const last = data?.runs[0]
  const visited = new Set((last?.trace ?? []).map((t) => t.node))

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      {error && <Card className="border-destructive/50 p-4 text-sm">Could not load the agent graph: {error}</Card>}
      {data && !data.available && (
        <Card className="border-amber-500/60 p-4 text-sm">
          <TriangleAlert className="mr-1 inline size-4 text-amber-600" />
          LangGraph is not installed on the server ({data.importError}). The event router is re-planning
          directly. Deploy with the updated requirements.txt to switch the graph on.
        </Card>
      )}

      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0 pb-2">
          <div>
            <CardTitle className="flex items-center gap-2 text-sm"><GitBranch className="size-4" /> The cycle</CardTitle>
            <CardDescription>
              Every change in the world runs this graph. Highlighted: the path the latest run took.
              Checkpointer: {data?.checkpointer ?? "—"} · officer approval when a plan pulls a unit off an
              incident of severity {data?.approvalSeverity ?? 4}+.
            </CardDescription>
          </div>
          <Button size="sm" onClick={() => void runNow()} disabled={!data?.enabled || busy === "run"}>
            {busy === "run" ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />} Run now
          </Button>
        </CardHeader>
        <CardContent>
          <ol className="flex flex-wrap items-center gap-1.5 text-xs">
            {NODES.map(([id, label], i) => (
              <li key={id} className="flex items-center gap-1.5">
                <span className={`rounded-md border px-2 py-1 ${visited.has(id) ? "border-primary bg-primary/10 font-medium" : "text-muted-foreground"}`}>
                  {label}
                </span>
                {i < NODES.length - 1 && <span className="text-muted-foreground">→</span>}
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>

      {(data?.waiting.length ?? 0) > 0 && (
        <Card className="border-amber-500/60">
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Waiting for an officer</CardTitle>
            <CardDescription>Nothing is written until you answer. Unanswered plans are rejected automatically after the timeout, keeping units where they are.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {data!.waiting.map((r) => {
              const p = (r.pending ?? {}) as { reason?: string; headline?: string; reassigned?: { resource_label: string; from_incident_title: string; incident_title: string }[] }
              return (
                <div key={r.run_id} className="rounded-md border p-3 text-sm">
                  <div className="font-medium">{p.reason}</div>
                  <div className="text-muted-foreground text-xs">{p.headline} · trigger: {r.trigger}</div>
                  {!!p.reassigned?.length && (
                    <ul className="mt-2 space-y-0.5 text-xs">
                      {p.reassigned.map((c, i) => (
                        <li key={i}>{c.resource_label}: {c.from_incident_title || "—"} → <b>{c.incident_title}</b></li>
                      ))}
                    </ul>
                  )}
                  <div className="mt-2 flex gap-2">
                    <Button size="sm" onClick={() => void answer(r.run_id, true)} disabled={busy === r.run_id}>
                      <Check className="size-3.5" /> Approve
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => void answer(r.run_id, false)} disabled={busy === r.run_id}>
                      <X className="size-3.5" /> Reject
                    </Button>
                  </div>
                </div>
              )
            })}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">Recent runs</CardTitle>
        </CardHeader>
        <CardContent>
          {!data?.runs.length ? (
            <p className="text-muted-foreground text-sm">No runs yet. The next report, incident or road block will start one.</p>
          ) : (
            <ul className="divide-y">
              {data.runs.map((r) => (
                <li key={r.run_id} className="py-2 text-sm">
                  <button className="flex w-full flex-wrap items-center gap-2 text-left" onClick={() => setOpen(open === r.run_id ? null : r.run_id)}>
                    <span className="text-muted-foreground w-16 text-xs tabular-nums">{clock(r.started_at)}</span>
                    <span className="min-w-0 flex-1 truncate">{r.trigger}</span>
                    <Badge variant="outline" className="text-xs">{r.trace.length} steps</Badge>
                    <span className={`rounded px-1.5 py-0.5 text-xs ${OUTCOME[r.outcome ?? ""] ?? "bg-amber-500/15"}`}>
                      {r.status === "waiting" ? "waiting for officer" : r.outcome ?? r.status}
                    </span>
                  </button>
                  {open === r.run_id && (
                    <ol className="mt-2 space-y-1 border-l-2 pl-3 text-xs">
                      {r.trace.map((t, i) => (
                        <li key={i}>
                          <b className="font-mono">{t.node}{t.source ? `[${t.source}]` : t.domain ? `[${t.domain}]` : ""}</b>{" "}
                          <span className="text-muted-foreground">{t.text}</span>
                        </li>
                      ))}
                      {r.error && <li className="text-destructive">{r.error}</li>}
                    </ol>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
      <div className="grid gap-5 xl:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">State contracts</CardTitle>
            <CardDescription>
              Each node sees only what it reads and may change only what it writes; anything else fails
              the step. Guardrails: at most {data?.guardrails?.maxRetaskPerCycle ?? 8} units moved per cycle
              without an officer, coverage may not drop more than{" "}
              {Math.round((data?.guardrails?.coverageDropTolerance ?? 0.05) * 100)}%, run timeout{" "}
              {data?.guardrails?.runTimeoutS ?? 90}s, ≤{data?.guardrails?.maxAttempts ?? 3} solver attempts.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <table className="w-full text-xs">
              <thead className="text-muted-foreground text-left">
                <tr><th className="py-1">Node</th><th>Reads</th><th>Writes</th><th>Memory</th></tr>
              </thead>
              <tbody>
                {(data?.contracts ?? []).map((c) => (
                  <tr key={c.node} className="border-t align-top">
                    <td className="py-1 font-mono">{c.node}</td>
                    <td className="text-muted-foreground">{c.reads.join(", ") || "—"}</td>
                    <td>{c.writes.join(", ") || "trace only"}</td>
                    <td>{c.agent ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Agent memory</CardTitle>
            <CardDescription>
              One namespace per agent. Standing orders are written only by officers and read by every agent;
              “police” is readable by none. Every read, write and refusal is in the ledger.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-xs">
            <div className="flex gap-2">
              <input
                value={order}
                onChange={(e) => setOrder(e.target.value)}
                placeholder="Standing order, e.g. Keep boat 3 at Sangam bridge until 18:00"
                className="bg-background flex-1 rounded-md border px-2 py-1.5"
                maxLength={300}
              />
              <Button size="sm" onClick={() => void addOrder()} disabled={busy === "order" || order.trim().length < 3}>
                Add order
              </Button>
            </div>
            {!!mem?.orders.length && (
              <ul className="space-y-1">
                {mem.orders.map((o) => <li key={o.id}>📌 {o.content}</li>)}
              </ul>
            )}
            <div className="grid grid-cols-[auto_1fr_1fr] gap-x-3 gap-y-0.5">
              <span className="text-muted-foreground">Agent</span>
              <span className="text-muted-foreground">Reads</span>
              <span className="text-muted-foreground">Writes</span>
              {Object.entries(data?.memoryAccess ?? {}).map(([a, r]) => (
                <div key={a} className="contents">
                  <span className="font-mono">{a}</span>
                  <span>{r.read.join(", ")}</span>
                  <span>{r.write.join(", ")}</span>
                </div>
              ))}
            </div>
            <div>
              <div className="text-muted-foreground mb-1">Ledger (latest)</div>
              <ul className="max-h-56 space-y-0.5 overflow-y-auto">
                {(mem?.ledger ?? []).slice(0, 60).map((e, i) => (
                  <li key={i} className={e.op === "denied" ? "text-destructive" : ""}>
                    <span className="text-muted-foreground tabular-nums">{clock(e.at)}</span>{" "}
                    <b className="font-mono">{e.agent}</b> {e.op} <span className="font-mono">{e.namespace}</span>
                    {e.detail ? ` — ${e.detail}` : ""}
                  </li>
                ))}
                {!mem?.ledger.length && <li className="text-muted-foreground">Nothing yet.</li>}
              </ul>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
