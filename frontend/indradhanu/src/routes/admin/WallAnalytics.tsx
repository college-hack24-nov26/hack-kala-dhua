import { useMemo, type ReactNode } from "react"
import { useNavigate } from "react-router-dom"
import { ArrowUpRight } from "lucide-react"
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts"
import type { DemoState } from "@/routes/demo/useDemo"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { useMeshStatus } from "./meshApi"
import { isOpen, type Zone } from "./zones"
import { StatCard, useSeries } from "@/components/common/StatCard"

/** Everything the system knows, as charts, each with a jump to where it is acted on.
 *
 *  Colours by job (see the dataviz method): severity uses the fixed status
 *  palette (critical / serious / warning / neutral) and always carries its "S4"
 *  label; identity (report source, unit state) uses the categorical order below,
 *  assigned by entity, never by rank; plain magnitude is one blue hue.
 */

const CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
const OTHER = "#898781"
const MAG = "#2a78d6"
const SEV: Record<number, string> = { 5: "#d03b3b", 4: "#ec835a", 3: "#fab219", 2: "#a1a1aa", 1: "#d4d4d8" }
const GOOD = "#0ca30c"

const axis = { fontSize: 11, fill: "var(--muted-foreground)" }
const tip = {
  contentStyle: {
    background: "var(--popover)", border: "1px solid var(--border)", borderRadius: 8,
    fontSize: 12, color: "var(--popover-foreground)",
  },
  cursor: { fill: "var(--muted)", opacity: 0.4 },
}

function Panel({ title, sub, to, cta, children, className }: {
  title: string; sub?: string; to: string; cta: string; children: ReactNode; className?: string
}) {
  const navigate = useNavigate()
  return (
    <Card className={`flex flex-col ${className ?? ""}`}>
      <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0 pb-1">
        <div className="min-w-0">
          <CardTitle className="text-sm">{title}</CardTitle>
          {sub && <CardDescription className="text-xs">{sub}</CardDescription>}
        </div>
        <button
          onClick={() => navigate(to)}
          className="text-primary inline-flex shrink-0 items-center gap-0.5 rounded-md px-1.5 py-0.5 text-xs hover:bg-muted"
        >
          {cta} <ArrowUpRight className="size-3.5" />
        </button>
      </CardHeader>
      <CardContent className="flex-1 pb-3">{children}</CardContent>
    </Card>
  )
}

function Stat({ label, value, sub, to, warn }: {
  label: string; value: string | number; sub?: string; to: string; warn?: boolean
}) {
  const navigate = useNavigate()
  const series = useSeries(`wall:${label}`, parseFloat(String(value)))
  return (
    <StatCard label={label} value={value} sub={sub} series={series}
              tone={warn ? "warn" : undefined} onClick={() => navigate(to)} />
  )
}

const Empty = ({ text }: { text: string }) => (
  <div className="text-muted-foreground flex h-[180px] items-center justify-center text-xs">{text}</div>
)

const pretty = (s: string) => s.replace(/_/g, " ")

