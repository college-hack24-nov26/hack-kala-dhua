import { useMemo, useState } from "react"
import { Hospital, Loader2, PackagePlus, TrendingUp, TriangleAlert, Truck } from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

/** What is likely to happen next, and how much of that is actually known.
 *
 *  Every number on this screen is a posterior mean from a Gamma-Poisson model
 *  fitted to this system's own incident history. The bar beside each one is not
 *  the rate: it is how much of the rate came from that ward's own record rather
 *  than from the city-wide prior. A screen that showed the prediction without
 *  that bar would be presenting a prior as a forecast, which on an empty
 *  database is exactly what it would be.
 */

const PRESSURE: Record<string, { label: string; tone: string }> = {
  full: { label: "Full", tone: "bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/30" },
  saturating: {
    label: "Fills within the hour",
    tone: "bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/30",
  },
  tightening: {
    label: "Tightening",
    tone: "bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30",
  },
  steady: {
    label: "Steady",
    tone: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30",
  },
}

/** How much of this answer is this ward's own history. */
function EvidenceBar({ value }: { value: number }) {
  return (
    <div className="flex items-center gap-1.5" title="Share of the estimate that comes from this ward's own record rather than the city-wide prior">
      <div className="bg-muted h-1 w-14 overflow-hidden rounded-full">
        <div
          className={`h-full rounded-full ${
            value > 0.7 ? "bg-emerald-500" : value > 0.15 ? "bg-sky-500" : "bg-slate-400"
          }`}
          style={{ width: `${Math.max(3, value * 100)}%` }}
        />
      </div>
      <span className="text-muted-foreground w-8 text-right text-[11px] tabular-nums">
        {(value * 100).toFixed(0)}%
      </span>
    </div>
  )
}

