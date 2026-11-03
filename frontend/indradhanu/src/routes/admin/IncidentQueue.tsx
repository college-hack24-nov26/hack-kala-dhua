import { useMemo, useState } from "react"
import { Copy, Siren, TriangleAlert, Users } from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

/** Every open incident, what it needs, and who is on it.
 *
 *  One row per incident, not per report. Four reports of the same flooded road
 *  are one row with a merge count beside it, which is the point of the
 *  deduplication and the only honest way to size the queue.
 */

const SORTS = {
  severity: "Severity",
  newest: "Newest",
  shortfall: "Biggest shortfall",
  reports: "Most reports",
} as const
type SortKey = keyof typeof SORTS

function since(iso: string) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (!Number.isFinite(s) || s < 0) return ""
  if (s < 90) return `${Math.round(s)}s`
  if (s < 5400) return `${Math.round(s / 60)} min`
  return `${Math.round(s / 3600)} h`
}

export default function IncidentQueue() {
  const { state, activity, selected, setSelected, busy, run } = useDemo()
  const [sort, setSort] = useState<SortKey>("severity")
  const [query, setQuery] = useState("")

  const wardName = useMemo(
    () => new Map(state.wards.map((w) => [w.id, w.name] as const)),
    [state.wards]
  )

  /** The solver's own sentence about why an incident got nobody.
   *
   *  "Nothing committed yet" was true and useless — it restated the empty list
   *  above it. The reason exists: the allocator writes one per uncovered demand
   *  and it names the actual constraint ("no unit able to provide water rescue
   *  could reach this ward within 45 minutes without pulling a unit off a
   *  higher-severity ward"). It was on one screen only, keyed by an id nothing
   *  else joined on. Keyed here, an officer finds out whether this is a queue
   *  that is about to clear or a hole somebody has to fill. */
  const whyUncovered = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const u of state.plan?.uncovered ?? []) {
      if (!u.incident_id || !u.reason) continue
      const list = m.get(u.incident_id)
      if (list) list.push(u.reason)
      else m.set(u.incident_id, [u.reason])
    }
    return m
  }, [state.plan])

  const rows = useMemo(() => {
    const needsBy = new Map<string, typeof state.needs>()
    for (const n of state.needs) {
      const list = needsBy.get(n.incidentId)
      if (list) list.push(n)
      else needsBy.set(n.incidentId, [n])
    }
    const unitsBy = new Map<string, typeof state.resources>()
    for (const r of state.resources) {
      if (!r.incidentId) continue
      const list = unitsBy.get(r.incidentId)
      if (list) list.push(r)
      else unitsBy.set(r.incidentId, [r])
    }
    const q = query.trim().toLowerCase()

    const built = state.incidents
      .map((i) => {
        const needs = needsBy.get(i.id) ?? []
        return {
          ...i,
          needs,
          units: unitsBy.get(i.id) ?? [],
          shortfall: needs.reduce((n, x) => n + Math.max(0, x.required - x.met), 0),
          ward: wardName.get(i.wardId) ?? i.wardId,
        }
      })
      .filter(
        (i) =>
          !q ||
          i.title.toLowerCase().includes(q) ||
          i.ward.toLowerCase().includes(q) ||
          i.category.includes(q)
      )

    const cmp: Record<SortKey, (a: typeof built[0], b: typeof built[0]) => number> = {
      severity: (a, b) => b.severity - a.severity || b.shortfall - a.shortfall,
      newest: (a, b) => (a.createdAt < b.createdAt ? 1 : -1),
      shortfall: (a, b) => b.shortfall - a.shortfall || b.severity - a.severity,
      reports: (a, b) => b.reportCount - a.reportCount,
    }
    return built.sort(cmp[sort])
  }, [state.incidents, state.needs, state.resources, wardName, sort, query])

  const uncovered = rows.filter((r) => r.shortfall > 0).length
  const merged = rows.reduce((n, i) => n + Math.max(0, i.reportCount - 1), 0)

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter by title, ward or category"
          className="h-8 max-w-xs text-xs"
        />
        <div className="flex gap-1">
          {(Object.keys(SORTS) as SortKey[]).map((k) => (
            <Button
              key={k}
              size="sm"
              variant={sort === k ? "secondary" : "ghost"}
              className="h-8 text-xs"
              onClick={() => setSort(k)}
            >
              {SORTS[k]}
            </Button>
          ))}
        </div>
        <div className="text-muted-foreground ml-auto flex items-center gap-3 text-xs">
          <span>{rows.length} open</span>
          {merged > 0 && (
            <Badge variant="outline" className="gap-1">
              <Copy className="size-3" />
              {merged} merged away
            </Badge>
          )}
          {uncovered > 0 && (
            <Badge variant="destructive">{uncovered} with a shortfall</Badge>
          )}
        </div>
      </div>

      {rows.length === 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <Siren className="size-4" /> Nothing open
            </CardTitle>
            <CardDescription>
              {state.running
                ? "Reports are arriving; the first incident will appear here within a few seconds."
                : "Start live ingest on the command console and reports will land here."}
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <div className="space-y-2">
        {rows.map((i) => {
          const open = selected === i.id
          return (
            <Card
              key={i.id}
              className={`cursor-pointer transition-colors ${
                open ? "border-primary" : "hover:border-muted-foreground/40"
              }`}
              onClick={() => setSelected(open ? null : i.id)}
            >
              <CardContent className="p-3">
                <div className="flex flex-wrap items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={i.severity >= 4 ? "destructive" : "secondary"}
                        className="tabular-nums"
                      >
                        sev {i.severity}
                      </Badge>
                      <span className="truncate text-sm font-medium">{i.title}</span>
                    </div>
                    <div className="text-muted-foreground mt-1 flex flex-wrap gap-x-3 text-xs">
                      <span>{i.ward}</span>
                      <span>{i.category.replace(/_/g, " ")}</span>
                      <span>{since(i.createdAt)} old</span>
                      <span>confidence {(i.confidence * 100).toFixed(0)}%</span>
                      {i.reportCount > 1 && (
                        <span className="text-sky-600 dark:text-sky-400">
                          {i.reportCount} reports merged
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex shrink-0 flex-wrap items-center gap-1">
                    {i.needs.map((n) => (
                      <Badge
                        key={n.capability}
                        variant={n.met >= n.required ? "secondary" : "destructive"}
                        className="text-xs font-normal"
                      >
                        {n.capability.replace(/_/g, " ")} {n.met}/{n.required}
                      </Badge>
                    ))}
                    {i.needs.length === 0 && (
                      <span className="text-muted-foreground text-xs">no needs recorded</span>
                    )}
                  </div>
                </div>

                {open && (
                  <div className="mt-3 grid gap-3 border-t pt-3 md:grid-cols-2">
                    <div>
                      <div className="text-muted-foreground mb-1 text-xs font-medium uppercase tracking-wide">
                        Units on it
                      </div>
                      {i.units.length === 0 ? (
                        <div className="space-y-1">
                          <p className="text-muted-foreground text-xs">
                            {i.shortfall > 0
                              ? "Nothing committed yet."
                              : "Nothing committed; nothing needed."}
                          </p>
                          {(whyUncovered.get(i.id) ?? []).map((reason, n) => (
                            <p key={n} className="text-destructive text-xs">
                              {reason}
                            </p>
                          ))}
                          {i.shortfall > 0 && !whyUncovered.has(i.id) && (
                            <p className="text-muted-foreground text-xs">
                              The last plan did not rule this out — it has not
                              been solved since this appeared. Re-plan below.
                            </p>
                          )}
                        </div>
                      ) : (
                        i.units.map((r) => (
                          <div key={r.id} className="flex items-center gap-2 text-xs">
                            <Badge variant="outline">{r.label}</Badge>
                            <span className="text-muted-foreground">
                              {r.status.replace(/_/g, " ")}
                              {r.etaMinutes ? `, ${r.etaMinutes} min out` : ""}
                              {r.distanceKm ? `, ${r.distanceKm.toFixed(1)} km` : ""}
                            </span>
                          </div>
                        ))
                      )}
                      {i.shortfall > 0 && (
                        <p className="text-destructive mt-2 inline-flex items-center gap-1 text-xs">
                          <TriangleAlert className="size-3" />
                          Short by {i.shortfall} unit(s). The planner records this
                          rather than silently under-serving it.
                        </p>
                      )}
                      <div className="mt-2 flex flex-wrap gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 text-xs"
                          disabled={busy !== null}
                          onClick={(e) => {
                            e.stopPropagation()
                            void run("replan", "/demo/replan")
                          }}
                        >
                          Re-plan now
                        </Button>

                        {/* The honest version of "make the demo look busy".
                            Three simulated neighbours, each with its own device
                            id — because only *independent* sources corroborate,
                            and a button that reused one id would show no lift at
                            all, correctly. They are labelled as simulated in the
                            database, the inbox and the audit log.

                            The point is watching the threshold get crossed: one
                            report is an unconfirmed rumour, three independent
                            ones clear the auto-confirm floor and the solver may
                            commit a unit. Fabricating forty agreeing reporters
                            instead would be demonstrating the exact attack the
                            trust layer exists to defeat. */}
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-7 text-xs"
                          disabled={busy !== null}
                          title="Files 3 simulated, independent reports on this incident — labelled as simulated everywhere"
                          onClick={(e) => {
                            e.stopPropagation()
                            void run(
                              `corr-${i.id}`,
                              `/demo/incidents/${i.id}/corroborate`,
                              { count: 3 }
                            )
                          }}
                        >
                          <Users className="mr-1 size-3" />
                          3 neighbours report this
                        </Button>
                      </div>
                    </div>
                    <div>
                      <div className="text-muted-foreground mb-1 text-xs font-medium uppercase tracking-wide">
                        What happened
                      </div>
                      {(activity.get(i.id) ?? []).map((a, n) => (
                        <div key={n} className="text-xs">
                          <span className="text-muted-foreground mr-2 tabular-nums">
                            {new Date(a.at).toLocaleTimeString(undefined, {
                              hour: "2-digit", minute: "2-digit", hour12: false,
                            })}
                          </span>
                          {a.text}
                        </div>
                      ))}
                      {(activity.get(i.id)?.length ?? 0) === 0 && (
                        <p className="text-muted-foreground text-xs">
                          Nothing recorded against this one yet.
                        </p>
                      )}
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>
          )
        })}
      </div>
    </div>
  )
}
