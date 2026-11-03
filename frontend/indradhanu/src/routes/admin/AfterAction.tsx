import { useMemo, useState } from "react"
import {
  CornerDownRight, FileText, History, Radio, ShieldAlert, ShieldCheck,
} from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty } from "./DecisionGate"
import BenchmarkCard from "./BenchmarkCard"

/** The record, replayed from the audit log rather than reconstructed.
 *
 *  Every other screen shows the world as it is now. This one shows how it got
 *  that way, and it is built out of `events` — the append-only table the agents
 *  write to as they work — rather than out of the current rows. The difference
 *  matters: current rows have forgotten the reassignment that happened at
 *  tick 40, and the audit log has not.
 *
 *  `causationId` is why the chain reads as a chain. Each event names the event
 *  that caused it, so "a resident reported water on Ganeshkhind Road" and "a
 *  pump was reassigned away from Aundh" are one thread and can be shown as one,
 *  which is the thing an auditor actually asks for: not what was decided, but
 *  what made you decide it.
 */

const time = (iso: string | null) =>
  iso
    ? new Date(iso).toLocaleTimeString(undefined, {
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
      })
    : "—"

/** Tone, keyed on the real event kinds.
 *
 *  These were underscored — `incident_opened`, `assignment_created` — and the
 *  kinds in the table are dotted: `incident.opened`, `assignment.created`. So
 *  every key here missed, and the `HEADLINE` set below missed too, which meant
 *  the "key events only" view of this screen has been **empty since it was
 *  written**. A filter that silently matches nothing looks exactly like a quiet
 *  run.
 */
const TONE: Record<string, string> = {
  "incident.opened": "text-orange-600 dark:text-orange-400",
  "report.received": "text-muted-foreground",
  "report.linked": "text-sky-600 dark:text-sky-400",
  "report.rejected": "text-red-600 dark:text-red-400",
  "decision.proposed": "text-blue-600 dark:text-blue-400",
  "decision.gated": "text-blue-600 dark:text-blue-400",
  "decision.acted": "text-blue-600 dark:text-blue-400",
  "alert.issued": "text-violet-600 dark:text-violet-400",
  "assignment.created": "text-emerald-600 dark:text-emerald-400",
  "assignment.changed": "text-violet-600 dark:text-violet-400",
  "assignment.rerouted": "text-amber-600 dark:text-amber-400",
  "assignment.cancelled": "text-muted-foreground",
  "demand.uncovered": "text-red-600 dark:text-red-400",
  "incident.resolved": "text-emerald-600 dark:text-emerald-400",
  "plan.generated": "text-blue-600 dark:text-blue-400",
  "agency.requested": "text-teal-600 dark:text-teal-400",
  "agency.fulfilled": "text-teal-600 dark:text-teal-400",
  "shelter.arrival": "text-emerald-600 dark:text-emerald-400",
  "road.blocked": "text-red-600 dark:text-red-400",
  "risk.updated": "text-amber-600 dark:text-amber-400",
  "feed.degraded": "text-amber-600 dark:text-amber-400",
}

/** The event kinds worth showing when somebody asks for the short version.
 *  Everything else stays available behind "everything". */
const HEADLINE = new Set([
  "incident.opened", "report.linked", "decision.proposed", "decision.acted",
  "alert.issued", "assignment.changed", "incident.resolved",
  "agency.requested", "agency.fulfilled", "demand.uncovered",
  "assignment.rerouted",
])

