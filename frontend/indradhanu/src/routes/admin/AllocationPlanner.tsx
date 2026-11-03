import { useMemo } from "react"
import { ArrowRight, Handshake, Loader2, Route, Zap } from "lucide-react"
import { Link } from "react-router-dom"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import type { RawChange } from "@/routes/demo/useDemo"
import { ENGINE } from "@/lib/plain"

/** What the last solve decided, and what it cost to decide it.
 *
 *  The useful artefact of a re-plan is not the plan, it is the diff. An officer
 *  cannot check a fresh assignment of twenty-three units against their own
 *  judgement, but they can check four changes and the sentence attached to each
 *  one. Kept units are shown too, because "nothing moved" is a result and a
 *  screen that only lists changes cannot tell you it happened.
 */

const LABELS = {
  reassigned: "Re-tasked",
  assigned: "Newly tasked",
  released: "Stood down",
  kept: "Left alone",
} as const

const GROUPS: { key: keyof typeof LABELS; tone: string }[] = [
  { key: "reassigned", tone: "border-violet-500/40 bg-violet-500/5" },
  { key: "assigned", tone: "border-emerald-500/40 bg-emerald-500/5" },
  { key: "released", tone: "border-slate-500/40 bg-slate-500/5" },
  { key: "kept", tone: "border-sky-500/40 bg-sky-500/5" },
]

function ChangeRow({ c }: { c: RawChange }) {
  return (
    <div className="rounded border p-2 text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="outline">{c.resource_label}</Badge>
        {c.from_incident_title && (
          <>
            <span className="text-muted-foreground truncate">{c.from_incident_title}</span>
            <ArrowRight className="text-muted-foreground size-3 shrink-0" />
          </>
        )}
        <span className="truncate font-medium">
          {c.incident_title || "no task"}
        </span>
        {c.eta_minutes > 0 && (
          <span className="text-muted-foreground tabular-nums">
            {c.eta_minutes} min
          </span>
        )}
      </div>
      {c.reason && <div className="text-muted-foreground mt-1">{c.reason}</div>}
    </div>
  )
}

