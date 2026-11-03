import { useEffect, useMemo, useRef, useState } from "react"
import {
  BrainCircuit, ChevronRight, CornerDownLeft, Loader2, Sparkles, Wrench,
} from "lucide-react"
import { request } from "@/api/httpClient"
import { useDemo } from "@/routes/demo/DemoProvider"
import { BlockView, type Block } from "@/components/copilot/blocks"
import { CommanderPanel } from "@/components/copilot/CommanderPanel"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { Alert, AlertDescription } from "@/components/ui/alert"

/** The command console's front door.
 *
 *  The argument for this screen is that the other eleven are organised by *our*
 *  data model — intake, risk, allocation, gate — and a commissioner's questions
 *  are not. "Why is Kothrud above Aundh" crosses four of those screens. "What
 *  happens if we move that pump" crosses none of them, because until now
 *  nothing could answer it without actually moving the pump.
 *
 *  Three columns, and the middle one is the conversation:
 *
 *  * left, the live world, so the answers have something to be checked against
 *    and the operator can see the state move while they read;
 *  * middle, question and rendered answer — tables, comparisons, evidence, not
 *    a wall of prose with numbers embedded in it;
 *  * right, whatever is pending: proposals that have been put to the gate, and
 *    what the gate did with each.
 *
 *  Nothing on this screen can act on its own. The buttons say "put to the
 *  policy gate" rather than "apply" because that is what they do, and the
 *  difference is the product.
 */

type Answer = {
  text: string
  blocks: Block[]
  intent: string
  toolsUsed: string[]
  engine: string
  suggestions: string[]
  note: string | null
  /** What long-term memory the Copilot was reminded of for this answer. */
  memory?: { scope: string; content: string; by: string }[]
}

type Turn = {
  id: number
  question: string
  answer: Answer | null
  error?: string
}

const OPENERS = [
  "What is happening?",
  "Which wards need me first?",
  "Give me mitigation options for the next three hours.",
  "What is waiting for my approval?",
  "Who is on what right now?",
  "What is the forecast for the next few hours?",
  "Who authorises an evacuation?",
]

/** Rendered inside the header's side panel rather than as its own screen.
 *
 *  A judge's first complaint about this console was that it showed them things
 *  they had no business seeing, and a full-screen three-column Copilot in the
 *  primary navigation was the clearest example: an officer with an approval
 *  waiting does not navigate to a chat. So the conversation moved into a panel
 *  that opens over whatever they are doing, and the two side columns — the live
 *  stat rail and the proposal list — are dropped there, because both restate
 *  what the screen behind the panel is already showing.
 *
 *  The full page stays, one level down under Analysis, for the case the panel
 *  is too small for: reading a long comparison table beside the world it came
 *  from. */
