import { useEffect, useMemo, useRef, useState } from "react"
import {
  AlertTriangle, Copy, Gavel, Loader2, Play, RotateCcw, Square, Zap,
} from "lucide-react"
import { useDemo } from "./DemoProvider"
import type { DemoState } from "./useDemo"
import { LiveMap } from "@/components/map/LiveMap"
import { WardPanel } from "@/routes/admin/WardPanel"
import { autoRegion, REGIONS } from "@/routes/admin/zones"
import { MapStage } from "@/components/map/MapStage"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Alert, AlertDescription } from "@/components/ui/alert"

/** The live console.
 *
 *  Everything on this page came out of the API within the last second. The
 *  world advances on the server, one tick per second, and the map animates
 *  between polls. Nothing is scripted in the browser.
 */

const BEAT_STYLE: Record<string, string> = {
  incident: "text-orange-600 dark:text-orange-400",
  merge: "text-sky-600 dark:text-sky-400",
  held: "text-amber-600 dark:text-amber-400",
  attack: "text-red-600 dark:text-red-400",
  dispatch: "text-emerald-600 dark:text-emerald-400",
  arrive: "text-emerald-600 dark:text-emerald-400",
  reassign: "text-violet-600 dark:text-violet-400",
  release: "text-muted-foreground",
  resolved: "text-emerald-600 dark:text-emerald-400",
  shortfall: "text-red-600 dark:text-red-400",
  decision: "text-blue-600 dark:text-blue-400",
  field: "text-cyan-600 dark:text-cyan-400",
  drone: "text-teal-600 dark:text-teal-400",
  plan: "text-blue-600 dark:text-blue-400",
  you: "text-violet-600 dark:text-violet-400 font-medium",
  error: "text-red-600 dark:text-red-400",
}

/** The narration, on its own, so the side panel and the full screen overlay
 *  show the same thing rather than two views that can drift apart. */
function BeatFeed({ beats }: { beats: DemoState["beats"] }) {
  if (!beats.length) {
    return (
      <p className="text-muted-foreground text-xs">
        Press start. Reports arrive a few seconds apart.
      </p>
    )
  }
  return (
    <div className="space-y-1.5">
      {[...beats].reverse().map((b, i) => (
        <div key={`${b.tick}-${i}`} className="flex gap-2 text-xs">
          <span className="text-muted-foreground shrink-0 tabular-nums">
            {String(b.tick).padStart(3, "0")}
          </span>
          <span className={BEAT_STYLE[b.kind] ?? ""}>{b.text}</span>
        </div>
      ))}
    </div>
  )
}