export default function WallAnalytics({ zones, now, state, scope, onClearScope, stats = true }: {
  zones: Zone[]; now: number; state: DemoState
  /** The zone whose screen is selected, or null for the whole city. */
  scope: Zone | null
  onClearScope: () => void
  /** The headline number row; a zone page has its own. */
  stats?: boolean
}) {
  const { data: mesh } = useMeshStatus(5000)

  const d = useMemo(() => {
    const open = state.incidents.filter((i) => isOpen(i.status))

    const bySeverity = [5, 4, 3, 2, 1].map((s) => ({
      name: `S${s}`, sev: s, value: open.filter((i) => i.severity === s).length,
    })).filter((r) => r.value > 0 || r.sev >= 3)

    const catMap = new Map<string, number>()
    for (const i of open) catMap.set(i.category, (catMap.get(i.category) ?? 0) + 1)
    const byCategory = [...catMap].map(([k, v]) => ({ name: pretty(k), value: v }))
      .sort((a, b) => b.value - a.value).slice(0, 8)

    const byZone = zones.slice(0, 8).map((z) => ({
      name: z.name.length > 18 ? `${z.name.slice(0, 17)}…` : z.name,
      assigned: z.incidents.length - z.unattended,
      unassigned: z.unattended,
    }))

    // Units, by what they are doing. Fixed entity order so colours never move.
    const UNIT_STATES = ["available", "assigned", "en_route", "on_site", "returning", "offline"]
    const unitCount = new Map<string, number>()
    for (const r of state.resources) {
      const s = (r.assignmentStatus ?? r.status ?? "other").toLowerCase()
      const k = UNIT_STATES.includes(s) ? s : s === "busy" ? "assigned" : "other"
      unitCount.set(k, (unitCount.get(k) ?? 0) + 1)
    }
    const units = [...UNIT_STATES, "other"]
      .map((k, i) => ({ name: pretty(k), value: unitCount.get(k) ?? 0, color: k === "other" ? OTHER : CAT[i] }))
      .filter((u) => u.value > 0)
    const committed = state.resources.filter((r) => r.incidentId).length

    const capMap = new Map<string, { required: number; met: number }>()
    for (const n of state.needs) {
      const c = capMap.get(n.capability) ?? { required: 0, met: 0 }
      c.required += n.required
      c.met += Math.min(n.met, n.required)
      capMap.set(n.capability, c)
    }
    const needs = [...capMap].map(([k, v]) => ({ name: pretty(k), met: v.met, short: v.required - v.met }))
      .sort((a, b) => b.met + b.short - (a.met + a.short))
    const needTotal = needs.reduce((s, n) => s + n.met + n.short, 0)
    const needMet = needs.reduce((s, n) => s + n.met, 0)

    const SOURCES = ["app", "mesh", "camera / vlm", "crew", "other"]
    const srcOf = (s: string) => {
      const x = s.toLowerCase()
      if (x.startsWith("mesh")) return "mesh"
      if (/sensor|camera|vlm|vision/.test(x)) return "camera / vlm"
      if (/field|crew/.test(x)) return "crew"
      if (/app|citizen|phone|voice|web/.test(x)) return "app"
      return "other"
    }
    const srcCount = new Map<string, { opened: number; merged: number; held: number }>()
    for (const r of state.reports) {
      const k = srcOf(r.source)
      const c = srcCount.get(k) ?? { opened: 0, merged: 0, held: 0 }
      if (r.opened) c.opened += 1
      else if (r.incidentId) c.merged += 1
      else c.held += 1
      srcCount.set(k, c)
    }
    const sources = SOURCES.filter((s) => srcCount.has(s)).map((s) => ({ name: s, ...srcCount.get(s)! }))

    // Report arrivals, last 2 hours in 10-minute buckets.
    const buckets = Array.from({ length: 12 }, (_, i) => {
      const end = now - (11 - i) * 10 * 60_000
      return {
        name: new Date(end).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false }),
        end, value: 0,
      }
    })
    for (const r of state.reports) {
      const t = Date.parse(r.createdAt)
      const age = now - t
      if (age < 0 || age >= 120 * 60_000) continue
      const idx = 11 - Math.floor(age / (10 * 60_000))
      if (buckets[idx]) buckets[idx].value += 1
    }

    const risk = [...state.wards].filter((w) => w.score != null)
      .sort((a, b) => (b.score ?? 0) - (a.score ?? 0)).slice(0, 8)
      .map((w) => ({ name: w.name.length > 18 ? `${w.name.slice(0, 17)}…` : w.name, value: Math.round((w.score ?? 0) * 100), sev: w.severity ?? 0 }))

    const shelters = state.facilities.filter((f) => (f.capacity ?? 0) > 0)
      .map((f) => ({ name: f.name.length > 20 ? `${f.name.slice(0, 19)}…` : f.name, value: Math.round(((f.occupancy ?? 0) / (f.capacity || 1)) * 100) }))
      .sort((a, b) => b.value - a.value).slice(0, 8)

    const DEC = ["awaiting_approval", "approved", "executed", "rejected", "expired"]
    const decCount = new Map<string, number>()
    for (const x of state.decisions) decCount.set(x.status, (decCount.get(x.status) ?? 0) + 1)
    const decisions = [...DEC, ...[...decCount.keys()].filter((k) => !DEC.includes(k))]
      .filter((k) => decCount.has(k)).map((k) => ({ name: pretty(k), value: decCount.get(k)! }))
    const waiting = decCount.get("awaiting_approval") ?? 0

    const alertMap = new Map<string, number>()
    for (const a of state.alerts) alertMap.set(a.wardName ?? a.wardId, (alertMap.get(a.wardName ?? a.wardId) ?? 0) + (a.reach || 0))
    const alerts = [...alertMap].map(([k, v]) => ({ name: k.length > 18 ? `${k.slice(0, 17)}…` : k, value: v }))
      .sort((a, b) => b.value - a.value).slice(0, 8)
    const reach = state.alerts.reduce((s, a) => s + (a.reach || 0), 0)

    return {
      open, bySeverity, byCategory, byZone, units, committed, needs, needTotal, needMet,
      sources, buckets, risk, shelters, decisions, waiting, alerts, reach,
    }
  }, [state, zones, now])

  const packets = useMemo(() => {
    const m = new Map<string, number>()
    for (const p of mesh?.recent ?? []) m.set(p.type, (m.get(p.type) ?? 0) + 1)
    const LABEL: Record<string, string> = { R: "reports", S: "camera", F: "crew", H: "heartbeat", K: "ack" }
    return [...m].map(([k, v]) => ({ name: LABEL[k] ?? k, value: v }))
  }, [mesh])
  const gateways = mesh?.nodes.filter((n) => n.kind === "gateway" && n.ageS < 60).length ?? 0
  const coverage = d.needTotal ? Math.round((d.needMet / d.needTotal) * 100) : 100
  const unattended = d.open.filter((i) => !i.unitsEnRoute).length

  return (
    <section className="space-y-5">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold tracking-tight">Analytics</h2>
        {scope ? (
          <>
            <span className="rounded-md bg-sky-500/15 px-2 py-0.5 text-xs font-medium text-sky-800 dark:text-sky-300">
              {scope.name} only
            </span>
            <button onClick={onClearScope} className="text-primary text-xs hover:underline">
              show the whole city
            </button>
          </>
        ) : (
          <span className="text-muted-foreground text-xs">
            Whole city · click a zone screen above to see just that area
          </span>
        )}
      </div>

      {stats && <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Open incidents" value={d.open.length} sub={`${zones.length} zones`} to="/admin/response" />
        <Stat label="Nobody assigned" value={unattended} to="/admin/response" warn={unattended > 0} />
        <Stat label="Units committed" value={`${d.committed}/${state.resources.length}`} to="/admin/dispatch" />
        <Stat label="Needs covered" value={`${coverage}%`} sub={`${d.needMet} of ${d.needTotal}`} to="/admin/allocation" warn={coverage < 100} />
        <Stat label="Reports in" value={state.reports.length} to="/admin/feed" />
        <Stat label="Waiting approval" value={d.waiting} to="/admin/decisions" warn={d.waiting > 0} />
        <Stat label="Alerts out" value={state.alerts.length} sub={`${d.reach.toLocaleString()} reached`} to="/admin/alerts" />
        <Stat label="Mesh gateways" value={gateways} sub="linked now" to="/admin/mesh" warn={gateways === 0} />
      </div>}

      <div className="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
        <Panel title="Open incidents by severity" sub="S5 critical → S1 minor" to="/admin/response" cta="Incidents & response">
          <ResponsiveContainer width="100%" height={180}>
            <BarChart data={d.bySeverity} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--border)" />
              <XAxis dataKey="name" tick={axis} tickLine={false} axisLine={false} />
              <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
              <Tooltip {...tip} />
              <Bar isAnimationActive={false} dataKey="value" name="incidents" radius={[4, 4, 0, 0]} maxBarSize={36}>
                {d.bySeverity.map((r) => <Cell key={r.name} fill={SEV[r.sev]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Panel>

        <Panel title="Open incidents by type" to="/admin/incidents" cta="Incident queue">
          {d.byCategory.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={d.byCategory} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 0 }}>
                <XAxis type="number" allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
                <YAxis type="category" dataKey="name" width={110} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} />
                <Bar isAnimationActive={false} dataKey="value" name="incidents" fill={MAG} radius={[0, 4, 4, 0]} maxBarSize={16} />
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No open incidents." />}
        </Panel>

        <Panel title="Zones: assigned vs nobody on the way" sub="open incidents per active ward" to="/admin/dispatch" cta="Who is on what">
          {d.byZone.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={d.byZone} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 0 }}>
                <XAxis type="number" allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
                <YAxis type="category" dataKey="name" width={110} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} />
                <Legend iconType="circle" wrapperStyle={{ fontSize: 11 }} />
                <Bar isAnimationActive={false} dataKey="assigned" name="unit on the way" stackId="z" fill={CAT[0]} maxBarSize={16} />
                <Bar isAnimationActive={false} dataKey="unassigned" name="nobody assigned" stackId="z" fill={SEV[3]} radius={[0, 4, 4, 0]} maxBarSize={16} />
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No active zones." />}
        </Panel>

        <Panel title="Allocation: needs met vs short" sub={`${coverage}% of required capability covered`} to="/admin/allocation" cta="Allocation planner">
          {d.needs.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={d.needs} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis dataKey="name" tick={axis} tickLine={false} axisLine={false} interval={0} angle={-20} textAnchor="end" height={44} />
                <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} />
                <Legend iconType="circle" wrapperStyle={{ fontSize: 11 }} />
                <Bar isAnimationActive={false} dataKey="met" name="covered" stackId="n" fill={GOOD} maxBarSize={28} />
                <Bar isAnimationActive={false} dataKey="short" name="short" stackId="n" fill={SEV[5]} radius={[4, 4, 0, 0]} maxBarSize={28} />
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No open needs." />}
        </Panel>

        <Panel title="Fleet: what units are doing" sub={`${d.committed} of ${state.resources.length} committed`} to="/admin/resources" cta="Resources">
          {d.units.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={d.units} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis dataKey="name" tick={axis} tickLine={false} axisLine={false} />
                <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} />
                <Bar isAnimationActive={false} dataKey="value" name="units" radius={[4, 4, 0, 0]} maxBarSize={36}>
                  {d.units.map((u) => <Cell key={u.name} fill={u.color} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No units loaded." />}
        </Panel>

        <Panel title="Reports arriving" sub="last 2 hours, per 10 minutes" to="/admin/feed" cta="Live feed">
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={d.buckets} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
              <CartesianGrid vertical={false} stroke="var(--border)" />
              <XAxis dataKey="name" tick={axis} tickLine={false} axisLine={false} interval={2} />
              <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
              <Tooltip {...tip} cursor={{ stroke: "var(--muted-foreground)", strokeWidth: 1 }} />
              <Area isAnimationActive={false} type="monotone" dataKey="value" name="reports" stroke={MAG} strokeWidth={2} fill={MAG} fillOpacity={0.15} />
            </AreaChart>
          </ResponsiveContainer>
        </Panel>

        <Panel title="Reports by channel and outcome" sub="opened an incident, merged into one, or held" to="/admin/intake" cta="Reports">
          {d.sources.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={d.sources} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis dataKey="name" tick={axis} tickLine={false} axisLine={false} />
                <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} />
                <Legend iconType="circle" wrapperStyle={{ fontSize: 11 }} />
                <Bar isAnimationActive={false} dataKey="opened" name="opened" stackId="s" fill={CAT[1]} maxBarSize={32} />
                <Bar isAnimationActive={false} dataKey="merged" name="merged" stackId="s" fill={CAT[0]} maxBarSize={32} />
                <Bar isAnimationActive={false} dataKey="held" name="held" stackId="s" fill={OTHER} radius={[4, 4, 0, 0]} maxBarSize={32} />
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No reports yet." />}
        </Panel>

        <Panel title="Ward risk" sub="highest hazard scores now" to="/admin/risk" cta="Risk board">
          {d.risk.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={d.risk} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 0 }}>
                <XAxis type="number" domain={[0, 100]} tick={axis} tickLine={false} axisLine={false} unit="%" />
                <YAxis type="category" dataKey="name" width={110} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} formatter={(v) => [`${v}%`, "risk"]} />
                <Bar isAnimationActive={false} dataKey="value" name="risk" radius={[0, 4, 4, 0]} maxBarSize={16}>
                  {d.risk.map((r) => <Cell key={r.name} fill={SEV[Math.max(1, Math.min(5, r.sev))] ?? MAG} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No hazard run yet." />}
        </Panel>

        <Panel title="Shelter and hospital load" sub="occupancy as % of capacity" to="/admin/forecast" cta="Forecast">
          {d.shelters.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={d.shelters} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 0 }}>
                <XAxis type="number" domain={[0, 100]} tick={axis} tickLine={false} axisLine={false} unit="%" />
                <YAxis type="category" dataKey="name" width={120} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} formatter={(v) => [`${v}%`, "full"]} />
                <Bar isAnimationActive={false} dataKey="value" name="full" radius={[0, 4, 4, 0]} maxBarSize={16}>
                  {d.shelters.map((s) => <Cell key={s.name} fill={s.value >= 90 ? SEV[5] : s.value >= 70 ? SEV[3] : GOOD} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No facility has a capacity set." />}
        </Panel>

        <Panel title="Decisions at the gate" sub={`${d.waiting} waiting for an officer`} to="/admin/decisions" cta="Approvals">
          {d.decisions.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={d.decisions} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis dataKey="name" tick={axis} tickLine={false} axisLine={false} />
                <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} />
                <Bar isAnimationActive={false} dataKey="value" name="decisions" fill={MAG} radius={[4, 4, 0, 0]} maxBarSize={36} />
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No decisions yet." />}
        </Panel>

        <Panel title="Alerts: people reached by ward" sub={`${d.reach.toLocaleString()} estimated reach`} to="/admin/alerts" cta="Issued alerts">
          {d.alerts.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={d.alerts} layout="vertical" margin={{ top: 4, right: 12, left: 8, bottom: 0 }}>
                <XAxis type="number" tick={axis} tickLine={false} axisLine={false} />
                <YAxis type="category" dataKey="name" width={110} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} />
                <Bar isAnimationActive={false} dataKey="value" name="people" fill={MAG} radius={[0, 4, 4, 0]} maxBarSize={16} />
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No alerts issued." />}
        </Panel>

        <Panel title="Mesh traffic" sub={`${gateways} gateway phone${gateways === 1 ? "" : "s"} linked · latest packets`} to="/admin/mesh" cta="Mesh & devices">
          {packets.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={packets} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke="var(--border)" />
                <XAxis dataKey="name" tick={axis} tickLine={false} axisLine={false} />
                <YAxis allowDecimals={false} tick={axis} tickLine={false} axisLine={false} />
                <Tooltip {...tip} />
                <Bar isAnimationActive={false} dataKey="value" name="packets" fill={MAG} radius={[4, 4, 0, 0]} maxBarSize={36} />
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty text="No mesh packets yet." />}
        </Panel>
      </div>
    </section>
  )
}
