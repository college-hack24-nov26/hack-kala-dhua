import { useEffect, useMemo, useState } from "react"
import { useSearchParams } from "react-router-dom"
import {
  CheckCircle2, Inbox, Link2, Loader2, ShieldX, Sparkles, XCircle,
} from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import type { RawReport } from "@/routes/demo/useDemo"

/** What actually entered the system.
 *
 *  Everywhere else shows incidents, and an incident is already several reports
 *  and a judgement about them. This is the layer underneath: each message as it
 *  arrived, in the words it arrived in, with the trust score that was computed
 *  from it and the one decision the system made about it — opened a new
 *  incident, merged into an existing one, or held below the floor and moved
 *  nothing.
 *
 *  That last outcome is the one worth putting on a screen. A system that
 *  quietly drops reports and a system that has nothing to drop look identical
 *  from the outside.
 */

type Outcome = "opened" | "merged" | "unclassified" | "held" | "pending"

/** Reports nothing could classify. They never open an incident on their own:
 *  an officer reads them and says what they are (`/reports/{id}/classify`). */
const isUnclassified = (r: RawReport) => r.category === "unknown_report" && !r.incidentId

function outcomeOf(r: RawReport): Outcome {
  if (isUnclassified(r)) return "unclassified"
  if (r.status === "quarantined" || r.status === "rejected") return "held"
  if (!r.incidentId) return "pending"
  return r.opened ? "opened" : "merged"
}

const OUTCOME: Record<Outcome, { label: string; tone: string; note: string }> = {
  opened: {
    label: "Opened an incident",
    tone: "bg-orange-500/15 text-orange-600 dark:text-orange-400 border-orange-500/30",
    note: "First report of this. It created the incident everything else attaches to.",
  },
  merged: {
    label: "Merged",
    tone: "bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30",
    note: "Matched an incident that already existed, so it did not cause a second dispatch.",
  },
  unclassified: {
    label: "Needs classifying",
    tone: "bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-500/30",
    note: "No keyword, classifier or model could tell what this describes, so it opened no incident. Read it and say what it is.",
  },
  held: {
    label: "Held",
    tone: "bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/30",
    note: "Below the trust floor. It moved nothing and no unit was committed.",
  },
  pending: {
    label: "No incident",
    tone: "bg-slate-500/15 text-slate-600 dark:text-slate-400 border-slate-500/30",
    note: "Accepted but not yet attached to an incident.",
  },
}

const SOURCE: Record<string, string> = {
  app: "Citizen app", field: "Field crew", agency: "Agency feed",
  sensor: "Sensor", sim: "Simulator",
}

const time = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  })

/** The trust score is six components, and the argument for it is that you can
 *  read them. Showing the number without them would be exactly the opacity the
 *  deterministic scoring exists to avoid. */
function TrustBars({ breakdown }: { breakdown: Record<string, unknown> }) {
  const parts = Object.entries(breakdown).filter(
    ([, v]) => typeof v === "number"
  ) as [string, number][]
  if (!parts.length) return null
  return (
    <div className="space-y-1">
      {parts.map(([name, value]) => (
        <div key={name} className="flex items-center gap-2">
          <span className="text-muted-foreground w-32 shrink-0 truncate text-xs">
            {name.replace(/_/g, " ")}
          </span>
          <div className="bg-muted h-1.5 flex-1 overflow-hidden rounded-full">
            <div
              className={`h-full rounded-full ${
                value >= 0.6 ? "bg-emerald-500" : value >= 0.3 ? "bg-amber-500" : "bg-red-500"
              }`}
              style={{ width: `${Math.max(2, Math.min(100, value * 100))}%` }}
            />
          </div>
          <span className="w-10 shrink-0 text-right text-xs tabular-nums">
            {value.toFixed(2)}
          </span>
        </div>
      ))}
    </div>
  )
}