export default function Copilot({ compact = false }: { compact?: boolean }) {
  const { state } = useDemo()
  const [turns, setTurns] = useState<Turn[]>([])
  const [question, setQuestion] = useState("")
  const [busy, setBusy] = useState(false)
  const [proposals, setProposals] = useState<Block | null>(null)
  const [applying, setApplying] = useState(false)
  const [showTools, setShowTools] = useState(false)
  const [tools, setTools] = useState<any[] | null>(null)
  const feedRef = useRef<HTMLDivElement>(null)
  /** One conversation per browser tab, so "cancel it" and "apply the second
   *  one" resolve against what was just said. The turns themselves are kept
   *  server-side in Supabase (`copilot_turns`), not in the browser. */
  const sessionId = useMemo(() => {
    const fresh = () =>
      (typeof crypto !== "undefined" && "randomUUID" in crypto)
        ? crypto.randomUUID()
        : `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
    try {
      let v = sessionStorage.getItem("indradhanu.copilotSession")
      if (!v) {
        v = fresh()
        sessionStorage.setItem("indradhanu.copilotSession", v)
      }
      return v
    } catch {
      return fresh()
    }
  }, [])

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: "smooth" })
  }, [turns.length, busy])

  const situation = useMemo(() => {
    const committed = state.resources.filter((r) => r.status !== "available").length
    const critical = state.incidents.filter((i) => i.severity >= 4).length
    const unmet = state.needs.reduce((n, x) => n + Math.max(0, x.required - x.met), 0)
    const waiting = state.decisions.filter((d) => d.status === "awaiting_approval").length
    return { committed, critical, unmet, waiting }
  }, [state])

  async function ask(text: string) {
    const q = text.trim()
    if (!q || busy) return
    const id = Date.now()
    setTurns((t) => [...t, { id, question: q, answer: null }])
    setQuestion("")
    setBusy(true)
    try {
      const answer = await request<Answer>("/copilot/ask", {
        method: "POST",
        body: { question: q, cityId: "pune", sessionId },
      })
      setTurns((t) => t.map((x) => (x.id === id ? { ...x, answer } : x)))
    } catch (e) {
      setTurns((t) =>
        t.map((x) =>
          x.id === id
            ? { ...x, error: e instanceof Error ? e.message : String(e) }
            : x
        )
      )
    } finally {
      setBusy(false)
    }
  }

  /** Put actions to the gate. Never "apply": the gate decides, not this button. */
  async function propose(actions: any[], strategyId?: string) {
    if (applying) return
    setApplying(true)
    try {
      const result = await request<any>("/copilot/apply", {
        method: "POST",
        body: strategyId
          ? { strategyId, cityId: "pune" }
          : { actions, cityId: "pune" },
      })
      setProposals({
        type: "proposals",
        proposals: result.proposals ?? [],
        summary: result.summary,
      })
    } catch (e) {
      setProposals({
        type: "proposals", proposals: [],
        summary: e instanceof Error ? e.message : String(e),
      })
    } finally {
      setApplying(false)
    }
  }

  async function loadTools() {
    setShowTools((v) => !v)
    if (tools) return
    try {
      const r = await request<{ tools: any[] }>("/copilot/tools")
      setTools(r.tools)
    } catch {
      setTools([])
    }
  }

  return (
    <div
      className={
        compact
          ? "flex h-full min-h-0 flex-col gap-3"
          // Fills what is left of the screen under the header, and never more:
          // the conversation scrolls inside its own box and the question box
          // stays on screen. It grew with the transcript before, so the box
          // slid down the page and had to be scrolled to.
          : "grid min-h-0 flex-1 grid-cols-1 grid-rows-[minmax(0,1fr)_auto] gap-4 p-4 md:p-6 lg:grid-cols-[220px_minmax(0,1fr)_320px] lg:grid-rows-[minmax(0,1fr)]"
      }
    >
      {/* The world, so an answer can be checked against it while it is read. */}
      {!compact && (
      <aside className="hidden min-h-0 space-y-2 overflow-auto lg:block">
        <Stat label="Open incidents" value={state.incidents.length} />
        <Stat label="Severity 4+" value={situation.critical} tone={situation.critical ? "bad" : undefined} />
        <Stat label="Units committed" value={`${situation.committed}/${state.resources.length}`} />
        <Stat label="Unmet demand" value={situation.unmet} tone={situation.unmet ? "bad" : undefined} />
        <Stat label="Awaiting approval" value={situation.waiting} tone={situation.waiting ? "warn" : undefined} />
        <Stat label="Advisories out" value={state.alerts.length} />

        <Button variant="ghost" size="sm" className="w-full justify-start gap-1.5 text-xs"
                onClick={() => void loadTools()}>
          <Wrench className="size-3.5" />
          {showTools ? "Hide" : "What can it reach?"}
        </Button>
        {showTools && (
          <div className="max-h-72 space-y-1 overflow-auto rounded-md border p-2">
            {(tools ?? []).map((t) => (
              <div key={t.name} className="text-[11px]">
                <span className="font-medium">{t.name}</span>
                <Badge variant="outline" className="ml-1 px-1 py-0 text-[9px]">{t.tier}</Badge>
                <p className="text-muted-foreground leading-snug">{t.description}</p>
              </div>
            ))}
            {tools?.length === 0 && (
              <p className="text-muted-foreground text-[11px]">Catalogue unavailable.</p>
            )}
          </div>
        )}
        <CommanderPanel />
      </aside>
      )}

      {/* The conversation. */}
      <section className="flex min-h-0 flex-col overflow-hidden rounded-2xl border bg-card shadow-card">
        <div ref={feedRef} className="min-h-0 flex-1 space-y-5 overflow-auto p-4">
          {turns.length === 0 && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <BrainCircuit className="size-5" />
                <div>
                  <h1 className="text-base font-semibold">Commissioner Copilot</h1>
                  <p className="text-muted-foreground text-xs">
                    Ask, analyse, simulate, recommend — and propose. Every number
                    comes from the same database and the same solver the console
                    runs on, and nothing here can act without the policy gate.
                  </p>
                </div>
              </div>
              <div className="grid gap-1.5 sm:grid-cols-2">
                {OPENERS.map((o) => (
                  <button
                    key={o} type="button" onClick={() => void ask(o)}
                    className="hover:bg-muted/50 flex items-center gap-2 rounded-md border p-2 text-left text-xs transition-colors"
                  >
                    <Sparkles className="size-3.5 shrink-0" /> {o}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((t) => (
            <div key={t.id} className="space-y-3">
              <div className="flex justify-end">
                <div className="bg-muted max-w-[85%] rounded-lg px-3 py-2 text-sm">
                  {t.question}
                </div>
              </div>

              {t.error && (
                <Alert variant="destructive">
                  <AlertDescription className="text-xs">{t.error}</AlertDescription>
                </Alert>
              )}

              {t.answer && (
                <div className="space-y-3">
                  <p className="text-sm leading-relaxed">{t.answer.text}</p>
                  {(t.answer.memory?.length ?? 0) > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {t.answer.memory!.map((m, i) => (
                        <span key={i}
                              className="rounded-md border border-dashed px-2 py-0.5 text-[11px] text-muted-foreground"
                              title={`Remembered from ${m.by}. Words only; every number above is from a live tool call.`}>
                          remembered · {m.scope.replace(/_/g, " ")}: {m.content.slice(0, 90)}
                        </span>
                      ))}
                    </div>
                  )}
                  {t.answer.blocks.map((b, i) => (
                    <BlockView key={i} block={b} onPropose={propose} />
                  ))}
                  <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-[11px]">
                    <Badge variant="outline" className="font-normal">{t.answer.intent}</Badge>
                    {t.answer.toolsUsed.map((x) => (
                      <span key={x} className="font-mono">{x}</span>
                    ))}
                    <span className="ml-auto">
                      {t.answer.engine === "fallback"
                        ? "written without a model — the numbers are unaffected"
                        : `narrated by ${t.answer.engine}`}
                    </span>
                  </div>
                  {t.answer.suggestions.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {t.answer.suggestions.map((s) => (
                        <button
                          key={s} type="button" onClick={() => void ask(s)}
                          className="hover:bg-muted/50 flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px]"
                        >
                          {s} <ChevronRight className="size-3" />
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}

          {busy && (
            <div className="text-muted-foreground flex items-center gap-2 text-xs">
              <Loader2 className="size-3.5 animate-spin" />
              Reading the world, re-solving where it has to.
            </div>
          )}
        </div>

        <div className="shrink-0 border-t bg-card p-3">
          <div className="relative">
            <Textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault()
                  void ask(question)
                }
              }}
              placeholder="Ask anything — or say what you want to do."
              className="min-h-[52px] resize-none pr-24 text-sm"
            />
            <Button size="sm" className="absolute bottom-2 right-2 h-7 gap-1 text-xs"
                    disabled={busy || !question.trim()}
                    onClick={() => void ask(question)}>
              {busy ? <Loader2 className="size-3 animate-spin" /> : <CornerDownLeft className="size-3" />}
              Ask
            </Button>
          </div>
        </div>
      </section>

      {/* Whatever is pending. */}
      {!compact && (
      <aside className="max-h-56 min-h-0 space-y-2 overflow-auto rounded-2xl border bg-card p-4 shadow-card lg:max-h-none">
        <div className="text-muted-foreground text-[11px] font-medium uppercase tracking-wide">
          Proposed actions
        </div>
        {applying && (
          <div className="text-muted-foreground flex items-center gap-2 text-xs">
            <Loader2 className="size-3.5 animate-spin" /> Putting it to the gate.
          </div>
        )}
        {!proposals && !applying && (
          <p className="text-muted-foreground text-xs leading-relaxed">
            Nothing proposed yet. Ask for options, or for what would happen if
            you moved a unit, and anything actionable turns up here with the
            clause that governs it.
          </p>
        )}
        {proposals && <BlockView block={proposals} />}

        {state.decisions.filter((d) => d.status === "awaiting_approval").length > 0 && (
          <div className="pt-2">
            <div className="text-muted-foreground mb-1 text-[11px] font-medium uppercase tracking-wide">
              Already on the gate
            </div>
            {state.decisions
              .filter((d) => d.status === "awaiting_approval")
              .slice(0, 5)
              .map((d) => (
                <div key={d.id} className="mb-1.5 rounded-md border border-amber-500/40 p-2 text-xs">
                  <div className="font-medium">{d.action}</div>
                  <p className="text-muted-foreground">{d.clause ?? "no clause matched"}</p>
                </div>
              ))}
            <p className="text-muted-foreground text-[11px]">
              Approve these on the decision gate. Approving now also carries out
              the ones that carry parameters.
            </p>
          </div>
        )}
      </aside>
      )}
    </div>
  )
}

function Stat({
  label, value, tone,
}: {
  label: string
  value: string | number
  tone?: "bad" | "warn"
}) {
  return (
    <div className="rounded-md border p-2">
      <div className="text-muted-foreground text-[11px]">{label}</div>
      <div
        className={`text-lg font-semibold tabular-nums ${
          tone === "bad" ? "text-red-600 dark:text-red-400"
          : tone === "warn" ? "text-amber-600 dark:text-amber-400" : ""
        }`}
      >
        {value}
      </div>
    </div>
  )
}
