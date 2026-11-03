import { useQuery } from "@tanstack/react-query"
import { Activity, Brain, Clock, Loader2, Route, ShieldCheck } from "lucide-react"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { StatCard } from "@/components/common/StatCard"

/** The models, in the open: how each did on held-out storms against the simpler
 *  baselines, what they changed for crews on the road and in the scenario
 *  replays, and what they are doing live right now. Every number here comes
 *  from files and tables the API serves; nothing is typed into this page. */

type M = Record<string, number>
type Models = {
  champions: { passability?: string; eta?: string }
  meta: { passability?: Record<string, any>; eta?: Record<string, any> }
  passability?: { test_rows: number; test_positive_share: number; threshold: number
    results: { model: M; b1_logistic: M; b0_current_status: M
      anticipation?: { open_now_rows: number; will_close: number; will_close_model: M; will_close_b0: M } } }
  routes?: { trips: number; to_flooded_places: number; current_status: M; predicted: M; oracle: M }
  eta?: { train: number; tune: number; test: number; results: { model: M; distance_rule: M; traffic_eta: M }
    shap_mean_abs_p50?: [string, number][] }
  scenarios?: Record<string, { title: string; story: string; data: Record<string, any>
    policies: Record<string, Record<string, any>> }>
  live: { predictions?: number; predictions_24h?: number; outcomes?: number; outcomes_24h?: number; examples?: number
    registry?: { version: string; task: string; status: string; promoted_at: string | null }[]; error?: string }
  risk: { model: string; scored: number; avoided: number; atRisk: number; computedAt: number; notes: string[] } | null
}

const pct = (v?: number, d = 1) => (v == null ? "—" : `${(v * 100).toFixed(d)}%`)
const n2 = (v?: number, d = 3) => (v == null ? "—" : v.toFixed(d))
const POLICY: Record<string, string> = {
  current_status: "Current status only (baseline)", predicted: "With the model", oracle: "Perfect knowledge (scale)",
  naive_trust: "Believe every report (ablation)",
}

