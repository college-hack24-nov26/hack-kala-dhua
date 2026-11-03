import { forwardRef, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { ArrowUpRight, ChevronDown, Clock, FileText, HeartPulse, Inbox } from "lucide-react"
import type { DemoState, Incident } from "@/routes/demo/useDemo"
import { cn } from "@/lib/utils"
import { HAZARD_ICON, SEV_BAR, SEV_CHIP } from "./ZoneMap"
import { hazardOf, isLifeSafety, type Hazard } from "./zones"

/** Every open incident with no unit allocated: nobody driving to it and
 *  nobody on scene. Worst first, then lives at stake, then longest waiting,
 *  with what it still needs and the planner's own reason it is uncovered. */

const SHOW = 6

const waited = (iso: string, now: number) => {
  const m = Math.max(0, Math.round((now - (Date.parse(iso) || now)) / 60000))
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`
}

export const UnassignedIncidents = forwardRef<HTMLElement, {
  state: DemoState
  now: number
  sev: number
  hazards: Hazard[]
  q: string
  rankOf: Map<string, number>
}>(function UnassignedIncidents({ state, now, sev, hazards, q, rankOf }, ref) {
  const navigate = useNavigate()
  const [all, setAll] = useState(false)

  const rows = useMemo(() => {
    const working = new Set(state.resources.filter((r) => r.incidentId).map((r) => r.incidentId as string))
    const ward = new Map(state.wards.map((w) => [w.id, w.name]))
    const reason = new Map<string, string>()
    for (const u of state.plan?.uncovered ?? []) if (u.incident_id && !reason.has(u.incident_id)) reason.set(u.incident_id, u.reason)
    const needs = new Map<string, { capability: string; short: number }[]>()
    for (const n of state.needs) {
      if (n.met >= n.required) continue
      const list = needs.get(n.incidentId) ?? []
      list.push({ capability: n.capability, short: n.required - n.met })
      needs.set(n.incidentId, list)
    }
    const needle = q.trim().toLowerCase()
    return state.incidents
      .filter((i: Incident) => !/resolved|closed|cancel/i.test(i.status))
      .filter((i) => !i.unitsEnRoute && !working.has(i.id))
      .filter((i) => i.severity >= sev)
      .filter((i) => !hazards.length || hazards.includes(hazardOf(i.category)))
      .filter((i) => !needle || `${i.title} ${ward.get(i.wardId) ?? ""} ${i.street ?? ""}`.toLowerCase().includes(needle))
      .map((i) => ({
        i, ward: ward.get(i.wardId) ?? i.wardId, life: isLifeSafety(i.category),
        needs: needs.get(i.id) ?? [], reason: reason.get(i.id) ?? null,
      }))
      .sort((a, b) =>
        b.i.severity - a.i.severity || Number(b.life) - Number(a.life) ||
        (Date.parse(a.i.createdAt) || 0) - (Date.parse(b.i.createdAt) || 0))
  }, [state, sev, hazards, q])

  const shown = all ? rows : rows.slice(0, SHOW)
  const life = rows.filter((r) => r.life).length

  return (
    <section ref={ref} className="scroll-mt-24 rounded-2xl border bg-card shadow-card">
      <div className="flex flex-wrap items-center gap-3 border-b px-5 py-4">
        <span className="grid size-9 place-items-center rounded-xl bg-red-50 text-red-600 dark:bg-red-500/15 dark:text-red-300">
          <Inbox className="size-4" />
        </span>
        <div className="min-w-0">
          <h3 className="text-[15px] font-semibold tracking-tight">Incidents with no allocation</h3>
          <p className="text-xs text-muted-foreground">
            Nobody on the way and nobody on scene · worst first, then lives at stake, then longest waiting
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {life > 0 && (
            <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2.5 py-1 text-[11px] font-medium text-red-700 ring-1 ring-red-200 dark:bg-red-500/15 dark:text-red-300 dark:ring-red-500/30">
              <HeartPulse className="size-3" /> {life} life-safety
            </span>
          )}
          <span className="rounded-full bg-muted px-2.5 py-1 text-xs font-semibold tabular-nums">{rows.length}</span>
        </div>
      </div>

      {rows.length ? (
        <ul className="divide-y">
          {shown.map(({ i, ward, life: isLife, needs, reason }) => {
            const Icon = HAZARD_ICON[hazardOf(i.category)]
            const rank = rankOf.get(i.wardId)
            return (
              <li key={i.id} className="flex flex-wrap items-start gap-4 px-5 py-4 md:flex-nowrap">
                <span className={cn("mt-1 h-10 w-1 shrink-0 rounded-full", SEV_BAR[i.severity] ?? "bg-zinc-300")} />
                <span className="grid size-10 shrink-0 place-items-center rounded-xl bg-accent text-accent-foreground">
                  <Icon className="size-4" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-medium">{i.title}</span>
                    <span className={cn("rounded-md px-1.5 py-0.5 text-[10px] font-semibold", SEV_CHIP[i.severity])}>S{i.severity}</span>
                    {isLife && (
                      <span className="inline-flex items-center gap-1 rounded-md bg-red-50 px-1.5 py-0.5 text-[10px] font-semibold text-red-700 dark:bg-red-500/15 dark:text-red-300">
                        <HeartPulse className="size-3" /> life at stake
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
                    <span>{ward}{i.street ? ` · ${i.street}` : ""}</span>
                    <span className="inline-flex items-center gap-1"><Clock className="size-3" /> waiting {waited(i.createdAt, now)}</span>
                    <span className="inline-flex items-center gap-1"><FileText className="size-3" /> {i.reportCount} report{i.reportCount === 1 ? "" : "s"}</span>
                  </div>
                  {needs.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {needs.map((n) => (
                        <span key={n.capability} className="rounded-md border bg-muted/50 px-1.5 py-0.5 text-[11px]">
                          needs {n.short} × {n.capability.replace(/_/g, " ")}
                        </span>
                      ))}
                    </div>
                  )}
                  <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                    <b className="font-medium text-foreground">Why: </b>
                    {reason ?? "Not in a plan yet. The planner runs on every new report, so this is picked up on the next pass unless no unit can reach it."}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2 md:flex-col md:items-stretch">
                  <button
                    onClick={() => navigate(`/admin/response?incident=${i.id}`)}
                    className="inline-flex h-8 items-center justify-center gap-1 rounded-lg bg-primary px-3 text-xs font-medium text-primary-foreground shadow-xs hover:bg-primary/90"
                  >
                    Respond <ArrowUpRight className="size-3.5" />
                  </button>
                  <button
                    onClick={() => navigate(`/admin/wall/zone/${encodeURIComponent(i.wardId)}`)}
                    className="inline-flex h-8 items-center justify-center gap-1 rounded-lg border bg-card px-3 text-xs font-medium shadow-xs hover:bg-muted"
                  >
                    Zone{rank ? ` #${rank}` : ""}
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      ) : (
        <p className="px-5 py-8 text-center text-sm text-muted-foreground">
          Every open incident{sev > 1 || hazards.length || q ? " matching these filters" : ""} has a unit allocated.
        </p>
      )}

      {rows.length > SHOW && (
        <button
          onClick={() => setAll((v) => !v)}
          className="flex w-full items-center justify-center gap-1 border-t px-5 py-3 text-xs font-medium text-primary hover:bg-muted/50"
        >
          {all ? "Show fewer" : `Show all ${rows.length}`}
          <ChevronDown className={cn("size-3.5 transition-transform", all && "rotate-180")} />
        </button>
      )}
    </section>
  )
})