export default function AllocationPlanner() {
  const { state, busy, run } = useDemo()
  const plan = state.plan

  const fleet = useMemo(() => {
    const byStatus = new Map<string, number>()
    for (const r of state.resources) {
      byStatus.set(r.status, (byStatus.get(r.status) ?? 0) + 1)
    }
    return byStatus
  }, [state.resources])

  const wardName = useMemo(
    () => new Map(state.wards.map((w) => [w.id, w.name] as const)),
    [state.wards]
  )
  const byIncident = useMemo(
    () => new Map(state.incidents.map((i) => [i.id, i] as const)),
    [state.incidents]
  )
  /** Shortfalls somebody has already sent out. Offering "ask another agency"
   *  for a request that is sitting unanswered is how the same gap gets asked
   *  for twice, which is the duplicate-dispatch problem wearing a different
   *  hat. */
  const alreadyAsked = useMemo(
    () =>
      new Set(
        state.agencyRequests
          .filter((r) => r.status === "requested" || r.status === "acknowledged")
          .map((r) => `${r.incidentId}:${r.capability}`)
      ),
    [state.agencyRequests]
  )

  const unmet = state.needs.filter((n) => n.met < n.required)
  const shortBy = unmet.reduce((n, x) => n + (x.required - x.met), 0)

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-center gap-2">
        <Button
          onClick={() => run("replan", "/demo/replan")}
          disabled={busy !== null}
        >
          {busy === "replan" ? (
            <Loader2 className="size-4 animate-spin" />
          ) : (
            <Zap className="size-4" />
          )}
          Re-plan now
        </Button>
        <div className="text-muted-foreground flex flex-wrap items-center gap-3 text-xs">
          {[...fleet].map(([status, n]) => (
            <span key={status}>
              {n} {status.replace(/_/g, " ")}
            </span>
          ))}
          {shortBy > 0 && (
            <Badge variant="destructive">
              {shortBy} unit(s) short across {unmet.length} need(s)
            </Badge>
          )}
        </div>
      </div>

      {!plan ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <Route className="size-4" /> No plan yet
            </CardTitle>
            <CardDescription>
              The planner runs when new reports have changed the picture, at most
              once every six ticks, and on demand from the button above. Until it
              has run there is nothing to check, so nothing is shown.
            </CardDescription>
          </CardHeader>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm">{plan.headline}</CardTitle>
              <CardDescription>
                {/* "cp-sat" is the name of an algorithm. A commissioner needs
                    to know a solver decided this and not a person, which is
                    what the words say and the acronym does not. */}
                Solved by the {ENGINE[plan.engine] ?? plan.engine}
                {plan.engine === "cp-sat" ? " (CP-SAT)" : ""}.{" "}
                {(plan.coverage * 100).toFixed(0)}% of recorded demand has a
                unit against it.
                A unit already on its way costs something to move, and more the
                closer it is, which is why most of them stay put.
              </CardDescription>
            </CardHeader>
            {plan.uncovered.length > 0 && (
              <CardContent className="space-y-1.5">
                <div className="text-muted-foreground text-xs font-medium uppercase tracking-wide">
                  Could not be covered
                </div>
                {/* This list used to be the raw payload: a ward UUID, a colon,
                    and the solver's sentence. Three things were missing and all
                    three are what an officer needs — which ward in words, what
                    capability is short, and something to do about it. A gap the
                    municipal fleet cannot close is closed by asking somebody
                    else, and the screen that does that already exists; it just
                    had no route from the place the shortfall is discovered. */}
                {plan.uncovered.map((u, i) => {
                  const incident = u.incident_id
                    ? byIncident.get(u.incident_id)
                    : undefined
                  const ward = u.ward_id
                    ? wardName.get(u.ward_id)
                    : incident
                      ? wardName.get(incident.wardId)
                      : undefined
                  const asked = u.incident_id && u.capability
                    ? alreadyAsked.has(`${u.incident_id}:${u.capability}`)
                    : false
                  return (
                    <div
                      key={i}
                      className="flex flex-wrap items-start justify-between gap-2 rounded border border-destructive/30 p-2"
                    >
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1.5 text-xs">
                          {u.capability && (
                            <Badge variant="outline" className="font-normal">
                              {u.capability.replace(/_/g, " ")}
                            </Badge>
                          )}
                          <span className="font-medium">
                            {incident?.title ?? "Unmatched demand"}
                          </span>
                          {ward && (
                            <span className="text-muted-foreground">{ward}</span>
                          )}
                        </div>
                        <p className="text-muted-foreground mt-0.5 text-xs">
                          {u.reason}
                        </p>
                      </div>
                      {u.capability && (
                        asked ? (
                          <Badge variant="secondary" className="shrink-0 font-normal">
                            Already asked
                          </Badge>
                        ) : (
                          <Button asChild size="sm" variant="outline"
                                  className="h-7 shrink-0 text-xs">
                            <Link
                              to={`/admin/handoff?capability=${encodeURIComponent(u.capability)}${
                                u.incident_id
                                  ? `&incident=${encodeURIComponent(u.incident_id)}`
                                  : ""
                              }`}
                            >
                              <Handshake className="size-3" /> Ask another agency
                            </Link>
                          </Button>
                        )
                      )}
                    </div>
                  )
                })}
              </CardContent>
            )}
          </Card>

          <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-4">
            {GROUPS.map(({ key, tone }) => {
              const list = plan[key] ?? []
              return (
                <Card key={key} className={list.length ? tone : undefined}>
                  <CardHeader className="pb-3">
                    <CardTitle className="text-sm">
                      {LABELS[key]}{" "}
                      <span className="text-muted-foreground tabular-nums font-normal">
                        {list.length}
                      </span>
                    </CardTitle>
                  </CardHeader>
                  <CardContent className="max-h-[420px] space-y-1.5 overflow-y-auto">
                    {list.map((c, i) => (
                      <ChangeRow key={`${c.resource_id}-${i}`} c={c} />
                    ))}
                    {list.length === 0 && (
                      <p className="text-muted-foreground text-xs">None.</p>
                    )}
                  </CardContent>
                </Card>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
