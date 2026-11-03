import { useMemo, useState } from "react"
import { Activity, ArrowRight, ChevronRight, Link2 } from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import type { DemoEvent } from "@/routes/demo/useDemo"

/** What the agents did, and what they joined to what.
 *
 *  Two views of the same append-only table, because it answers two questions.
 *  The one at the top — *what got attached to what* — is the one an officer
 *  asks while an event is running, and it was unanswerable: the raw log says
 *  `assignment.changed`, a uuid, and a JSON payload, and reading it requires
 *  knowing which key in which payload holds the other end of the join. The raw
 *  rows stay underneath, with their causation chain, because that is what an
 *  auditor wants and it is the thing nothing else can reconstruct.
 *
 *  Nothing here is generated for the screen. A database trigger refuses updates
 *  to `events`, so every row below was written by the actor named on it, at the
 *  time shown, with the payload shown.
 */

/** The tone of the raw log rows, keyed on the part of the kind before the dot.
 *  Real kinds are dotted — `report.received`, `assignment.changed` — which is
 *  worth stating because an earlier version of the decoder below was keyed on
 *  underscored names and therefore matched nothing at all: the table rendered
 *  empty and looked like an agent that was not logging. It was logging. */
const TONE: Record<string, string> = {
  incident: "text-orange-600 dark:text-orange-400",
  report: "text-sky-600 dark:text-sky-400",
  assignment: "text-emerald-600 dark:text-emerald-400",
  plan: "text-blue-600 dark:text-blue-400",
  decision: "text-violet-600 dark:text-violet-400",
  demand: "text-red-600 dark:text-red-400",
  resource: "text-cyan-600 dark:text-cyan-400",
  risk: "text-amber-600 dark:text-amber-400",
  field: "text-cyan-600 dark:text-cyan-400",
  alert: "text-fuchsia-600 dark:text-fuchsia-400",
  road: "text-red-600 dark:text-red-400",
  feed: "text-amber-600 dark:text-amber-400",
  shelter: "text-emerald-600 dark:text-emerald-400",
  agency: "text-teal-600 dark:text-teal-400",
  run: "text-muted-foreground",
  world: "text-muted-foreground",
}
const toneFor = (kind: string) => TONE[kind.split(".")[0]] ?? "text-muted-foreground"

const time = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  })

const pretty = (s: string) => s.replace(/_/g, " ")

/** One decoded row: left, a verb, right, and the payload's own sentence. */
type Joined = {
  id: number
  at: string
  kind: string
  left: string
  verb: string
  right: string
  detail: string
  actor: string
}

/** Where the ingested reports come from, said in words rather than in the
 *  column value. "app" and "field" are the two that carry traffic today; a feed
 *  or an IVR line would appear here the moment one writes a report, because
 *  they all come through the same intake door. */
const SOURCE: Record<string, string> = {
  app: "resident app",
  field: "field crew",
  ivr: "phone line",
  sms: "SMS",
  feed: "upstream feed",
}

