import { useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertOctagon, Building2, Check, ChevronRight, FlaskConical, HandHelping, Loader2, RefreshCw, School, ShieldAlert,
  Truck, Users, X,
} from "lucide-react"
import { request } from "@/api/httpClient"
import { useRegion } from "@/lib/region"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { SurgeOperations, type Ops } from "./SurgeOperations"

/** Surge operations: what happens when units and shelters run out.
 *
 *  The ladder climbs one rung at a time, and only after the rung below has been
 *  tried and the pressure persists: triage and rationing first, then mutual aid
 *  from NGOs and neighbouring agencies, then schools and halls opened as
 *  shelters, and only then a state/national declaration, which an officer must
 *  approve. Every step shows why it was taken; aid that is declined is shown as
 *  declined, and the next agency is asked.
 */

type Overview = {
  available: boolean; reason?: string; region: string; level: number; name: string; levels: string[]
  reasons: string[]; since: string; updatedAt: string; drillActive: boolean
  metrics: {
    fleet?: { available: number; busy: number; offline: number; utilisation: number }
    shelters?: { capacity: number; occupancy: number; occupancy_ratio: number; full_sites: number; open_sites: number }
    unmet?: { count: number; by_capability: Record<string, number>; life_safety: number
      items?: { ward_id?: string; incident_id?: string; need?: string; capability?: string; reason?: string }[] }
    supplies_low_sites?: number
  }
  thresholds: { strainedUtilisation: number; strainedShelter: number; surgeShelter: number }
  aid: { id: number; label: string; agency: string; capability_id: string; quantity: number; requested_at: string
    due_at: string; outcome: string | null; units: string[] }[]
  surgeShelters: { id: string; name: string; capacity: number; occupancy: number; status: string; opened_at: string }[]
  offers: { id: string; agency: string; label: string; kind: string; quantity: number; response_minutes: number
    accept_p: number; min_level: number }[]
  log: { id: number; at: string; kind: string; actor: string; payload: Record<string, unknown> }[]
  operations?: Ops
}

const LADDER = [
  { name: "Normal", what: "Routine planning", icon: Check },
  { name: "Strained", what: "Triage: life-safety first, vulnerable wards next · rationing · stock redistributed", icon: AlertOctagon },
  { name: "Mutual aid", what: "Red Cross, volunteers, neighbouring corporation, SDRF asked; aid joins the fleet", icon: HandHelping },
  { name: "Surge shelters", what: "Schools, hotel rooms, host families · convoys by zone on fixed routes · shelter in place", icon: School },
  { name: "Declaration", what: "National: NDRF, Army, helicopters (officer approves)", icon: ShieldAlert },
]
const pretty = (s?: string | null) => (s ?? "").replace(/_/g, " ")
const time = (iso?: string | null) => (iso ? new Date(iso).toLocaleTimeString() : "—")

