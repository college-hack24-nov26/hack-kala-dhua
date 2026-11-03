import { useMemo, useState } from "react"
import { ArrowRight, Gauge, TriangleAlert } from "lucide-react"
import { Link } from "react-router-dom"
import { useDemo } from "@/routes/demo/DemoProvider"
import { LiveMap } from "@/components/map/LiveMap"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"

/** Which wards are in trouble, and what is actually happening in them.
 *
 *  This read a fixture until now, which is why it sat empty while reports were
 *  visibly arriving on the console next door. It is the same snapshot the map
 *  draws from: ward risk is the agent's score, the counts beside it are the
 *  incidents and units currently in that ward's polygon, and the two cannot
 *  disagree because they are one object.
 */

const SEV_TONE: Record<number, string> = {
  5: "bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/30",
  4: "bg-orange-500/15 text-orange-600 dark:text-orange-400 border-orange-500/30",
  3: "bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30",
  2: "bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30",
  1: "bg-slate-500/15 text-slate-600 dark:text-slate-400 border-slate-500/30",
}

export default function RiskBoard() {
  const { state, activity, setSelected } = useDemo()
  const [query, setQuery] = useState("")

  const rows = useMemo(() => {
    const incidentsBy = new Map<string, number>()
    const severestBy = new Map<string, number>()
    for (const i of state.incidents) {
      incidentsBy.set(i.wardId, (incidentsBy.get(i.wardId) ?? 0) + 1)
      severestBy.set(i.wardId, Math.max(severestBy.get(i.wardId) ?? 0, i.severity))
    }
    const unmetBy = new Map<string, number>()
    const byIncident = new Map(state.incidents.map((i) => [i.id, i] as const))
    for (const n of state.needs) {
      if (n.met >= n.required) continue
      const inc = byIncident.get(n.incidentId)
      if (!inc) continue
      unmetBy.set(inc.wardId, (unmetBy.get(inc.wardId) ?? 0) + (n.required - n.met))
    }

    const q = query.trim().toLowerCase()
    return state.wards
      .filter((w) => !q || w.name.toLowerCase().includes(q) || String(w.number).includes(q))
      .map((w) => ({
        ...w,
        openIncidents: incidentsBy.get(w.id) ?? 0,
        worstIncident: severestBy.get(w.id) ?? 0,
        unmet: unmetBy.get(w.id) ?? 0,
      }))
      .sort(
        (a, b) =>
          (b.severity ?? 0) - (a.severity ?? 0) ||
          (b.score ?? 0) - (a.score ?? 0) ||
          b.openIncidents - a.openIncidents
      )
  }, [state.wards, state.incidents, state.needs, query])

  const scored = rows.filter((r) => r.severity !== null).length
  /** The wards the sidebar sends an officer here for. Severity 4 is the line
   *  above which an advisory is warranted, and the advisory is issued through
   *  the gate rather than from this screen — a board that could broadcast to a
   *  ward directly would be a second path around the delegation check, which is
   *  the one thing in this system that must have no second path. So this names
   *  them and points at the gate. */
  const severe = rows.filter((r) => (r.severity ?? 0) >= 4)
  const atRisk = rows.reduce((n, r) => n + (r.populationAtRisk ?? 0), 0)

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="grid gap-5 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Wards scored</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {scored}
              <span className="text-muted-foreground text-base font-normal">
                {" "}/ {state.wards.length}
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            A ward is scored when the hazard agent has run against a forecast for
            it. Unscored is not the same as safe, and is shown as unscored.
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Estimated people exposed</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {atRisk.toLocaleString()}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            Summed from each ward's latest risk row, not from its population.
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Wards with an open incident</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {rows.filter((r) => r.openIncidents > 0).length}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            {state.running ? "Reports are arriving now." : "Ingest is stopped."}
          </CardContent>
        </Card>
      </div>

      {severe.length > 0 && (
        <Card className="border-amber-500/40">
          <CardContent className="flex flex-wrap items-center justify-between gap-2 p-3">
            <div className="min-w-0">
              <p className="flex items-center gap-1.5 text-sm font-medium">
                <TriangleAlert className="size-4" />
                {severe.length} ward{severe.length === 1 ? "" : "s"} at severity 4
                or above
              </p>
              <p className="text-muted-foreground mt-0.5 truncate text-xs">
                {severe.slice(0, 4).map((w) => w.name).join(", ")}
                {severe.length > 4 ? `, and ${severe.length - 4} more` : ""}
              </p>
            </div>
            <Button asChild size="sm" variant="outline" className="h-7 shrink-0 text-xs">
              <Link to="/admin/decisions">
                Advisories for these <ArrowRight className="size-3" />
              </Link>
            </Button>
          </CardContent>
        </Card>
      )}

      <div className="grid gap-5 lg:grid-cols-[420px_1fr]">
        <Card className="min-w-0">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Gauge className="size-4" /> Wards by risk
            </CardTitle>
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter by name or number"
              className="mt-2 h-8 text-xs"
            />
          </CardHeader>
          <CardContent className="max-h-[560px] space-y-1 overflow-y-auto">
            {rows.map((w) => (
              <div
                key={w.id}
                className="hover:bg-muted/50 rounded-md border p-2 transition-colors"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium">{w.name}</div>
                    <div className="text-muted-foreground text-xs">
                      Ward {w.number} · {w.population?.toLocaleString()} people
                    </div>
                  </div>
                  {w.severity !== null ? (
                    <Badge
                      variant="outline"
                      className={`shrink-0 ${SEV_TONE[w.severity] ?? ""}`}
                    >
                      sev {w.severity} · {((w.score ?? 0) * 100).toFixed(0)}%
                    </Badge>
                  ) : (
                    <Badge variant="outline" className="shrink-0 opacity-60">
                      not scored
                    </Badge>
                  )}
                </div>
                <div className="text-muted-foreground mt-1.5 flex flex-wrap gap-3 text-xs">
                  {w.populationAtRisk ? (
                    <span>{w.populationAtRisk.toLocaleString()} exposed</span>
                  ) : null}
                  {w.openIncidents > 0 && (
                    <span className="text-foreground">
                      {w.openIncidents} open incident{w.openIncidents === 1 ? "" : "s"}
                    </span>
                  )}
                  {w.unmet > 0 && (
                    <span className="text-destructive inline-flex items-center gap-1">
                      <TriangleAlert className="size-3" />
                      {w.unmet} unit(s) short
                    </span>
                  )}
                </div>
                {activity.get(w.id)?.slice(0, 2).map((a, i) => (
                  <div key={i} className="text-muted-foreground mt-1 truncate text-xs italic">
                    {a.text}
                  </div>
                ))}
              </div>
            ))}
            {rows.length === 0 && (
              <p className="text-muted-foreground p-4 text-center text-xs">
                No wards match that filter.
              </p>
            )}
          </CardContent>
        </Card>

        <LiveMap
          className="h-[620px] w-full rounded-lg border"
          wards={state.wards}
          incidents={state.incidents}
          needs={state.needs}
          blocks={state.roadBlocks}
          activity={activity}
          onPickIncident={setSelected}
        />
      </div>
    </div>
  )
}