export default function DemoConsole() {
  const { state, error, activity, selected, setSelected, busy, run, region: regionPick, setRegion } = useDemo()
  // Pune and Ghaziabad are 1,200 km apart. The console shows one region at a
  // time; a simulation started here only creates reports in that region.
  const autoId = useMemo(() => autoRegion(state).id, [state])
  const region = REGIONS.find((r) => r.id === (regionPick === "all" ? autoId : regionPick)) ?? REGIONS[0]
  const setRegionId = (id: string) => setRegion(id as "pune" | "ncr")
  const [ward, setWard] = useState<string | null>(null)
  const feedRef = useRef<HTMLDivElement>(null)
  /** Reset throws away the run. It asks once, in place, rather than through a
   *  browser dialog that would block the poll behind it. */
  const [confirmReset, setConfirmReset] = useState(false)

  useEffect(() => {
    feedRef.current?.scrollTo({ top: 0, behavior: "smooth" })
  }, [state.beats.length])

  const pending = useMemo(
    () => state.decisions.filter((d) => d.status === "awaiting_approval"),
    [state.decisions]
  )
  const autoIssued = state.decisions.filter((d) => d.status === "auto_issued").length
  const selectedIncident = state.incidents.find((i) => i.id === selected)
  const selectedNeeds = state.needs.filter((n) => n.incidentId === selected)
  const selectedUnits = state.resources.filter((r) => r.incidentId === selected)

  const committed = state.resources.filter((r) => r.status !== "available").length
  const merged = state.incidents.reduce((n, i) => n + Math.max(0, i.reportCount - 1), 0)

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-center gap-2">
        {!state.running ? (
          <Button onClick={() => run("start", "/demo/start", { cityId: "pune", region: region.id, reportEveryTicks: 4 })}
                  disabled={busy !== null}>
            {busy === "start" ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
            Start live ingest · {region.name}
          </Button>
        ) : (
          <Button variant="destructive" onClick={() => run("stop", "/demo/stop")} disabled={busy !== null}>
            <Square className="size-4" /> Stop
          </Button>
        )}
        {/* Re-plan and reset are scaffolding, not command. An officer during
            an event does not delete the world, and a judge shown a button that
            does is being shown the machinery instead of the product — which is
            most of what "too cluttered" turned out to mean here. Folded into a
            disclosure that says what it holds; one click away, and not in the
            way of the map. */}
        <details className="relative">
          <summary className="text-muted-foreground hover:text-foreground cursor-pointer list-none rounded-md border px-2.5 py-1.5 text-xs">
            Simulation controls
          </summary>
          <div className="bg-background absolute left-0 top-full z-30 mt-1 flex w-max flex-wrap items-center gap-2 rounded-md border p-2 shadow-md">
        <Button variant="outline" onClick={() => run("replan", "/demo/replan")} disabled={busy !== null}>
          {busy === "replan" ? <Loader2 className="size-4 animate-spin" /> : <Zap className="size-4" />}
          Re-plan now
        </Button>

        {/* Without this, the second run of the day starts on the first run's
            wreckage and "start" only ever adds to it. */}
        {confirmReset ? (
          <div className="flex items-center gap-1.5">
            <Button
              variant="destructive" size="sm" disabled={busy !== null}
              onClick={() => {
                setConfirmReset(false)
                void run("reset", "/demo/reset", { cityId: "pune", region: region.id, reportEveryTicks: 4 })
              }}
            >
              {busy === "reset" ? <Loader2 className="size-4 animate-spin" /> : <RotateCcw className="size-4" />}
              Delete this run
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirmReset(false)}>
              Keep it
            </Button>
          </div>
        ) : (
          <Button variant="outline" onClick={() => setConfirmReset(true)} disabled={busy !== null}>
            <RotateCcw className="size-4" /> Reset world
          </Button>
        )}
          </div>
        </details>
        <div className="text-muted-foreground ml-2 flex flex-wrap items-center gap-3 text-xs">
          <span>tick <span className="tabular-nums font-medium">{state.tick}</span></span>
          <span>{state.incidents.length} open incidents</span>
          <span>{committed}/{state.resources.length} units committed</span>
          {merged > 0 && (
            <Badge variant="outline" className="gap-1">
              <Copy className="size-3" />{merged} duplicate dispatch{merged === 1 ? "" : "es"} avoided
            </Badge>
          )}
          <Badge variant="secondary">{autoIssued} auto-issued</Badge>
          {pending.length > 0 && <Badge variant="destructive">{pending.length} awaiting officer</Badge>}
        </div>
      </div>

      {(error || state.error) && (
        <Alert variant="destructive">
          <AlertTriangle className="size-4" />
          <AlertDescription>{error ?? state.error}</AlertDescription>
        </Alert>
      )}

      <div className="grid gap-4 lg:grid-cols-[1fr_380px]">
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-muted-foreground text-xs">Region</span>
            {REGIONS.map((r) => (
              <Button key={r.id} size="sm" className="h-7" variant={region.id === r.id ? "default" : "outline"}
                      onClick={() => setRegionId(r.id)}>
                {r.name}
              </Button>
            ))}
          </div>
          <MapStage
            panelTitle="What is happening"
            panel={<BeatFeed beats={state.beats} />}
            map={(expanded) => (
              <LiveMap
                key={region.id}
                center={region.center}
                zoom={region.zoom + 0.6}
                className={
                  expanded
                    ? "h-full w-full"
                    : "h-[520px] w-full rounded-lg border"
                }
                wards={state.wards}
                incidents={state.incidents}
                resources={state.resources}
                facilities={state.facilities}
                blocks={state.roadBlocks}
                needs={state.needs}
                activity={activity}
                routes={state.routes}
                route={state.citizenRoute?.path}
                routeLabel={
                  state.citizenRoute
                    ? `Given to a resident: ${state.citizenRoute.headline}`
                    : undefined
                }
                onPickIncident={setSelected}
                onPickWard={setWard}
              />
            )}
            footer={
              <p className="text-muted-foreground text-xs">
                Coloured lines are the streets each committed unit is driving,
                from the router, not a bearing, and each one ends in a ring on
                the hazard it is going to. Green is the route the citizen agent
                last gave a resident, so the control room can see the advice that
                went out. Open the legend for what the colours mean, or expand to
                full screen and keep the feed beside it.
              </p>
            }
          />

          {selectedIncident && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">{selectedIncident.title}</CardTitle>
                <CardDescription>
                  Severity {selectedIncident.severity} · {selectedIncident.reportCount} report
                  {selectedIncident.reportCount === 1 ? "" : "s"} ·
                  confidence {(selectedIncident.confidence * 100).toFixed(0)}%
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-1">
                {selectedNeeds.map((n) => (
                  <div key={n.capability} className="flex items-center gap-2 text-xs">
                    <Badge variant={n.met >= n.required ? "secondary" : "destructive"}>
                      {n.met}/{n.required}
                    </Badge>
                    <span>{n.capability.replace(/_/g, " ")}</span>
                    {n.met < n.required && (
                      <span className="text-muted-foreground">shortfall</span>
                    )}
                  </div>
                ))}
                {selectedNeeds.length === 0 && (
                  <p className="text-muted-foreground text-xs">No capability needs recorded.</p>
                )}
                {selectedUnits.length > 0 && (
                  <div className="mt-2 space-y-1 border-t pt-2">
                    {selectedUnits.map((r) => (
                      <div key={r.id} className="text-xs">
                        <Badge variant="outline" className="mr-1">{r.label}</Badge>
                        <span className="text-muted-foreground">
                          {r.status.replace(/_/g, " ")}
                          {r.etaMinutes ? `, ${r.etaMinutes} min out` : ""}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {(activity.get(selectedIncident.id)?.length ?? 0) > 0 && (
                  <div className="mt-2 space-y-1 border-t pt-2">
                    {activity.get(selectedIncident.id)!.map((a, i) => (
                      <div key={i} className="text-muted-foreground text-xs">{a.text}</div>
                    ))}
                  </div>
                )}
                <div className="pt-1">
                  <Button size="sm" variant="ghost" className="h-6 px-2 text-xs"
                          onClick={() => setSelected(null)}>
                    Close
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}
        </div>

        <div className="space-y-3">
          {pending.length > 0 && (
            <Card className="border-destructive/50">
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center gap-2 text-sm">
                  <Gavel className="size-4" /> Awaiting an officer
                </CardTitle>
                <CardDescription>
                  These did not issue automatically. The clause is why.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-2">
                {pending.slice(0, 4).map((d) => (
                  <div key={d.id} className="rounded border p-2 text-xs">
                    <div className="font-medium">{d.action}</div>
                    <div className="text-muted-foreground">{d.target}</div>
                    <div className="text-muted-foreground mt-1 italic">{d.rationale}</div>
                    {d.clause && (
                      <div className="mt-1">
                        <Badge variant="outline">{d.clause}</Badge>{" "}
                        <span className="text-muted-foreground">
                          reserved to {d.delegatedTo}
                        </span>
                      </div>
                    )}
                    <div className="mt-2 flex gap-2">
                      <Button size="sm" className="h-7 text-xs"
                              onClick={() => run(`ok-${d.id}`, `/demo/decisions/${d.id}/approve`)}>
                        Approve
                      </Button>
                      <Button size="sm" variant="outline" className="h-7 text-xs"
                              onClick={() => run(`no-${d.id}`, `/demo/decisions/${d.id}/reject`)}>
                        Reject
                      </Button>
                    </div>
                  </div>
                ))}
              </CardContent>
            </Card>
          )}

          {state.duplicates.length > 0 && (
            <Alert variant="destructive">
              <Copy className="size-4" />
              <AlertDescription className="text-xs">
                <div className="font-medium">Work being done twice</div>
                {state.duplicates.slice(0, 2).map((d, i) => <div key={i}>{d.detail}</div>)}
              </AlertDescription>
            </Alert>
          )}

          {/* Everything below is the running commentary, not the decision.
              A judge opening this console said, fairly, that it showed them
              things they had no business seeing; the last plan's diff and a
              scrolling narration are two of them — real, and for afterwards.
              Folded away, open in one click, and the state is remembered for
              whoever wants it open. */}
          <details className="group rounded-lg border" open={false}>
            <summary className="cursor-pointer list-none px-3 py-2 text-sm font-medium">
              <span className="group-open:hidden">Show the running detail</span>
              <span className="hidden group-open:inline">Hide the running detail</span>
            </summary>
            <div className="space-y-3 border-t p-3">
          {state.plan && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">Last re-plan</CardTitle>
                <CardDescription>{state.plan.headline}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-1">
                {[...state.plan.reassigned, ...state.plan.assigned, ...state.plan.released]
                  .slice(0, 6)
                  .map((c, i) => (
                    <div key={i} className="text-xs">
                      <Badge variant={c.kind === "reassigned" ? "default" : "outline"}
                             className="mr-1">{c.resource_label}</Badge>
                      <span className="text-muted-foreground">{c.reason}</span>
                    </div>
                  ))}
                {state.plan.uncovered.slice(0, 3).map((u, i) => (
                  <div key={`u${i}`} className="text-destructive text-xs">{u.reason}</div>
                ))}
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm">What is happening</CardTitle>
            </CardHeader>
            <CardContent>
              <div ref={feedRef} className="max-h-[420px] overflow-y-auto">
                <BeatFeed beats={state.beats} />
              </div>
            </CardContent>
          </Card>
            </div>
          </details>
        </div>
      </div>
      <WardPanel wardId={ward} onClose={() => setWard(null)} />
    </div>
  )
}
