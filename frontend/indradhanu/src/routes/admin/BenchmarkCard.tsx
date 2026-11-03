import { useState } from "react"
import { FlaskConical } from "lucide-react"
import {
  Card, CardContent, CardDescription, CardHeader, CardTitle,
} from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { benchmark as data } from "@/data/benchmark"

/** Does re-planning actually beat what a control room does today?
 *
 *  Until now the answer lived in `backend/scripts/benchmark_strategies.py` and
 *  in a table in the README, and nothing in the running system showed it. A
 *  judge asking "how much better" had to be told a number rather than shown
 *  one, which is the weakest possible form of the claim.
 *
 *  What is on screen is the harness's own output, committed as
 *  `src/data/benchmark.json`, with the conditions printed under it and every
 *  seed available. It is not live and it does not pretend to be: the simulation
 *  takes minutes to run and would say nothing about the demo world anyway. The
 *  honest framing is "here is the measurement, here is how to re-run it".
 *
 *  The cost row is not optional. Re-tasking a committed crew is paid for on the
 *  arrival tail, and a panel that showed only the coverage gain would be the
 *  same selective reporting this system exists to avoid.
 */

/** Two hues, validated for both themes against the CVD and contrast checks
 *  rather than picked by eye. Colour follows the arm, never the rank, so
 *  Indradhanu is the same blue in the row it wins and the row it loses. */
const SERIES = {
  baseline: { light: "#a16207", dark: "#c2740a" },
  ours: { light: "#0284c7", dark: "#2a97d4" },
}

type ArmKey = "nearest" | "oneshot" | "indradhanu"
type Stats = {
  unmet: number; unmet_pct: number
  assign_p50: number; assign_p90: number
  arrive_p50: number; arrive_p90: number
  switches: number; engine: string
}

const arms = data.arms as { key: ArmKey; label: string; note: string; stats: Stats }[]
const armOf = (k: ArmKey) => arms.find((a) => a.key === k)!

/** Each row is its own measure in its own unit, so each gets its own scale and
 *  is labelled with it. Two measures never share one axis. */
const ROWS: {
  label: string; unit: string; pick: (s: Stats) => number; lowerIsBetter: boolean
}[] = [
  { label: "Demands left uncovered", unit: "%", pick: (s) => s.unmet_pct, lowerIsBetter: true },
  { label: "Time to commit a unit, p90", unit: "min", pick: (s) => s.assign_p90, lowerIsBetter: true },
  { label: "Time to commit a unit, median", unit: "min", pick: (s) => s.assign_p50, lowerIsBetter: true },
  { label: "Time to arrival, p90", unit: "min", pick: (s) => s.arrive_p90, lowerIsBetter: true },
]

function pct(from: number, to: number) {
  if (!from) return "—"
  const d = ((to - from) / from) * 100
  return `${d > 0 ? "+" : ""}${d.toFixed(0)}%`
}

export default function BenchmarkCard() {
  const [table, setTable] = useState(false)
  const base = armOf("nearest").stats
  const ours = armOf("indradhanu").stats

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-sm">
            <FlaskConical className="size-4" />
            Against a nearest-first control room
          </CardTitle>
          <Button size="sm" variant="ghost" className="h-7 text-xs"
                  onClick={() => setTable((v) => !v)}>
            {table ? "Chart" : `Every seed (${data.perSeed.length})`}
          </Button>
        </div>
        <CardDescription>
          {data.conditions.seeds.length} seeds, one third of the fleet,{" "}
          {data.conditions.demands} demands over {data.conditions.simMinutes} sim
          minutes each. The same incident stream is replayed into every arm, so
          the only thing that differs is the dispatch policy.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* The headline and its price, together. One without the other is a
            sales figure rather than a measurement. */}
        <div className="grid gap-5 sm:grid-cols-2">
          <div className="rounded-md border p-3">
            <div className="text-2xl font-semibold tabular-nums">
              {(((base.unmet - ours.unmet) / base.unmet) * 100).toFixed(0)}% fewer
            </div>
            <p className="text-muted-foreground mt-1 text-xs">
              demands left uncovered — {ours.unmet} against {base.unmet} of{" "}
              {data.conditions.demands}. Worst-case time to commit a unit falls{" "}
              {pct(base.assign_p90, ours.assign_p90).replace("-", "")} as well.
            </p>
          </div>
          <div className="rounded-md border p-3">
            <div className="text-2xl font-semibold tabular-nums">
              {pct(base.arrive_p90, ours.arrive_p90)} arrival p90
            </div>
            <p className="text-muted-foreground mt-1 text-xs">
              what it costs. {ours.switches} committed units were re-tasked across
              the {data.conditions.seeds.length} runs, and every switch adds
              driving to somebody's journey. Shown because we chose the trade.
            </p>
          </div>
        </div>

        {table ? (
          <PerSeedTable />
        ) : (
          <>
            <Legend />
            <div className="space-y-3">
              {ROWS.map((r) => {
                const b = r.pick(base)
                const o = r.pick(ours)
                const max = Math.max(b, o) || 1
                return (
                  <div key={r.label}>
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-xs">{r.label}</span>
                      <span className="text-muted-foreground text-xs">
                        {r.lowerIsBetter ? "lower is better" : "higher is better"} · {r.unit}
                      </span>
                    </div>
                    <Bar value={b} max={max} series="baseline" unit={r.unit} />
                    <Bar value={o} max={max} series="ours" unit={r.unit} />
                  </div>
                )
              })}
            </div>
          </>
        )}

        <Separator />
        <p className="text-muted-foreground text-xs">
          Simulated arrivals and fixed service times against a{" "}
          {data.conditions.city}. A fair test of dispatch policy, and not
          evidence about Pune. Travel times are {data.conditions.travelTimes}.
          Re-run it yourself: <code className="text-[11px]">{data.harness}</code>
        </p>
      </CardContent>
    </Card>
  )
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-4">
      {(["nearest", "indradhanu"] as ArmKey[]).map((k) => {
        const arm = armOf(k)
        const s = k === "nearest" ? "baseline" : "ours"
        return (
          <span key={k} className="flex items-center gap-1.5 text-xs">
            <Swatch series={s as keyof typeof SERIES} />
            {arm.label}
          </span>
        )
      })}
    </div>
  )
}

