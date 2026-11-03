import { useState } from "react"
import { useQueryClient } from "@tanstack/react-query"
import {
  Bed, Bus, ClipboardCheck, Home, Hotel, Landmark, Megaphone, Route, Send, ShieldCheck, Siren, TimerReset, Undo2,
} from "lucide-react"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Textarea } from "@/components/ui/textarea"

/** The rest of the surge plan: who leads at each level, evacuation zones and
 *  convoys on fixed routes, priority corridors, every shelter option, crew
 *  rest, the single public channel, and stepping down again. */

type Action = { id: number; kind: string; status: string; title: string; ward_id?: string | null
  detail: Record<string, any>; created_at: string; closed_at?: string | null }
export type Ops = {
  available: boolean; reason?: string
  authority?: { tier: string; who: string; does: string }[]
  shiftHours?: number; restHours?: number
  zones?: Record<"A" | "B" | "C", { id: string; name: string; why: string; risk: number; openFlood: number }[]>
  actions?: Record<string, Action[]>
  notices?: { id: number; kind: string; headline: string; body: string; ward_ids: string[]; issued_at: string
    superseded_at?: string | null; level: number }[]
  pools?: { id: string; kind: string; name: string; capacity: number; occupancy: number; status: string }[]
  shifts?: { resource_id: string; label: string; on_duty_since?: string | null; rest_until?: string | null
    shifts: number; status: string }[]
}

const time = (iso?: string | null) => (iso ? new Date(iso).toLocaleTimeString() : "—")
const ZONE = {
  A: { label: "A · evacuate now", cls: "border-red-500/60 bg-red-500/10" },
  B: { label: "B · prepare (after A)", cls: "border-amber-500/60 bg-amber-500/10" },
  C: { label: "C · shelter in place", cls: "border-sky-500/60 bg-sky-500/10" },
} as const