export default function SurgePage() {
  const [pick] = useRegion()
  const region = pick === "ncr" ? "ncr" : "pune"
  const qc = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)
  const q = useQuery({
    queryKey: ["surge", region], refetchInterval: 4000,
    queryFn: () => request<Overview>("/surge", { toast: false, query: { region } }),
  })
  const act = async (key: string, path: string, body?: unknown, label?: string) => {
    setBusy(key)
    try {
      await request(path, { method: "POST", body, query: body ? undefined : { region }, toast: { loading: label, success: "Done" } })
      await qc.invalidateQueries({ queryKey: ["surge", region] })
    } finally { setBusy(null) }
  }

  const d = q.data
  if (q.isLoading) return <div className="p-6 text-sm text-muted-foreground"><Loader2 className="mr-2 inline size-4 animate-spin" />Loading surge state…</div>
  if (!d || !d.available) return <div className="p-6 text-sm text-muted-foreground">Surge operations are not set up yet: {d?.reason ?? "run migration 034"}.</div>
  const m = d.metrics
  const util = m.fleet?.utilisation ?? 0
  const occ = m.shelters?.occupancy_ratio ?? 0

  return (
    <div className="flex flex-col gap-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-sm font-medium">{region === "pune" ? "Pune" : "Ghaziabad (NCR)"}</p>
          <p className="text-sm text-muted-foreground">When units and shelters run out: triage, mutual aid, surge shelters, declaration. Each step only after the one before it.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {d.drillActive ? (
            <Button size="sm" variant="outline" className="gap-1.5" disabled={!!busy}
                    onClick={() => act("drill", "/surge/drill/end", undefined, "Ending drill")}>
              <X className="size-4" /> End scarcity drill
            </Button>
          ) : (
            <Button size="sm" className="gap-1.5" disabled={!!busy}
                    onClick={() => act("drill", "/surge/drill/start", { region, fleetKeep: 0.35, shelterScale: 0.15 }, "Starting scarcity drill")}>
              <FlaskConical className="size-4" /> Start scarcity drill
            </Button>
          )}
          <Button size="sm" variant="outline" className="gap-1.5" disabled={!!busy}
                  onClick={() => act("eval", "/surge/evaluate", undefined, "Evaluating")}>
            <RefreshCw className="size-4" /> Evaluate now
          </Button>
        </div>
      </div>

      {/* the ladder */}
      <Card>
        <CardContent className="grid gap-2 p-4 md:grid-cols-5">
          {LADDER.map((r, i) => {
            const Icon = r.icon
            const here = i === d.level
            const passed = i < d.level
            return (
              <div key={r.name} className={`relative rounded-lg border p-3 ${here ? "border-primary bg-primary/10 ring-1 ring-primary" : passed ? "bg-muted/60" : "opacity-70"}`}>
                <div className="flex items-center gap-2 text-sm font-medium">
                  <Icon className={`size-4 ${here ? "text-primary" : ""}`} /> {i}. {r.name}
                  {here && <Badge className="ml-auto">now</Badge>}
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{r.what}</p>
                {i < 4 && <ChevronRight className="absolute -right-3 top-1/2 hidden size-4 -translate-y-1/2 text-muted-foreground md:block" />}
              </div>
            )
          })}
        </CardContent>
        <div className="border-t px-4 py-3 text-sm">
          <span className="font-medium">Why {LADDER[d.level].name.toLowerCase()}:</span>{" "}
          {d.reasons.length ? d.reasons.join(" · ") : "no pressure on any line"}
          <span className="ml-2 text-xs text-muted-foreground">since {time(d.since)} · checked {time(d.updatedAt)}</span>
          {d.level === 3 && (
            <Button size="sm" variant="destructive" className="ml-3 h-7 gap-1.5" disabled={!!busy}
                    onClick={() => act("declare", "/surge/declare", undefined, "Requesting NDRF and Army")}>
              <ShieldAlert className="size-3.5" /> Approve declaration
            </Button>
          )}
        </div>
      </Card>

      {/* load */}
      <div className="grid gap-3 md:grid-cols-4">
        <Gauge icon={Truck} label="Fleet busy" value={util} lines={[d.thresholds.strainedUtilisation]}
               sub={`${m.fleet?.busy ?? 0} busy · ${m.fleet?.available ?? 0} free · ${m.fleet?.offline ?? 0} out`} />
        <Gauge icon={Building2} label="Shelters full" value={occ} lines={[d.thresholds.strainedShelter, d.thresholds.surgeShelter]}
               sub={`${(m.shelters?.occupancy ?? 0).toLocaleString()} / ${(m.shelters?.capacity ?? 0).toLocaleString()} · ${m.shelters?.full_sites ?? 0} full`} />
        <Card>
          <CardHeader className="pb-1"><CardDescription className="flex items-center gap-1.5"><Users className="size-4" /> Needs with no unit</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{m.unmet?.count ?? 0}<span className="ml-2 text-sm font-normal text-red-600">{m.unmet?.life_safety ? `${m.unmet.life_safety} life-safety` : ""}</span></CardTitle></CardHeader>
          <CardContent className="flex flex-wrap gap-1 pb-4">
            {Object.entries(m.unmet?.by_capability ?? {}).map(([k, v]) => <Badge key={k} variant="destructive">{pretty(k)} {v}</Badge>)}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-1"><CardDescription>Relief sites below 25% stock</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{m.supplies_low_sites ?? 0}</CardTitle></CardHeader>
          <CardContent className="pb-4 text-xs text-muted-foreground">Rationed ×0.7 and restocked from low-need sites while strained.</CardContent>
        </Card>
      </div>

      <SurgeOperations ops={d.operations} level={d.level} region={region} />

      <div className="grid gap-5 xl:grid-cols-2">
        <Card>
          <CardHeader className="flex-row items-center justify-between pb-2">
            <div><CardTitle className="text-base">Mutual aid</CardTitle><CardDescription>Requests the ladder made and what came of them</CardDescription></div>
            <Button size="sm" variant="outline" disabled={!!busy} onClick={() => act("aid", "/surge/aid", undefined, "Requesting aid")}>Ask now</Button>
          </CardHeader>
          <CardContent className="p-0">
            <ScrollArea className="h-[260px]">
              <Table>
                <TableHeader><TableRow><TableHead>From</TableHead><TableHead>For</TableHead><TableHead>Asked</TableHead><TableHead>Due</TableHead><TableHead>Outcome</TableHead></TableRow></TableHeader>
                <TableBody>
                  {d.aid.map((a) => (
                    <TableRow key={a.id}>
                      <TableCell><div className="font-medium">{a.label}</div><div className="text-xs text-muted-foreground">{a.agency}</div></TableCell>
                      <TableCell>{a.quantity} × {pretty(a.capability_id)}</TableCell>
                      <TableCell className="text-xs">{time(a.requested_at)}</TableCell>
                      <TableCell className="text-xs">{time(a.due_at)}</TableCell>
                      <TableCell>
                        {a.outcome === "arrived" ? <Badge className="bg-emerald-600 text-white">{a.units.length} arrived</Badge>
                          : a.outcome === "declined" ? <Badge variant="destructive">declined</Badge>
                            : <Badge variant="outline">waiting</Badge>}
                      </TableCell>
                    </TableRow>
                  ))}
                  {!d.aid.length && <TableRow><TableCell colSpan={5} className="text-sm text-muted-foreground">Nothing asked yet.</TableCell></TableRow>}
                </TableBody>
              </Table>
            </ScrollArea>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex-row items-center justify-between pb-2">
            <div><CardTitle className="text-base">Surge shelters</CardTitle><CardDescription>Schools and halls opened by the ladder</CardDescription></div>
            <Button size="sm" variant="outline" disabled={!!busy} onClick={() => act("open", "/surge/shelters/open", undefined, "Opening shelters")}>Open 2 now</Button>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {d.surgeShelters.map((s) => (
              <div key={s.id} className="flex items-center justify-between rounded border p-2 text-sm">
                <div><div className="font-medium">{s.name}</div><div className="text-xs text-muted-foreground">opened {time(s.opened_at)}</div></div>
                <div className="text-right"><div className="tabular-nums">{s.occupancy} / {s.capacity}</div><Badge variant="outline">{s.status}</Badge></div>
              </div>
            ))}
            {!d.surgeShelters.length && <div className="text-sm text-muted-foreground">None open. They open at rung 3, in wards with no open flooding, nearest the fullest shelters.</div>}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-[1.2fr_1fr]">
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Surge log</CardTitle><CardDescription>Every move, with its reason</CardDescription></CardHeader>
          <CardContent>
            <ScrollArea className="h-[300px] pr-2">
              <ol className="relative ml-2 border-l pl-4">
                {d.log.map((e) => (
                  <li key={e.id} className="mb-3">
                    <span className="absolute -left-[5px] mt-1.5 size-2.5 rounded-full bg-primary/70" />
                    <div className="text-xs text-muted-foreground">{time(e.at)} · {pretty(e.kind.replace("surge.", ""))} · {e.actor}</div>
                    <div className="text-sm">{String(e.payload.reason ?? e.payload.label ?? e.payload.name ?? "")}</div>
                  </li>
                ))}
                {!d.log.length && <li className="text-sm text-muted-foreground">Quiet so far.</li>}
              </ol>
            </ScrollArea>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2"><CardTitle className="text-base">Who can be asked</CardTitle><CardDescription>Aid offers by rung (planning figures)</CardDescription></CardHeader>
          <CardContent className="p-0">
            <ScrollArea className="h-[300px]">
              <Table>
                <TableHeader><TableRow><TableHead>Agency</TableHead><TableHead>Units</TableHead><TableHead>Rung</TableHead><TableHead>Takes</TableHead></TableRow></TableHeader>
                <TableBody>
                  {d.offers.map((o) => (
                    <TableRow key={o.id}>
                      <TableCell><div className="text-sm">{o.label}</div><div className="text-xs text-muted-foreground">{o.agency}</div></TableCell>
                      <TableCell className="text-sm">{o.quantity} × {pretty(o.kind)}</TableCell>
                      <TableCell>{o.min_level}</TableCell>
                      <TableCell className="text-xs">{o.response_minutes} min · {Math.round(o.accept_p * 100)}%</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </ScrollArea>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function Gauge({ icon: Icon, label, value, lines, sub }: { icon: typeof Truck; label: string; value: number; lines: number[]; sub: string }) {
  const tone = value >= (lines[lines.length - 1] ?? 1) ? "bg-red-500" : value >= lines[0] ? "bg-amber-500" : "bg-emerald-500"
  return (
    <Card>
      <CardHeader className="pb-1">
        <CardDescription className="flex items-center gap-1.5"><Icon className="size-4" /> {label}</CardDescription>
        <CardTitle className="text-2xl tabular-nums">{Math.round(value * 100)}%</CardTitle>
      </CardHeader>
      <CardContent className="pb-4">
        <div className="relative h-2 rounded bg-muted">
          <div className={`h-2 rounded ${tone}`} style={{ width: `${Math.min(100, value * 100)}%` }} />
          {lines.map((l) => <div key={l} className="absolute -top-1 h-4 w-px bg-foreground/60" style={{ left: `${l * 100}%` }} title={`${Math.round(l * 100)}%`} />)}
        </div>
        <div className="mt-1.5 text-xs text-muted-foreground">{sub}</div>
      </CardContent>
    </Card>
  )
}