function Swatch({ series }: { series: keyof typeof SERIES }) {
  return (
    <>
      <span
        aria-hidden
        className="size-2.5 rounded-[2px] dark:hidden"
        style={{ background: SERIES[series].light }}
      />
      <span
        aria-hidden
        className="hidden size-2.5 rounded-[2px] dark:inline-block"
        style={{ background: SERIES[series].dark }}
      />
    </>
  )
}

/** A thin bar with its value written on it. The number is the point; the bar is
 *  only there so two of them can be compared without reading. */
function Bar({
  value, max, series, unit,
}: { value: number; max: number; series: keyof typeof SERIES; unit: string }) {
  const width = Math.max(2, (value / max) * 100)
  return (
    <div className="mt-1 flex items-center gap-2">
      <div className="bg-muted/60 h-3 flex-1 rounded-[3px]">
        <div
          className="h-3 rounded-[3px] dark:hidden"
          style={{ width: `${width}%`, background: SERIES[series].light }}
        />
        <div
          className="hidden h-3 rounded-[3px] dark:block"
          style={{ width: `${width}%`, background: SERIES[series].dark }}
        />
      </div>
      <span className="w-16 shrink-0 text-right text-xs tabular-nums">
        {value}
        <span className="text-muted-foreground"> {unit}</span>
      </span>
    </div>
  )
}

/** The table view. Required so nothing on this card is carried by colour
 *  alone, and useful in its own right: a per-seed spread is what tells you
 *  whether the headline is a result or one lucky run. */
function PerSeedTable() {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-muted-foreground">
          <tr className="border-b">
            <th className="py-1 text-left font-medium">Seed</th>
            <th className="py-1 text-right font-medium">Demands</th>
            <th className="py-1 text-right font-medium">Uncovered, nearest</th>
            <th className="py-1 text-right font-medium">Uncovered, ours</th>
            <th className="py-1 text-right font-medium">Arrival p90, nearest</th>
            <th className="py-1 text-right font-medium">Arrival p90, ours</th>
            <th className="py-1 text-right font-medium">Re-tasked</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {data.perSeed.map((r) => (
            <tr key={r.seed} className="border-b last:border-0">
              <td className="py-1">{r.seed}</td>
              <td className="py-1 text-right">{r.demands}</td>
              <td className="py-1 text-right">{r.arms.nearest.unmet}</td>
              <td className="py-1 text-right">{r.arms.indradhanu.unmet}</td>
              <td className="py-1 text-right">{r.arms.nearest.arriveP90}</td>
              <td className="py-1 text-right">{r.arms.indradhanu.arriveP90}</td>
              <td className="py-1 text-right">{r.arms.indradhanu.switches}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-muted-foreground mt-2 text-xs">
        The middle arm — the same solver run once and never revisited — left{" "}
        {armOf("oneshot").stats.unmet} uncovered, the same as nearest-first. The
        gain is not the optimiser. It is re-running it.
      </p>
    </div>
  )
}