export function SurgeOperations({ ops, level, region }: { ops?: Ops; level: number; region: string }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState<string | null>(null)
  const [head, setHead] = useState("")
  const [body, setBody] = useState("")
  const go = async (key: string, path: string, opts: { body?: unknown; query?: Record<string, string | number> } = {}) => {
    setBusy(key)
    try {
      await request(path, { method: "POST", body: opts.body, query: opts.body ? undefined : { region, ...(opts.query ?? {}) },
        toast: { loading: "Working…", success: "Done" } })
      await qc.invalidateQueries({ queryKey: ["surge", region] })
    } finally { setBusy(null) }
  }
  if (!ops?.available) {
    return <Card><CardContent className="p-4 text-sm text-muted-foreground">Evacuation, convoys, corridors, shelter options, crew rest and the public channel need migration 035 ({ops?.reason ?? "not applied"}).</CardContent></Card>
  }
  const a = ops.actions ?? {}
  const active = (k: string) => (a[k] ?? []).filter((x) => x.status === "active")
  const convoys = [...(a.convoy ?? []), ...(a.transfer ?? [])]
  const live = (ops.notices ?? []).filter((n) => !n.superseded_at)

  return (
    <div className="flex flex-col gap-4">
      {/* who leads */}
      <Card>
        <CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-base"><Landmark className="size-4" /> Who leads at each level</CardTitle>
          <CardDescription>Escalation goes local → municipal → district → state → national; each tier adds what it controls.</CardDescription></CardHeader>
        <CardContent className="grid gap-2 md:grid-cols-5">
          {(ops.authority ?? []).map((t, i) => (
            <div key={t.tier} className={`rounded-lg border p-2 text-xs ${i === level ? "border-primary bg-primary/10" : i < level ? "bg-muted/60" : "opacity-70"}`}>
              <div className="text-sm font-medium capitalize">{i}. {t.tier}</div>
              <div className="mt-0.5">{t.who}</div>
              <div className="mt-1 text-muted-foreground">{t.does}</div>
            </div>
          ))}
        </CardContent>
      </Card>

      <div className="grid gap-5 xl:grid-cols-[1fr_1.3fr]">
        {/* zones */}
        <Card>
          <CardHeader className="flex-row items-center justify-between pb-2">
            <div><CardTitle className="flex items-center gap-2 text-base"><Siren className="size-4" /> Evacuation zones</CardTitle>
              <CardDescription>From the latest ward risk and open flood reports</CardDescription></div>
            <Button size="sm" variant="outline" disabled={!!busy} onClick={() => go("evac", "/surge/evacuate")}>Stage evacuation now</Button>
          </CardHeader>
          <CardContent className="grid gap-2">
            {(["A", "B", "C"] as const).map((z) => (
              <div key={z} className={`rounded-lg border p-2 ${ZONE[z].cls}`}>
                <div className="text-xs font-semibold uppercase tracking-wide">{ZONE[z].label}</div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {(ops.zones?.[z] ?? []).map((w) => <Badge key={w.id} variant="outline" title={w.why}>{w.name}</Badge>)}
                  {!(ops.zones?.[z] ?? []).length && <span className="text-xs text-muted-foreground">none</span>}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>

        {/* convoys */}
        <Card>
          <CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-base"><Bus className="size-4" /> Convoys on fixed routes</CardTitle>
            <CardDescription>Buses are assigned by the planner; the route is computed once around closures and published</CardDescription></CardHeader>
          <CardContent className="p-0">
            <ScrollArea className="h-[260px] px-4">
              {convoys.map((c) => (
                <div key={c.id} className="mb-2 rounded border p-2 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-medium">{c.title}</div>
                    <Badge variant={c.status === "active" ? "default" : "outline"}>{c.status === "active" ? "moving" : c.status}</Badge>
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {c.detail.people} people · {c.detail.buses} bus(es) · from {c.detail.assembly_point?.name}
                    {" · "}{c.detail.route?.minutes ? `${c.detail.route.minutes} min, ${c.detail.route.km} km` : c.detail.route?.engine}
                    {c.detail.route?.avoids_blocks ? ` · avoids ${c.detail.route.avoids_blocks} closure(s)` : ""}
                  </div>
                  {!!c.detail.route?.streets?.length && <div className="mt-1 flex items-center gap-1 text-xs"><Route className="size-3" />{c.detail.route.streets.join(" → ")}</div>}
                  {c.detail.closed_because && <div className="mt-1 text-xs text-emerald-700">{c.detail.closed_because}</div>}
                </div>
              ))}
              {!convoys.length && <div className="py-3 text-sm text-muted-foreground">No convoys. Zone A wards get one at level 3; zone B follows after A or {45} minutes.</div>}
            </ScrollArea>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-3">
        {/* corridors */}
        <Card>
          <CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-base"><ShieldCheck className="size-4" /> Priority corridors</CardTitle>
            <CardDescription>Police hold the road for ambulances and relief trucks (level 2+)</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            {(a.priority_corridor ?? []).slice(0, 8).map((c) => (
              <div key={c.id} className="flex items-center justify-between rounded border p-2">
                <span>{c.title}</span><Badge variant="outline">{c.status === "active" ? "held" : "released"}</Badge>
              </div>
            ))}
            {!(a.priority_corridor ?? []).length && <span className="text-muted-foreground">None yet.</span>}
          </CardContent>
        </Card>

        {/* shelter options */}
        <Card>
          <CardHeader className="flex-row items-center justify-between pb-2">
            <div><CardTitle className="flex items-center gap-2 text-base"><Bed className="size-4" /> Shelter options</CardTitle>
              <CardDescription>Schools · hotel rooms · host families · unaffected areas</CardDescription></div>
            <Button size="sm" variant="outline" disabled={!!busy} onClick={() => go("pools", "/surge/pools/open")}>Open hotels & hosts</Button>
          </CardHeader>
          <CardContent className="p-0">
            <ScrollArea className="h-[220px] px-4">
              {(ops.pools ?? []).map((p) => (
                <div key={p.id} className="mb-1.5 flex items-center justify-between gap-2 text-sm">
                  <span className="flex items-center gap-1.5">{p.kind === "hotel" ? <Hotel className="size-3.5" /> : p.kind === "host_family" ? <Home className="size-3.5" /> : <Bed className="size-3.5" />}{p.name}</span>
                  <span className="whitespace-nowrap text-xs tabular-nums">{p.occupancy}/{p.capacity} <Badge variant={p.status === "closed" ? "outline" : "default"}>{p.status}</Badge></span>
                </div>
              ))}
              {active("shelter_in_place").length > 0 && <div className="mt-2 text-xs text-muted-foreground">Shelter in place: {active("shelter_in_place").map((x) => x.title.split(":")[0]).join(", ")}</div>}
            </ScrollArea>
          </CardContent>
        </Card>

        {/* crews */}
        <Card>
          <CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-base"><TimerReset className="size-4" /> Crews on duty</CardTitle>
            <CardDescription>{ops.shiftHours} h shift, then {ops.restHours} h rest between jobs (level 1+); volunteers called when 3+ rest</CardDescription></CardHeader>
          <CardContent className="p-0">
            <ScrollArea className="h-[220px] px-4">
              {(ops.shifts ?? []).map((s) => (
                <div key={s.resource_id} className="mb-1.5 flex items-center justify-between text-sm">
                  <span>{s.label}</span>
                  <span className="text-xs text-muted-foreground">{s.rest_until ? `resting until ${time(s.rest_until)}` : `on duty since ${time(s.on_duty_since)}`} · {s.shifts} shift(s)</span>
                </div>
              ))}
              {!(ops.shifts ?? []).length && <div className="py-2 text-sm text-muted-foreground">No crew on a tracked shift.</div>}
            </ScrollArea>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-5 xl:grid-cols-[1.3fr_1fr]">
        {/* public channel */}
        <Card>
          <CardHeader className="pb-2"><CardTitle className="flex items-center gap-2 text-base"><Megaphone className="size-4" /> Public channel</CardTitle>
            <CardDescription>The one place instructions come from (app, SMS, mesh, radio). New notices replace the old ones for the same place. Each notice is sent as alerts to its wards, so it shows in the citizen portal.</CardDescription></CardHeader>
          <CardContent className="grid gap-3 md:grid-cols-[1.2fr_1fr]">
            <ScrollArea className="h-[240px] pr-2">
              {live.map((n) => (
                <div key={n.id} className="mb-2 rounded border p-2">
                  <div className="flex items-center justify-between text-xs text-muted-foreground"><span className="uppercase">{n.kind.replace(/_/g, " ")}</span><span>{time(n.issued_at)}</span></div>
                  <div className="text-sm font-medium">{n.headline}</div>
                  <div className="text-xs">{n.body}</div>
                </div>
              ))}
              {!live.length && <div className="text-sm text-muted-foreground">Nothing published.</div>}
            </ScrollArea>
            <div className="flex flex-col gap-2">
              <Input placeholder="Headline" value={head} onChange={(e) => setHead(e.target.value)} />
              <Textarea placeholder="What people should do" rows={5} value={body} onChange={(e) => setBody(e.target.value)} />
              <Button size="sm" className="gap-1.5" disabled={!!busy || head.length < 3 || body.length < 3}
                      onClick={async () => { await go("notice", "/surge/notice", { body: { region, kind: "general", headline: head, body } }); setHead(""); setBody("") }}>
                <Send className="size-3.5" /> Publish
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* recovery */}
        <Card>
          <CardHeader className="flex-row items-center justify-between pb-2">
            <div><CardTitle className="flex items-center gap-2 text-base"><ClipboardCheck className="size-4" /> Recovery & demobilisation</CardTitle>
              <CardDescription>Stepping down closes what was opened, in reverse</CardDescription></div>
            <Button size="sm" variant="outline" className="gap-1.5" disabled={!!busy || level === 0}
                    onClick={() => go("demob", "/surge/demobilise", { query: { to: Math.max(0, level - 1) } })}>
              <Undo2 className="size-3.5" /> Step down
            </Button>
          </CardHeader>
          <CardContent className="text-sm">
            {(a.recovery ?? []).slice(0, 1).map((r) => (
              <div key={r.id} className="grid gap-1">
                <div>{r.detail.open_incidents} open incident(s) · {r.detail.roads_still_closed?.length ?? 0} road(s) closed · {r.detail.people_still_sheltered} people still sheltered</div>
                <div>{r.detail.relief_sites_to_restock?.length ?? 0} relief site(s) to restock · {r.detail.aid_units_still_deployed} aid unit(s) deployed · {r.detail.crews_resting} crew(s) resting</div>
                <ol className="ml-4 mt-1 list-decimal text-xs text-muted-foreground">{(r.detail.steps ?? []).map((s: string) => <li key={s}>{s}</li>)}</ol>
              </div>
            ))}
            {(a.demobilise ?? []).slice(0, 4).map((d) => <div key={d.id} className="mt-1 text-xs text-muted-foreground">{time(d.created_at)} · {d.title}</div>)}
            {!(a.recovery ?? []).length && !(a.demobilise ?? []).length && <span className="text-muted-foreground">The checklist appears when the ladder returns to normal.</span>}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