export default function AfterAction() {
  const { state } = useDemo()
  const [all, setAll] = useState(false)

  const wardName = useMemo(() => {
    const m = new Map<string, string>()
    for (const w of state.wards) m.set(w.id, w.name)
    return m
  }, [state.wards])

  /** Oldest first: this is a record of what happened, not a feed. */
  const timeline = useMemo(() => {
    const rows = all ? state.events : state.events.filter((e) => HEADLINE.has(e.kind))
    return [...rows].sort((a, b) => a.id - b.id)
  }, [state.events, all])

  /** Which events have a cause that is itself on screen, so the chain can be
   *  drawn without pretending to a link we cannot show. */
  const caused = useMemo(() => {
    const byId = new Map(state.events.map((e) => [e.id, e]))
    const m = new Map<number, string>()
    for (const e of state.events) {
      if (e.causationId == null) continue
      const parent = byId.get(e.causationId)
      if (parent) m.set(e.id, parent.text || parent.kind)
    }
    return m
  }, [state.events])

  const overrides = state.decisions.filter(
    (d) => d.status === "overridden" || d.status === "rejected"
  )
  const auto = state.decisions.filter((d) => d.status === "auto_issued").length
  const byOfficer = state.decisions.filter((d) =>
    ["approved", "overridden", "rejected"].includes(d.status)
  ).length
  const resolved = state.incidents.filter((i) => i.status === "resolved").length
  const reach = state.alerts.reduce((n, a) => n + (a.reach || 0), 0)

  // The benchmark is shown here too. It is a measurement of the dispatch
  // policy rather than of this run, so it has an answer before a run has
  // produced anything — and "how much better is it than what we do now" is a
  // question that gets asked before the demo starts at least as often as after.
  if (!state.events.length) {
    return (
      <div className="space-y-8 p-5 md:p-8 xl:px-10">
        <Empty
          icon={<History className="text-muted-foreground size-8" />}
          title="Nothing to review yet"
          body="Every recommendation, cited clause, approval, override and closure lands in the audit log as it happens, and is replayed here in order with the thing that caused it. Start a run and it fills."
        />
        <BenchmarkCard />
      </div>
    )
  }

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="grid gap-5 md:grid-cols-4">
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Decisions</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{state.decisions.length}</CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground pt-0 text-xs">
            {auto} auto-issued, {byOfficer} by an officer
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Advisories</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{state.alerts.length}</CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground pt-0 text-xs">
            {reach.toLocaleString("en-IN")} residents reached
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Incidents</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{state.incidents.length}</CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground pt-0 text-xs">
            {resolved} resolved
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Unmet demand</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {state.plan?.uncovered.length ?? 0}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground pt-0 text-xs">
            logged, not hidden
          </CardContent>
        </Card>
      </div>

      <BenchmarkCard />

      {state.plan?.headline && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm">Last plan</CardTitle>
          </CardHeader>
          <CardContent className="text-sm">
            {state.plan.headline}{" "}
            <span className="text-muted-foreground">
              ({state.plan.engine}, {Math.round((state.plan.coverage || 0) * 100)}% covered)
            </span>
          </CardContent>
        </Card>
      )}

      {overrides.length > 0 && (
        <Card className="border-amber-500/50">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <ShieldAlert className="size-4" />
              Where a person departed from the recommendation
            </CardTitle>
            <CardDescription>
              The most valuable rows in the system: each one is a labelled
              example of the model being wrong in a way an officer could name.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {overrides.map((d) => (
              <div key={d.id} className="rounded-md border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">{d.action}</span>
                  <Badge variant="outline">{d.status}</Badge>
                </div>
                <p className="text-muted-foreground mt-1 text-sm">{d.target}</p>
                {d.clause && (
                  <p className="text-muted-foreground mt-1 text-xs">
                    held under {d.clause}
                    {d.delegatedTo ? ` · reserved to ${d.delegatedTo.replace(/_/g, " ")}` : ""}
                  </p>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle className="text-sm">Event timeline</CardTitle>
            <Button size="sm" variant="ghost" className="h-7 text-xs"
                    onClick={() => setAll((v) => !v)}>
              {all ? "Key events only" : `Everything (${state.events.length})`}
            </Button>
          </div>
          <CardDescription>
            Straight from the append-only log, oldest first. An indented line is
            the event that caused the one above it.
          </CardDescription>
        </CardHeader>
        <CardContent className="px-0">
          <div className="max-h-[32rem] overflow-auto px-6">
            {timeline.map((e, i) => (
              <div key={e.id} className="flex gap-3 py-2">
                <div className="flex flex-col items-center">
                  <span className="bg-muted flex size-6 shrink-0 items-center justify-center rounded-full">
                    {e.kind === "alert.issued" ? (
                      <Radio className="size-3.5" />
                    ) : (
                      <FileText className="size-3.5" />
                    )}
                  </span>
                  {i < timeline.length - 1 && <span className="bg-border mt-1 w-px flex-1" />}
                </div>
                <div className="min-w-0 flex-1 pb-1">
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className={`text-sm ${TONE[e.kind] ?? ""}`}>
                      {e.text || e.kind.replace(/_/g, " ")}
                    </span>
                    <span className="text-muted-foreground text-xs tabular-nums">
                      {time(e.occurredAt)}
                    </span>
                  </div>
                  <p className="text-muted-foreground text-xs">
                    {e.actor.replace(/[:_]/g, " ")}
                    {e.wardId && ` · ${wardName.get(e.wardId) ?? e.wardId}`}
                  </p>
                  {caused.has(e.id) && (
                    <p className="text-muted-foreground mt-0.5 flex items-start gap-1 text-xs">
                      <CornerDownRight className="mt-0.5 size-3 shrink-0" />
                      <span className="min-w-0 truncate">because: {caused.get(e.id)}</span>
                    </p>
                  )}
                </div>
              </div>
            ))}
          </div>
          <Separator className="my-2" />
          <p className="text-muted-foreground flex items-center gap-2 px-6 text-xs">
            <ShieldCheck className="size-3.5" />
            Nothing here is written by this screen. Every row is a row in the
            events table, with the actor that wrote it.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