export default function ModelsPage() {
  const q = useQuery({ queryKey: ["models"], refetchInterval: 30_000, queryFn: () => request<Models>("/nav/models", { toast: false }) })
  const d = q.data
  if (q.isLoading) return <div className="p-6 text-sm text-muted-foreground"><Loader2 className="mr-2 inline size-4 animate-spin" />Loading model results…</div>
  if (!d) return <div className="p-6 text-sm text-muted-foreground">Model results are not available.</div>
  const P = d.passability?.results
  const pm = d.meta.passability
  return (
    <div className="flex flex-col gap-8 p-5 md:p-8 xl:px-10">
      <div>
        <p className="text-sm text-muted-foreground">
          Road passability at arrival time and response ETA. Trained on simulated storms 2015–21 (weather and river
          drivers from ERA5 and GloFAS), tuned and calibrated on 2022, tested once on 2023–26. Every result below is on
          held-out storms against a simpler baseline on the same inputs.
        </p>
      </div>

      {/* live */}
      <div className="grid gap-3 md:grid-cols-4">
        <Stat icon={Brain} label="Road model in use" value={d.champions.passability ?? "none"} sub={pm ? `threshold ${n2(pm.threshold, 3)} (${pm.threshold_rule})` : ""} />
        <Stat icon={Activity} label="Roads scored live" value={d.risk ? String(d.risk.scored) : "—"}
              sub={d.risk ? `${d.risk.atRisk} at risk · ${d.risk.avoided} avoided by routing · ${new Date(d.risk.computedAt * 1000).toLocaleTimeString()}` : "waiting for the first scoring (every 5 min)"} />
        <Stat icon={Route} label="Predictions logged" value={String(d.live.predictions ?? 0)} sub={`${d.live.predictions_24h ?? 0} in 24 h · used by routes and the re-learning loop`} />
        <Stat icon={ShieldCheck} label="Outcomes collected" value={String(d.live.outcomes ?? 0)} sub={`${d.live.outcomes_24h ?? 0} in 24 h · roads driven, incidents confirmed or cleared`} />
      </div>

      {/* passability vs baselines */}
      {P && (
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Will this road be usable when the unit gets there?</CardTitle>
            <CardDescription>{d.passability!.test_rows.toLocaleString()} test rows (road × decision time × unit class × 30/60/90 min), {pct(d.passability!.test_positive_share)} blocked. Labels from the hazard rules, never from the model.</CardDescription></CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader><TableRow><TableHead>On the same held-out rows</TableHead><TableHead>ROC AUC</TableHead><TableHead>PR AUC</TableHead><TableHead>Closures caught</TableHead><TableHead>Closures missed</TableHead><TableHead>False closures</TableHead><TableHead>Brier</TableHead><TableHead>Calibration error</TableHead></TableRow></TableHeader>
              <TableBody>
                {([["Passability model", P.model, true], ["Logistic regression (B1)", P.b1_logistic, false], ["Current status only (B0)", P.b0_current_status, false]] as const).map(([name, m, best]) => (
                  <TableRow key={name} className={best ? "bg-primary/5 font-medium" : ""}>
                    <TableCell>{name}</TableCell><TableCell>{n2(m.roc_auc)}</TableCell><TableCell>{n2(m.pr_auc)}</TableCell>
                    <TableCell>{pct(m.recall)}</TableCell><TableCell>{pct(m.missed_closure_rate)}</TableCell><TableCell>{pct(m.false_closure_rate)}</TableCell>
                    <TableCell>{n2(m.brier_natural, 4)}</TableCell><TableCell>{n2(m.ece_natural, 4)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {P.anticipation && (
              <div className="border-t p-4 text-sm">
                <span className="font-medium">Roads open now that close before the unit arrives</span> ({P.anticipation.will_close.toLocaleString()} of {P.anticipation.open_now_rows.toLocaleString()}):
                the model catches <b>{pct(P.anticipation.will_close_model.recall, 0)}</b>, current status catches <b>{pct(P.anticipation.will_close_b0.recall, 0)}</b>.
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* routes */}
      {d.routes && (
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">What it changes for crews on the road</CardTitle>
            <CardDescription>{d.routes.trips.toLocaleString()} test trips from real unit bases ({d.routes.to_flooded_places} to flooded places), each driven through the simulated truth with every planner on identical inputs.</CardDescription></CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader><TableRow><TableHead>Planner</TableHead><TableHead>Invalid routes</TableHead><TableHead>Replans per trip</TableHead><TableHead>Ended on foot</TableHead><TableHead>Arrival P50</TableHead><TableHead>Arrival P90</TableHead><TableHead>Delay vs perfect</TableHead></TableRow></TableHeader>
              <TableBody>
                {(["current_status", "predicted", "oracle"] as const).map((k) => {
                  const m = d.routes![k]
                  return (
                    <TableRow key={k} className={k === "predicted" ? "bg-primary/5 font-medium" : ""}>
                      <TableCell>{POLICY[k]}</TableCell><TableCell>{pct(m.invalid_route_rate)}</TableCell><TableCell>{n2(m.mean_replans, 2)}</TableCell>
                      <TableCell>{pct(m.ended_on_foot)}</TableCell><TableCell>{m.arrival_p50_min?.toFixed(1)} min</TableCell><TableCell>{m.arrival_p90_min?.toFixed(1)} min</TableCell>
                      <TableCell>{m.delay_vs_oracle_mean_min != null ? `${m.delay_vs_oracle_mean_min.toFixed(1)} min` : "—"}</TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      {/* eta */}
      {d.eta && (
        <Card>
          <CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-base"><Clock className="size-4" /> Response ETA (P50 to plan, P90 to promise)</CardTitle>
            <CardDescription>{d.eta.test.toLocaleString()} test trips · used live in dispatch, the field app and the Units page</CardDescription></CardHeader>
          <CardContent className="grid gap-5 p-0 md:grid-cols-[1.4fr_1fr]">
            <Table>
              <TableHeader><TableRow><TableHead>Estimate</TableHead><TableHead>Mean error</TableHead><TableHead>Within 5 min</TableHead><TableHead>P90 covers</TableHead></TableRow></TableHeader>
              <TableBody>
                <TableRow className="bg-primary/5 font-medium"><TableCell>ETA model</TableCell><TableCell>{d.eta.results.model.mae_min} min</TableCell><TableCell>{pct(d.eta.results.model.within_5_min)}</TableCell><TableCell>{pct(d.eta.results.model.p90_coverage)}</TableCell></TableRow>
                <TableRow><TableCell>Traffic ETA (knows traffic, not floods)</TableCell><TableCell>{d.eta.results.traffic_eta.mae_min} min</TableCell><TableCell>{pct(d.eta.results.traffic_eta.within_5_min)}</TableCell><TableCell>—</TableCell></TableRow>
                <TableRow><TableCell>Distance rule</TableCell><TableCell>{d.eta.results.distance_rule.mae_min} min</TableCell><TableCell>{pct(d.eta.results.distance_rule.within_5_min)}</TableCell><TableCell>—</TableCell></TableRow>
              </TableBody>
            </Table>
            <div className="p-4 text-xs">
              <div className="mb-2 font-medium">What drives the ETA (mean |SHAP|, minutes)</div>
              {(d.eta.shap_mean_abs_p50 ?? []).slice(0, 8).map(([f, v]) => (
                <div key={f} className="mb-1 flex items-center gap-2">
                  <span className="w-36 truncate">{f.replace(/_/g, " ")}</span>
                  <div className="h-2 flex-1 rounded bg-muted"><div className="h-2 rounded bg-primary" style={{ width: `${Math.min(100, (v / (d.eta!.shap_mean_abs_p50![0][1] || 1)) * 100)}%` }} /></div>
                  <span className="w-10 text-right tabular-nums">{v.toFixed(2)}</span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* scenarios */}
      {d.scenarios && (
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">End-to-end scenarios (replays of held-out storms)</CardTitle>
            <CardDescription>Same storm, evidence, fleet and shelters for every policy. Timestamps are labelled REPLAY: historical weather, simulated roads and people. Units never double-booked and shelters never overfilled in any run.</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-4">
            {Object.entries(d.scenarios).map(([key, s]) => (
              <div key={key} className="rounded-lg border">
                <div className="border-b p-3">
                  <div className="font-medium">{s.title}</div>
                  <div className="text-xs text-muted-foreground">{s.story}</div>
                  <div className="mt-1 text-xs text-muted-foreground">storm {s.data.storm_id} · {s.data.window_start_replay?.slice(0, 16)} → {s.data.window_end_replay?.slice(0, 16)} UTC · {s.data.hours} h</div>
                </div>
                <Table>
                  <TableHeader><TableRow><TableHead>Policy</TableHead><TableHead>Invalid routes</TableHead><TableHead>Response P50 / P90</TableHead><TableHead>Real reached / unreached</TableHead><TableHead>Crews to false reports</TableHead><TableHead>Sheltered</TableHead><TableHead>Surge</TableHead><TableHead>Checks</TableHead></TableRow></TableHeader>
                  <TableBody>
                    {Object.entries(s.policies).map(([p, v]) => (
                      <TableRow key={p} className={p === "predicted" ? "bg-primary/5" : ""}>
                        <TableCell className="text-sm">{POLICY[p] ?? p}</TableCell>
                        <TableCell>{v.invalid_routes} ({pct(v.invalid_route_rate, 0)})</TableCell>
                        <TableCell>{v.response_p50_min ?? "—"} / {v.response_p90_min ?? "—"} min</TableCell>
                        <TableCell>{v.real_incidents_reached} / {v.real_incidents_unreached_at_end}</TableCell>
                        <TableCell>{v.crews_sent_to_false_reports}</TableCell>
                        <TableCell>{v.people_sheltered}</TableCell>
                        <TableCell>{v.surge_max_level ? `level ${v.surge_max_level} · ${v.aid_units_arrived} aid units` : "—"}</TableCell>
                        <TableCell>{v.checks && Object.values(v.checks).every((x) => x === 0) ? <Badge className="bg-emerald-600 text-white">0 conflicts</Badge> : <Badge variant="destructive">check</Badge>}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {/* registry */}
      {!!d.live.registry?.length && (
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Model registry</CardTitle>
            <CardDescription>A challenger is promoted only if it beats the champion on every gate, on the frozen test and on recent real outcomes (python -m ml.relearn run)</CardDescription></CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader><TableRow><TableHead>Version</TableHead><TableHead>Task</TableHead><TableHead>Status</TableHead><TableHead>Promoted</TableHead></TableRow></TableHeader>
              <TableBody>
                {d.live.registry.map((r) => (
                  <TableRow key={r.version}><TableCell className="font-mono text-xs">{r.version}</TableCell><TableCell>{r.task}</TableCell>
                    <TableCell><Badge variant={r.status === "champion" ? "default" : "outline"}>{r.status}</Badge></TableCell>
                    <TableCell className="text-xs">{r.promoted_at ? new Date(r.promoted_at).toLocaleString() : "—"}</TableCell></TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function Stat({ icon, label, value, sub }: { icon: typeof Brain; label: string; value: string; sub: string }) {
  return <StatCard icon={icon} label={label} value={value} sub={sub} />
}