export default function Forecast() {
  const { state, busy, run } = useDemo()
  const f = state.forecast
  const [tab, setTab] = useState<"facilities" | "wards" | "demand">("facilities")
  /** The same test the sidebar badge counts on, so the number an officer was
   *  sent here by is the number they find. */
  const short = (state.forecast?.demand ?? []).filter((d) => d.shortfall > 0.5).length

  const topWards = useMemo(() => {
    if (!f) return []
    // One row per ward: its most likely next incident, rather than every
    // category for every ward, which is forty rows of noise.
    const best = new Map<string, (typeof f.recurrence)[number]>()
    for (const r of f.recurrence) {
      const held = best.get(r.wardId)
      if (!held || r.expected > held.expected) best.set(r.wardId, r)
    }
    return [...best.values()].sort((a, b) => b.expected - a.expected).slice(0, 14)
  }, [f])

  if (!f || (!f.facilities.length && !f.recurrence.length)) {
    return (
      <div className="p-5 md:p-8 xl:px-10">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <TrendingUp className="size-4" /> Nothing to forecast from yet
            </CardTitle>
            <CardDescription>
              {f?.error
                ? `The forecast could not be built: ${f.error}`
                : "Rates are learned from incidents this system has actually seen. Start live ingest and the first estimates appear within a minute, marked as resting almost entirely on the city-wide prior until the wards separate."}
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    )
  }

  const saturating = f.facilities.filter(
    (x) => x.pressure === "saturating" || x.pressure === "full"
  )

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">
            Next {f.horizonHours} hours
          </CardTitle>
          <CardDescription>{f.confidenceNote}</CardDescription>
        </CardHeader>
        <CardContent className="text-muted-foreground text-xs">
          Rates are a Gamma-Poisson posterior: each ward starts on the city-wide
          rate for a category and moves onto its own record as that record grows.
          The bar beside each row is how far along that shift it is. Nothing here
          dispatches anything; a forecast prepositions and warns, and a unit
          still moves because something was reported.
        </CardContent>
      </Card>

      <Accuracy bt={f.backtest} />

      {saturating.length > 0 && (
        <Card className="border-destructive/50">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <TriangleAlert className="size-4" />
              {saturating.length} facilit{saturating.length === 1 ? "y" : "ies"} will
              not take who is coming
            </CardTitle>
            <CardDescription>
              The citizen agent already stops routing people to these. They are
              here so somebody can open capacity before it matters.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <div className="flex gap-1">
        {(
          [
            ["facilities", "Where people will arrive"],
            ["wards", "Where it will happen again"],
            ["demand", "What will be needed"],
          ] as const
        ).map(([k, label]) => (
          <Button
            key={k}
            size="sm"
            variant={tab === k ? "secondary" : "ghost"}
            className="h-8 text-xs"
            onClick={() => setTab(k)}
          >
            {label}
          </Button>
        ))}
      </div>

      {tab === "facilities" && (
        <div className="grid gap-2 xl:grid-cols-2">
          {f.facilities.map((x) => {
            const p = PRESSURE[x.pressure] ?? PRESSURE.steady
            return (
              <Card key={x.id}>
                <CardHeader className="pb-3">
                  <div className="flex items-start justify-between gap-2">
                    <CardTitle className="flex items-center gap-1.5 text-sm">
                      <Hospital className="size-3.5 shrink-0" />
                      {x.name}
                    </CardTitle>
                    <Badge variant="outline" className={`shrink-0 ${p.tone}`}>
                      {p.label}
                    </Badge>
                  </div>
                  <CardDescription>
                    {x.kind} ·{" "}
                    {x.capacity !== null
                      ? `${x.spare} of ${x.capacity} free`
                      : "no capacity recorded"}
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-1.5">
                  <p className="text-xs">{x.explanation}</p>
                  {x.fromWards.length > 0 && (
                    <p className="text-muted-foreground text-xs">
                      Mostly from{" "}
                      {x.fromWards.map((w) => w.ward).join(", ")}.
                    </p>
                  )}
                  {x.hoursToFull !== null && x.hoursToFull <= 3 && (
                    <div className="bg-muted h-1.5 overflow-hidden rounded-full">
                      <div
                        className={`h-full rounded-full ${
                          x.hoursToFull <= 1 ? "bg-red-500" : "bg-amber-500"
                        }`}
                        style={{
                          width: `${Math.max(4, 100 - (x.hoursToFull / 3) * 100)}%`,
                        }}
                      />
                    </div>
                  )}
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}

      {tab === "wards" && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-sm">Most likely next, by ward</CardTitle>
            <CardDescription>
              The bar is how much of the estimate is this ward's own history
              rather than the city prior.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-1">
            {topWards.map((r) => (
              <div
                key={`${r.wardId}-${r.category}`}
                className="hover:bg-muted/40 rounded-md border p-2"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">{r.wardName}</span>
                  <Badge variant="secondary" className="font-normal">
                    {r.category.replace(/_/g, " ")}
                  </Badge>
                  <span className="text-muted-foreground text-xs tabular-nums">
                    {(r.pAtLeastOne * 100).toFixed(0)}% chance of at least one
                  </span>
                  {r.hazardMultiplier > 1.05 && (
                    <Badge variant="outline" className="font-normal">
                      {r.hazardMultiplier.toFixed(1)}× for current hazard
                    </Badge>
                  )}
                  <div className="ml-auto">
                    <EvidenceBar value={r.evidence} />
                  </div>
                </div>
                <p className="text-muted-foreground mt-1 text-xs">{r.explanation}</p>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {tab === "demand" && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Truck className="size-4" /> Capability wanted over the horizon
            </CardTitle>
            <CardDescription>
              Expected incidents multiplied by what each category needs. Compared
              against what is free right now, not against the whole fleet.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-1">
            {/* A projected shortage with nothing to press was a weather report.
                `POST /forecast/preposition` has existed the whole time and was
                reachable only by somebody who knew the URL, so the forecast half
                of this system could see a shortage coming and do nothing about
                it. It moves no vehicle: it reads the projection and writes
                proposals into the decision gate, each one carrying the clause
                that delegates it — which is why the button says "propose" and
                sends the officer to the gate rather than reporting a result. */}
            {short > 0 && (
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2 rounded-md border border-amber-500/40 p-2">
                <p className="text-xs">
                  {short} capabilit{short === 1 ? "y is" : "ies are"} projected
                  short over the next {f.horizonHours} hours.
                </p>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 shrink-0 text-xs"
                  disabled={busy !== null}
                  onClick={() => void run("preposition", "/forecast/preposition")}
                >
                  {busy === "preposition" ? (
                    <Loader2 className="size-3 animate-spin" />
                  ) : (
                    <PackagePlus className="size-3" />
                  )}
                  Propose prepositioning
                </Button>
              </div>
            )}
            {f.demand.map((d) => (
              <div
                key={d.capability}
                className="flex flex-wrap items-center gap-2 rounded border p-2 text-xs"
              >
                <span className="min-w-40 font-medium">
                  {d.capability.replace(/_/g, " ")}
                </span>
                <span className="text-muted-foreground tabular-nums">
                  {d.expectedUnits.toFixed(1)} wanted
                </span>
                <span className="text-muted-foreground tabular-nums">
                  {d.availableNow} free, {d.committedNow} committed
                </span>
                {d.shortfall > 0.5 && (
                  <Badge variant="destructive" className="ml-auto">
                    short by {d.shortfall.toFixed(1)}
                  </Badge>
                )}
              </div>
            ))}
            {f.demand.length === 0 && (
              <p className="text-muted-foreground text-xs">
                No demand projected yet.
              </p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  )
}


/** How good the forecast has been on this run's own history, against two
 *  simple baselines on exactly the same windows. */
function Accuracy({ bt }: { bt?: NonNullable<ReturnType<typeof useDemo>["state"]["forecast"]>["backtest"] }) {
  if (!bt) return null
  if (!bt.available) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">How accurate it has been</CardTitle>
          <CardDescription>{bt.reason}</CardDescription>
        </CardHeader>
      </Card>
    )
  }
  const rows: [string, string, typeof bt.model][] = [
    ["Forecast", "Gamma-Poisson, as on this screen", bt.model],
    ["Persistence", "each ward repeats its last window", bt.persistence],
    ["Uniform", "the city rate spread evenly", bt.uniform],
  ]
  const best = (k: "mae" | "brier") => Math.min(...rows.map((r) => r[2]?.[k] ?? Infinity))
  const bestTop = Math.max(...rows.map((r) => r[2]?.top5 ?? -1))
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">How accurate it has been</CardTitle>
        <CardDescription>
          Replayed {bt.origins} times on this run&rsquo;s own history: rates rebuilt from what was known at each
          point, then compared with the {bt.incidents} incidents that arrived in the next {bt.horizonHours} h
          across {bt.wards} wards. {bt.verdict}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-muted-foreground">
              <th className="py-1.5 font-medium">Method</th>
              <th className="py-1.5 text-right font-medium" title="Mean absolute error of the count per ward and window">Count error</th>
              <th className="py-1.5 text-right font-medium" title="Brier score of 'at least one incident' (0 is perfect)">Brier</th>
              <th className="py-1.5 text-right font-medium" title="Share of incidents that landed in the five wards ranked highest">Top-5 wards caught</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([name, note, sc]) => (
              <tr key={name} className="border-t">
                <td className="py-2">
                  <div className="font-medium">{name}</div>
                  <div className="text-xs text-muted-foreground">{note}</div>
                </td>
                <td className={`py-2 text-right tabular-nums ${sc?.mae === best("mae") ? "font-semibold text-emerald-600" : ""}`}>{sc?.mae.toFixed(3) ?? "—"}</td>
                <td className={`py-2 text-right tabular-nums ${sc?.brier === best("brier") ? "font-semibold text-emerald-600" : ""}`}>{sc?.brier.toFixed(3) ?? "—"}</td>
                <td className={`py-2 text-right tabular-nums ${sc?.top5 != null && sc.top5 === bestTop ? "font-semibold text-emerald-600" : ""}`}>
                  {sc?.top5 != null ? `${Math.round(sc.top5 * 100)}%` : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  )
}