export default function IntakeInbox() {
  const { state, setSelected } = useDemo()
  /** Opened straight onto the held pile when the sidebar sent them here.
   *
   *  The badge counts reports the system refused to act on; landing on
   *  "Everything" and asking an officer to find them among four hundred rows is
   *  the number pointing at a haystack. Read once, at mount, so the filter stays
   *  the officer's to change afterwards. */
  const [params] = useSearchParams()
  const [filter, setFilter] = useState<Outcome | "all">(() =>
    params.get("filter") === "held" ? "held"
    : params.get("filter") === "unclassified" ? "unclassified" : "all"
  )
  const [categories, setCategories] = useState<{ id: string; label: string; lifeSafety: boolean }[]>([])
  const [pick, setPick] = useState<Record<string, string>>({})
  const [classifying, setClassifying] = useState<string | null>(null)
  useEffect(() => {
    request<{ id: string; label: string; lifeSafety: boolean }[]>("/reports/categories", { toast: false })
      .then(setCategories).catch(() => setCategories([]))
  }, [])

  /** An officer says what a held report is; it then opens or joins an incident
   *  through the same clustering as any classified report. */
  async function classify(reportId: string) {
    const category = pick[reportId]
    if (!category) return
    setClassifying(reportId)
    setRulingError(null)
    try {
      await request(`/reports/${reportId}/classify`, {
        method: "POST", body: { category },
        toast: { loading: "Classifying…", success: "Classified. It now goes through clustering like any report." },
      })
    } catch (e) {
      setRulingError(e instanceof Error ? e.message : String(e))
    } finally {
      setClassifying(null)
    }
  }
  const [query, setQuery] = useState("")
  const [open, setOpen] = useState<string | null>(null)
  /** Which report is mid-ruling, and what went wrong on the last attempt. */
  const [ruling, setRuling] = useState<string | null>(null)
  const [rulingError, setRulingError] = useState<string | null>(null)

  /** Record what was actually there.
   *
   *  The only ground truth this system will ever have. Everything else the
   *  trust scorer reads is its own earlier opinion, which over a long run
   *  hardens a first impression of a reporter into a fact — so this button is
   *  not a nicety, it is the input that stops the loop being circular.
   *
   *  `wholeIncident` because that is usually what an officer means: a crew
   *  standing at a flooded junction has just ruled on every report that merged
   *  into it, not only the one that happened to be on screen.
   */
  async function rule(reportId: string, outcome: "confirmed" | "false", wholeIncident: boolean) {
    setRuling(reportId)
    setRulingError(null)
    try {
      await request(`/reports/${reportId}/verdict`, {
        method: "POST",
        body: { outcome, wholeIncident },
      })
      // The inbox polls, so the new verdict and the reporter's revised
      // reliability arrive on the next tick rather than being patched in here.
      // One source of truth for what the database says beats a local guess
      // that can disagree with it.
    } catch (e) {
      setRulingError(e instanceof Error ? e.message : String(e))
    } finally {
      setRuling(null)
    }
  }

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    return state.reports
      .map((r) => ({ ...r, outcome: outcomeOf(r) }))
      .filter((r) => filter === "all" || r.outcome === filter)
      .filter(
        (r) =>
          !q ||
          r.text.toLowerCase().includes(q) ||
          (r.wardName ?? "").toLowerCase().includes(q) ||
          (r.deviceId ?? "").toLowerCase().includes(q) ||
          (r.street ?? "").toLowerCase().includes(q)
      )
  }, [state.reports, filter, query])

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: state.reports.length }
    for (const r of state.reports) {
      const o = outcomeOf(r)
      c[o] = (c[o] ?? 0) + 1
    }
    return c
  }, [state.reports])

  const merged = counts.merged ?? 0

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="grid gap-5 md:grid-cols-4">
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Reports received</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{counts.all}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Became an incident</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{counts.opened ?? 0}</CardTitle>
          </CardHeader>
        </Card>
        <Card className={merged ? "border-sky-500/40" : undefined}>
          <CardHeader className="pb-3">
            <CardDescription>Merged into one</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{merged}</CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            {merged} dispatch{merged === 1 ? "" : "es"} that a queue of raw reports
            would have sent twice.
          </CardContent>
        </Card>
        <Card className={counts.held ? "border-destructive/50" : undefined}>
          <CardHeader className="pb-3">
            <CardDescription>Held below the trust floor</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{counts.held ?? 0}</CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            Committed nothing. Still recorded, still auditable.
          </CardContent>
        </Card>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search the text, ward, street or device"
          className="h-8 max-w-xs text-xs"
        />
        {(["all", "unclassified", "opened", "merged", "held", "pending"] as const).map((k) => (
          <Button
            key={k}
            size="sm"
            variant={filter === k ? "secondary" : "ghost"}
            className="h-8 text-xs"
            onClick={() => setFilter(k)}
          >
            {k === "all" ? "Everything" : OUTCOME[k].label}
            <span className="text-muted-foreground ml-1.5 tabular-nums">
              {counts[k] ?? 0}
            </span>
          </Button>
        ))}
      </div>

      {rows.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <Inbox className="size-4" /> Nothing has come in
            </CardTitle>
            <CardDescription>
              Every report lands here first, whatever channel it arrived on, and
              before anything is decided about it. Start live ingest on the
              command console, or file one from the citizen portal.
            </CardDescription>
          </CardHeader>
        </Card>
      ) : (
        <div className="space-y-1.5">
          {rows.map((r) => {
            const expanded = open === r.id
            const o = OUTCOME[r.outcome]
            return (
              <Card key={r.id} className="overflow-hidden">
                <button
                  type="button"
                  onClick={() => setOpen(expanded ? null : r.id)}
                  className="hover:bg-muted/40 w-full p-3 text-left transition-colors"
                >
                  <div className="flex flex-wrap items-start gap-2">
                    <span className="text-muted-foreground shrink-0 pt-0.5 text-xs tabular-nums">
                      {time(r.createdAt)}
                    </span>
                    <span className="min-w-0 flex-1 text-sm">
                      &ldquo;{r.text || "(no text)"}&rdquo;
                    </span>
                    <Badge variant="outline" className={`shrink-0 ${o.tone}`}>
                      {o.label}
                    </Badge>
                  </div>
                  <div className="text-muted-foreground mt-1 flex flex-wrap gap-x-3 text-xs">
                    <span>{SOURCE[r.source] ?? r.source}</span>
                    {r.wardName && <span>{r.wardName}</span>}
                    {r.street && <span>{r.street}</span>}
                    {r.trust !== null && (
                      <span
                        className={
                          r.trust >= 0.72
                            ? "text-emerald-600 dark:text-emerald-400"
                            : r.trust < 0.35
                              ? "text-red-600 dark:text-red-400"
                              : ""
                        }
                      >
                        trust {(r.trust * 100).toFixed(0)}%
                      </span>
                    )}
                    {r.hasPhoto && <span>photo attached</span>}
                  </div>
                </button>

                {expanded && (
                  <CardContent className="grid gap-5 border-t pt-3 md:grid-cols-2">
                    <div className="space-y-3">
                      <div>
                        <div className="text-muted-foreground mb-1 text-xs font-medium uppercase tracking-wide">
                          How it was read
                        </div>
                        <div className="flex items-center gap-2 text-xs">
                          <Sparkles className="size-3 shrink-0" />
                          <Badge variant="secondary">
                            {(r.classifiedAs ?? r.category).replace(/_/g, " ")}
                          </Badge>
                          {r.classificationConfidence !== null && (
                            <span className="text-muted-foreground">
                              {(r.classificationConfidence * 100).toFixed(0)}% confident
                            </span>
                          )}
                        </div>
                        <p className="text-muted-foreground mt-1 text-xs">
                          Nobody picked this from a dropdown. It was parsed from the
                          sentence above.
                        </p>
                      </div>

                      <div>
                        <div className="text-muted-foreground mb-1 text-xs font-medium uppercase tracking-wide">
                          Why it scored {r.trust !== null ? (r.trust * 100).toFixed(0) : "—"}%
                        </div>
                        <TrustBars breakdown={r.trustBreakdown} />
                        {Object.keys(r.trustBreakdown).length === 0 && (
                          <p className="text-muted-foreground text-xs">
                            No breakdown recorded for this one.
                          </p>
                        )}
                      </div>

                      <div className="text-muted-foreground text-xs">
                        Device <span className="font-mono">{r.deviceId ?? "unknown"}</span>
                        {r.reporter ? ` · ${r.reporter}` : ""}
                        <br />
                        {r.location[1].toFixed(5)}, {r.location[0].toFixed(5)}
                      </div>
                    </div>

                    <div className="space-y-3">
                      <div>
                        <div className="text-muted-foreground mb-1 text-xs font-medium uppercase tracking-wide">
                          What the system did with it
                        </div>
                        <p className="text-xs">{o.note}</p>

                        {r.outcome === "merged" && r.linkScore !== null && (
                          <div className="bg-muted/40 mt-2 rounded border p-2 text-xs">
                            <div className="flex items-center gap-1.5">
                              <Link2 className="size-3 shrink-0" />
                              <span className="font-medium">
                                {(r.linkScore * 100).toFixed(0)}% match
                              </span>
                              {r.linkDecidedBy && (
                                <Badge variant="outline" className="font-normal">
                                  {r.linkDecidedBy}
                                </Badge>
                              )}
                            </div>
                            {r.linkReason && (
                              <p className="text-muted-foreground mt-1">{r.linkReason}</p>
                            )}
                          </div>
                        )}

                        {r.outcome === "unclassified" && (
                          <div className="mt-2 space-y-2 rounded-lg border border-violet-500/30 bg-violet-500/5 p-2.5 text-xs">
                            <p className="font-medium">What is it?</p>
                            <div className="flex flex-wrap items-center gap-1.5">
                              <select
                                value={pick[r.id] ?? ""}
                                onChange={(e) => setPick((p) => ({ ...p, [r.id]: e.target.value }))}
                                className="border-input bg-card h-8 min-w-44 rounded-lg border px-2 text-xs"
                                aria-label="Category"
                              >
                                <option value="">Choose a category…</option>
                                {categories.map((c) => (
                                  <option key={c.id} value={c.id}>{c.label}{c.lifeSafety ? " (life safety)" : ""}</option>
                                ))}
                              </select>
                              <Button size="sm" className="h-8 gap-1.5 text-xs"
                                      disabled={!pick[r.id] || classifying !== null}
                                      onClick={() => void classify(r.id)}>
                                {classifying === r.id ? <Loader2 className="size-3 animate-spin" /> : <Sparkles className="size-3" />}
                                Classify
                              </Button>
                            </div>
                            <p className="text-muted-foreground">
                              It then opens an incident or joins the one already open there, with that category&rsquo;s needs.
                              If it is nothing, use &ldquo;Nothing there&rdquo; below instead.
                            </p>
                          </div>
                        )}

                        {r.outcome === "held" && (
                          <div className="text-destructive mt-2 flex items-start gap-1.5 text-xs">
                            <ShieldX className="mt-0.5 size-3 shrink-0" />
                            <span>
                              Recorded as {r.status}. It is still in the log and an
                              officer can still act on it; what it could not do is
                              commit a unit on its own.
                            </span>
                          </div>
                        )}
                      </div>

                      {/* What was actually there.
                          The system's own read is above; this is the part only
                          a person can supply, and until it existed the trust
                          score was grading its own homework. */}
                      <div>
                        <div className="text-muted-foreground mb-1 text-xs font-medium uppercase tracking-wide">
                          What was actually there
                        </div>

                        {r.verdict ? (
                          <div className="flex flex-wrap items-center gap-1.5 text-xs">
                            {r.verdict === "confirmed" ? (
                              <Badge className="gap-1 border-emerald-500/30 bg-emerald-500/15 text-emerald-600 dark:text-emerald-400">
                                <CheckCircle2 className="size-3" /> Confirmed
                              </Badge>
                            ) : (
                              <Badge className="gap-1 border-red-500/30 bg-red-500/15 text-red-600 dark:text-red-400">
                                <XCircle className="size-3" /> Nothing there
                              </Badge>
                            )}
                            <span className="text-muted-foreground">
                              {r.verdictBy ? `recorded by ${r.verdictBy}` : "recorded"}
                              {r.verdictAt ? ` · ${time(r.verdictAt)}` : ""}
                            </span>
                          </div>
                        ) : (
                          <>
                            <p className="text-muted-foreground mb-1.5 text-xs">
                              Nobody has ruled on this yet. A verdict overrides the
                              automatic status for this row and changes what the
                              reporter&rsquo;s next report scores.
                            </p>
                            <div className="flex flex-wrap gap-1.5">
                              <Button
                                size="sm" variant="outline"
                                className="h-7 gap-1.5 border-emerald-500/40 text-xs"
                                disabled={ruling !== null}
                                onClick={() => void rule(r.id, "confirmed", Boolean(r.incidentId))}
                              >
                                {ruling === r.id
                                  ? <Loader2 className="size-3 animate-spin" />
                                  : <CheckCircle2 className="size-3" />}
                                It was real
                              </Button>
                              <Button
                                size="sm" variant="outline"
                                className="h-7 gap-1.5 border-red-500/40 text-xs"
                                disabled={ruling !== null}
                                onClick={() => void rule(r.id, "false", false)}
                              >
                                <XCircle className="size-3" /> Nothing there
                              </Button>
                            </div>
                            {r.incidentId && (
                              <p className="text-muted-foreground mt-1 text-[11px]">
                                &ldquo;It was real&rdquo; applies to every report merged
                                into this incident. &ldquo;Nothing there&rdquo; applies
                                to this one only — the others may still be real.
                              </p>
                            )}
                          </>
                        )}

                        {rulingError && (
                          <p className="text-destructive mt-1.5 text-xs">{rulingError}</p>
                        )}

                        {/* The reporter's standing, with the honesty column
                            next to it. A reliability built out of the scorer's
                            own opinion is a different number from one built out
                            of officer rulings, and showing them identically is
                            the bug this whole feature exists to fix. */}
                        {/* An anonymous report has no reporter to hold a
                            reputation, so a verdict on it is recorded for the
                            audit trail and adjusts nobody's standing. Saying so
                            matters: an officer ruling on twenty of these and
                            seeing no number move would reasonably conclude the
                            feature is broken. */}
                        {!r.reporterId && (
                          <p className="text-muted-foreground mt-2 text-[11px]">
                            Filed anonymously, so a verdict here is recorded against
                            the report and against no reporter — there is no history
                            to correct. Reports filed from a signed-in account carry
                            one, and those are the ones this loop improves.
                          </p>
                        )}

                        {r.reporterReliability !== null && (
                          <div className="bg-muted/40 mt-2 rounded border p-2 text-xs">
                            <div className="flex items-center justify-between gap-2">
                              <span className="text-muted-foreground">
                                This reporter&rsquo;s reliability
                              </span>
                              <span className="font-medium tabular-nums">
                                {(r.reporterReliability * 100).toFixed(0)}%
                              </span>
                            </div>
                            <p className="text-muted-foreground mt-0.5">
                              {r.reporterHumanVerdicts
                                ? `From ${r.reporterHumanVerdicts} human verdict(s) out of ${r.reporterTotal ?? 0} report(s).`
                                : `No human verdicts yet — this figure is the scorer's own opinion of ${r.reporterTotal ?? 0} report(s), not ground truth.`}
                            </p>
                          </div>
                        )}
                      </div>

                      {r.incidentId && (
                        <div>
                          <div className="text-muted-foreground mb-1 text-xs font-medium uppercase tracking-wide">
                            Incident it belongs to
                          </div>
                          <Button
                            size="sm"
                            variant="outline"
                            className="h-7 text-xs"
                            onClick={() => setSelected(r.incidentId)}
                          >
                            {r.incidentTitle}
                          </Button>
                          <p className="text-muted-foreground mt-1 text-xs">
                            Severity {r.incidentSeverity} · now {r.incidentReportCount}{" "}
                            report{r.incidentReportCount === 1 ? "" : "s"} in total.
                          </p>
                        </div>
                      )}
                    </div>
                  </CardContent>
                )}
              </Card>
            )
          })}
        </div>
      )}
    </div>
  )
}