export default function AgentTrace() {
  const { state } = useDemo()
  const [actor, setActor] = useState<string | null>(null)
  const [open, setOpen] = useState<number | null>(null)
  const [only, setOnly] = useState<"joins" | "ingest" | "allocation">("joins")

  const actors = useMemo(() => {
    const counts = new Map<string, number>()
    for (const e of state.events) counts.set(e.actor, (counts.get(e.actor) ?? 0) + 1)
    return [...counts].sort((a, b) => b[1] - a[1])
  }, [state.events])

  const byId = useMemo(
    () => new Map(state.events.map((e) => [e.id, e])),
    [state.events]
  )

  const chainOf = (e: DemoEvent) => {
    const chain: DemoEvent[] = []
    let cursor: DemoEvent | undefined = e
    const seen = new Set<number>()
    while (cursor && !seen.has(cursor.id)) {
      seen.add(cursor.id)
      chain.push(cursor)
      cursor = cursor.causationId ? byId.get(cursor.causationId) : undefined
    }
    return chain.reverse()
  }

  const rows = actor ? state.events.filter((e) => e.actor === actor) : state.events

  const unitLabel = useMemo(
    () => new Map(state.resources.map((r) => [r.id, r.label] as const)),
    [state.resources]
  )
  const incidentTitle = useMemo(
    () => new Map(state.incidents.map((i) => [i.id, i.title] as const)),
    [state.incidents]
  )
  const wardLabel = useMemo(
    () => new Map(state.wards.map((w) => [w.id, w.name] as const)),
    [state.wards]
  )
  /** Decisions by id, so a `decision.*` row can say what the decision would
   *  actually do — to which unit, at which incident — and why.
   *
   *  The event payload carries the action and the confidence and not the
   *  params. The params are on the decision row: `gate.propose` stores them so
   *  an approval twenty minutes later moves the unit it named. They had never
   *  left the database, which is why the audit could say "Reallocate a unit"
   *  and never which unit. */
  const decisionById = useMemo(
    () => new Map(state.decisions.map((d) => [d.id, d] as const)),
    [state.decisions]
  )

  /** Decoded per kind, not by guessing at the payload.
   *
   *  Each kind stores the other end of its join in a different key — a
   *  re-tasking carries `to_incident`, a fresh assignment carries `resource_id`
   *  and names the assignment as its subject, an uncovered demand names the
   *  incident as its subject and the capability in the payload. One generic
   *  extractor over all of them produces plausible nonsense, so each gets three
   *  lines of its own and every row on screen is a fact rather than a guess.
   *
   *  Ids are resolved against the live world; one that no longer resolves is
   *  shown as the id, because a row that quietly claimed to resolve would be
   *  worse than an ugly one.
   */
  const joined = useMemo<Joined[]>(() => {
    const name = (id: unknown): string | null => {
      if (typeof id !== "string" || !id) return null
      return unitLabel.get(id) ?? incidentTitle.get(id) ?? wardLabel.get(id) ?? id
    }
    const out: Joined[] = []

    for (const e of rows) {
      const p = (e.payload ?? {}) as Record<string, unknown>
      const str = (k: string) => (typeof p[k] === "string" ? (p[k] as string) : "")
      const num = (k: string) =>
        typeof p[k] === "number" ? (p[k] as number) : null
      const ward = name(e.wardId) ?? ""
      let row: Omit<Joined, "id" | "at" | "kind" | "actor"> | null = null

      switch (e.kind) {
        // ------------------------------------------------------- ingestion --
        case "report.received":
          row = {
            left: `Report from the ${SOURCE[str("source")] ?? (str("source") || "intake")}`,
            verb: "read as",
            right: pretty(str("category")) || "unclassified",
            detail:
              `trust ${num("trust")?.toFixed(2) ?? "—"}` +
              (str("verification") ? ` · ${pretty(str("verification"))}` : "") +
              (ward ? ` · ${ward}` : "") +
              (p.injection_suspected === true
                ? " · instruction-like text, treated as data"
                : ""),
          }
          break
        case "report.linked":
          row = {
            left: "Report",
            verb: "merged into",
            right: name(e.subjectId) ?? "an open incident",
            detail:
              str("rationale") ||
              `link score ${num("link_score")?.toFixed(2) ?? "—"}`,
          }
          break
        case "report.rejected":
          row = {
            left: "Report",
            verb: "held, not acted on",
            right: "a human",
            detail:
              `trust ${num("trust")?.toFixed(2) ?? "—"}` +
              (Array.isArray(p.reasons) && p.reasons.length
                ? ` · ${String(p.reasons[0])}`
                : ""),
          }
          break
        case "feed.degraded":
          row = {
            left: `${str("hazard") || "Upstream"} feed`,
            verb: "unavailable, serving",
            right: "the last good copy",
            detail: "Scored from cached data; the console shows the feed as stale.",
          }
          break

        // ------------------------------------------------------- clustering --
        case "incident.opened":
          row = {
            left: `${pretty(str("category")) || "Incident"}, severity ${num("severity") ?? "?"}`,
            verb: "opened in",
            right: ward || "the city",
            detail:
              str("rationale") ||
              (p.needs ? `needs ${Object.keys(p.needs as object).map(pretty).join(", ")}` : ""),
          }
          break
        case "incident.resolved":
          row = {
            left: name(p.resource_id) ?? "A crew",
            verb: "closed",
            right: name(e.subjectId) ?? "an incident",
            detail: "Worked and closed on scene, which is ground truth for every report behind it.",
          }
          break

        // ------------------------------------------------------ allocation --
        case "plan.generated":
          row = {
            left: `${str("engine") || "Solver"} run`,
            verb: "covered",
            right: `${num("demands") ?? "?"} demands with ${num("units") ?? "?"} units`,
            detail:
              str("reason") ||
              `${Math.round((num("coverage") ?? 0) * 100)}% covered` +
              (str("trigger") ? ` · triggered by ${str("trigger")}` : ""),
          }
          break
        case "assignment.created":
          row = {
            left: name(p.resource_id) ?? "A unit",
            verb: "committed for",
            right: pretty(str("capability")) || ward || "a demand",
            detail:
              str("reason") || (num("eta_minutes") != null ? `${num("eta_minutes")} min out` : ""),
          }
          break
        case "assignment.changed":
          row = {
            left: name(e.subjectId) ?? "A unit",
            verb: "re-tasked to",
            right: name(p.to_incident) ?? "another incident",
            // The planner writes the whole sentence here, including what it
            // gave up and why. It is the best line in the log.
            detail:
              str("reason") ||
              (name(p.from_incident) ? `from ${name(p.from_incident)}` : ""),
          }
          break
        case "assignment.rerouted":
          row = {
            left: name(e.subjectId) ?? "A unit",
            // Not a re-tasking, and it must not read as one: the unit, the
            // incident and the assignment are unchanged. Only the road is.
            verb: "given a new road to",
            right: name(p.to_incident) ?? "its task",
            detail: str("reason"),
          }
          break
        case "assignment.cancelled":
          row = {
            left: name(e.subjectId) ?? "A unit",
            verb: "released from",
            right: name(p.from_incident) ?? "its task",
            detail: str("reason") || "Back in the pool; the demand returns to unmet need.",
          }
          break
        case "demand.uncovered":
          row = {
            left: pretty(str("capability")) || "A capability",
            verb: "not covered for",
            right: name(e.subjectId) ?? "an incident",
            detail: str("reason"),
          }
          break

        // -------------------------------------------------- decisions, out --
        case "decision.proposed": {
          const d = e.subjectId ? decisionById.get(e.subjectId) : undefined
          // What it would move, and to what. `reallocate_unit` names both;
          // `preposition_equipment` names a ward; an alert names neither and
          // falls back to the target the decision was raised against.
          const onto =
            name(d?.params?.incident_id) ??
            name(d?.params?.ward_id) ??
            d?.target ??
            ward ??
            "the city"
          row = {
            left:
              (name(d?.params?.resource_id) ? `${name(d?.params?.resource_id)} — ` : "") +
              (str("action") || "A decision"),
            verb: "proposed for",
            right: String(onto),
            detail:
              (d?.rationale ? `${d.rationale} ` : "") +
              `(confidence ${num("confidence")?.toFixed(2) ?? "—"}` +
              (str("action_key") ? `, ${pretty(str("action_key"))}` : "") +
              ")",
          }
          break
        }
        case "decision.gated":
          row = {
            left: pretty(str("action_key")) || "A decision",
            verb: p.within_delegation === true ? "issued under" : "held under",
            right: str("clause") || "the delegation matrix",
            detail: str("reason"),
          }
          break
        case "decision.acted": {
          const d = e.subjectId ? decisionById.get(e.subjectId) : undefined
          row = {
            left:
              (name(d?.params?.resource_id) ? `${name(d?.params?.resource_id)} — ` : "") +
              (str("action") || "A decision"),
            verb: str("status") ? `${str("status")} by` : "settled by",
            right: e.actor.replace(/^officer:/, "") || "an officer",
            // An approval on a decision that carries params is also the moment
            // it is carried out, so the rationale is what the unit moved for.
            detail: d?.rationale ?? "",
          }
          break
        }
        case "alert.issued":
          row = {
            left: `Advisory, severity ${num("severity") ?? "?"}`,
            verb: "issued to",
            right: str("audience") || ward || "a ward",
            detail: `${(num("reach") ?? 0).toLocaleString("en-IN")} residents reached`,
          }
          break

        // ----------------------------------------------------- the rest ----
        case "road.blocked":
          row = {
            left: "Road block",
            verb: "reported at",
            right: name(p.incident_id) ?? (ward || "a point"),
            detail:
              str("reason") +
              (name(p.resource_id) ? ` · by ${name(p.resource_id)}` : ""),
          }
          break
        case "risk.updated":
          row = {
            left: `${str("hazard") || "Hazard"} run`,
            verb: "scored",
            right: `${num("scored") ?? "?"} wards`,
            detail:
              (name(p.top_ward) ? `worst ${name(p.top_ward)}` : "") +
              (num("top_score") != null ? ` at ${num("top_score")}` : "") +
              ` · ${num("actionable") ?? 0} actionable`,
          }
          break
        case "shelter.arrival":
          row = {
            left: `${num("party_size") ?? 1} person(s)`,
            verb: "arrived at",
            right: name(e.subjectId) ?? "a shelter",
            detail:
              `${num("admitted") ?? 0} admitted` +
              ((num("turned_away") ?? 0) > 0 ? `, ${num("turned_away")} turned away` : "") +
              ` · now ${num("occupancy") ?? "?"} of ${num("capacity") ?? "?"}`,
          }
          break
        case "agency.requested":
          row = {
            left: `${num("quantity") ?? 1} × ${pretty(str("capability"))}`,
            verb: "requested from",
            right: (str("to") || "another agency").toUpperCase(),
            detail: `asked by ${(str("from") || "us").toUpperCase()}`,
          }
          break
        case "agency.fulfilled":
          row = {
            left: `${num("quantity") ?? 1} × ${pretty(str("capability"))}`,
            verb: "supplied by",
            right: (str("to") || "another agency").toUpperCase(),
            detail: "",
          }
          break
        default:
          // Every other agent step that states its reason (surge ladder, units,
          // aid, convoys, crew rest) is shown with it rather than dropped.
          row = str("reason") || str("title")
            ? {
                left: e.kind.split(".")[0].replace(/_/g, " "),
                verb: e.kind.split(".").slice(1).join(" ").replace(/_/g, " ") || "logged",
                right: name(e.subjectId) ?? e.subjectId ?? "",
                detail: str("reason") || str("title"),
              }
            : null
      }

      if (row) {
        out.push({
          id: e.id,
          at: e.occurredAt,
          kind: e.kind,
          actor: e.actor.replace(/^agent:/, "").replace(/[:_]/g, " "),
          ...row,
        })
      }
    }
    return out
  }, [rows, unitLabel, incidentTitle, wardLabel])

  /** Three lenses on the decoded rows. "Everything that came in" and "what the
   *  solver did with it" are the two questions asked most, and mixing them into
   *  one list is what made the log feel like noise. */
  const lens = useMemo(() => {
    const ingest = new Set([
      "report.received", "report.linked", "report.rejected",
      "incident.opened", "feed.degraded", "road.blocked", "risk.updated",
    ])
    const allocation = new Set([
      "plan.generated", "assignment.created", "assignment.changed",
      "assignment.rerouted", "assignment.cancelled", "demand.uncovered",
      "incident.resolved",
    ])
    if (only === "ingest") return joined.filter((j) => ingest.has(j.kind))
    if (only === "allocation") return joined.filter((j) => allocation.has(j.kind))
    return joined
  }, [joined, only])

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="space-y-1.5">
        <p className="text-muted-foreground max-w-3xl text-sm">
          Every agent action, as the agent wrote it. The table below decodes each
          row into what was joined to what; the raw rows and their causation
          chains are underneath, untouched.
        </p>
      </div>

      {/* --------------------------------------------------------- joins -- */}
      <Card>
        <CardHeader className="gap-3 pb-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2 text-base">
                <Link2 className="size-4" /> What is attached to what
              </CardTitle>
              <CardDescription>
                Newest first, live. Decoded per event kind, so each row is a fact
                from one stored payload rather than a guess across all of them.
              </CardDescription>
            </div>
            <div className="flex shrink-0 gap-1">
              {(
                [
                  ["joins", "Everything"],
                  ["ingest", "Coming in"],
                  ["allocation", "Allocation"],
                ] as const
              ).map(([k, label]) => (
                <Button
                  key={k}
                  size="sm"
                  variant={only === k ? "secondary" : "ghost"}
                  className="h-8"
                  onClick={() => setOnly(k)}
                >
                  {label}
                </Button>
              ))}
            </div>
          </div>
        </CardHeader>
        <CardContent className="px-0 pb-2">
          <div className="max-h-[30rem] overflow-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[5.5rem] pl-6">Time</TableHead>
                  <TableHead>What</TableHead>
                  <TableHead>Joined to</TableHead>
                  <TableHead className="w-[11rem]">By</TableHead>
                  <TableHead className="pr-6">In its own words</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {lens.map((j) => (
                  <TableRow key={j.id}>
                    <TableCell className="text-muted-foreground py-3 pl-6 align-top tabular-nums">
                      {time(j.at)}
                    </TableCell>
                    <TableCell className="max-w-[15rem] py-3 align-top">
                      <div className="truncate font-medium">{j.left}</div>
                      <div className={`mt-0.5 text-xs ${toneFor(j.kind)}`}>{j.verb}</div>
                    </TableCell>
                    <TableCell className="max-w-[16rem] py-3 align-top">
                      <div className="flex items-start gap-1.5">
                        <ArrowRight className="text-muted-foreground mt-1 size-3 shrink-0" />
                        <span className="truncate">{j.right}</span>
                      </div>
                    </TableCell>
                    <TableCell className="py-3 align-top">
                      <Badge variant="outline" className="font-normal">{j.actor}</Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground max-w-[26rem] py-3 pr-6 align-top text-xs leading-relaxed">
                      {j.detail || "—"}
                    </TableCell>
                  </TableRow>
                ))}
                {lens.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={5} className="text-muted-foreground py-6 pl-6">
                      {state.events.length
                        ? "Nothing in this lens yet."
                        : "Nothing written yet. Start the world on the live map and the first rows arrive within a second."}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      {/* ----------------------------------------------------- raw rows -- */}
      <Card>
        <CardHeader className="gap-3 pb-4">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2 text-base">
              <Activity className="size-4" /> Append-only event log
            </CardTitle>
            <CardDescription>
              The rows as stored. Click one to follow it back to what caused it.
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <Button
              size="sm"
              variant={actor === null ? "secondary" : "ghost"}
              className="h-8"
              onClick={() => setActor(null)}
            >
              Everything
              <span className="text-muted-foreground ml-1.5">{state.events.length}</span>
            </Button>
            {actors.map(([a, n]) => (
              <Button
                key={a}
                size="sm"
                variant={actor === a ? "secondary" : "ghost"}
                className="h-8"
                onClick={() => setActor(a)}
              >
                {a.replace(/^agent:/, "").replace(/_/g, " ")}
                <span className="text-muted-foreground ml-1.5">{n}</span>
              </Button>
            ))}
          </div>
        </CardHeader>
        <CardContent className="max-h-[36rem] space-y-1 overflow-y-auto">
          {rows.map((e) => {
            const expanded = open === e.id
            const chain = expanded ? chainOf(e) : []
            return (
              <div key={e.id}>
                <button
                  type="button"
                  onClick={() => setOpen(expanded ? null : e.id)}
                  className="hover:bg-muted/50 flex w-full items-start gap-2.5 rounded-md px-2 py-1.5 text-left text-sm"
                >
                  <ChevronRight
                    className={`mt-1 size-3.5 shrink-0 transition-transform ${
                      expanded ? "rotate-90" : ""
                    }`}
                  />
                  <span className="text-muted-foreground shrink-0 tabular-nums">
                    {time(e.occurredAt)}
                  </span>
                  <Badge variant="outline" className="shrink-0 font-normal">
                    {e.actor.replace(/^agent:/, "").replace(/_/g, " ")}
                  </Badge>
                  <span className={`shrink-0 text-xs ${toneFor(e.kind)}`}>{e.kind}</span>
                  <span className="min-w-0 flex-1 truncate">{e.text}</span>
                </button>

                {expanded && (
                  <div className="bg-muted/30 mb-2 ml-7 space-y-3 rounded-md border p-3 text-sm">
                    <div>
                      <div className="text-muted-foreground text-xs font-medium uppercase tracking-wide">
                        Caused by
                      </div>
                      {chain.length <= 1 ? (
                        <p className="text-muted-foreground mt-1.5">
                          Nothing. This is the head of its chain.
                        </p>
                      ) : (
                        <ol className="mt-1.5 space-y-1.5">
                          {chain.map((c) => (
                            <li key={c.id} className="flex gap-2.5">
                              <span className="text-muted-foreground tabular-nums">
                                {time(c.occurredAt)}
                              </span>
                              <span className={`text-xs ${toneFor(c.kind)}`}>{c.kind}</span>
                              <span className="min-w-0 flex-1">{c.text}</span>
                            </li>
                          ))}
                        </ol>
                      )}
                    </div>
                    <div>
                      <div className="text-muted-foreground text-xs font-medium uppercase tracking-wide">
                        Subject
                      </div>
                      <div className="mt-1.5">
                        {e.subjectType ?? "—"}
                        {e.subjectId ? ` · ${e.subjectId}` : ""}
                        {e.wardId ? ` · ${wardLabel.get(e.wardId) ?? e.wardId}` : ""}
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground text-xs font-medium uppercase tracking-wide">
                        Payload as stored
                      </div>
                      <pre className="mt-1.5 overflow-x-auto whitespace-pre-wrap break-words text-xs leading-relaxed">
                        {JSON.stringify(e.payload ?? {}, null, 2)}
                      </pre>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </CardContent>
      </Card>
    </div>
  )
}
