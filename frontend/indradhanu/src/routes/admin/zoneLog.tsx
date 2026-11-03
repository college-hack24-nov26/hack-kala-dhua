import { AlertTriangle, Bell, Brain, ClipboardCheck, Inbox, Navigation, Route as RouteIcon } from "lucide-react"
import type { Beat, DemoEvent, DemoState } from "@/routes/demo/useDemo"
import type { Zone } from "./zones"

/** What the agents wrote about one zone: the audit log (events) and the live
 *  narration (beats), filtered to the zone's ward, incidents and the units
 *  working them. Shared by the agent page and the full-screen view. */

export type Group = "all" | "plan" | "assign" | "route" | "decide" | "alert" | "gap" | "intake"
export type KindGroup = Exclude<Group, "all">

export const GROUP_OF = (kind: string): KindGroup =>
  kind.startsWith("plan.") ? "plan"
  : kind === "assignment.rerouted" || kind.startsWith("road.") ? "route"
  : kind.startsWith("assignment.") ? "assign"
  : kind.startsWith("decision.") ? "decide"
  : kind.startsWith("alert.") ? "alert"
  : kind.startsWith("demand.") || kind.startsWith("agency.") || kind.startsWith("surge.") ? "gap"
  : "intake"

export const GROUP: Record<KindGroup, { label: string; icon: typeof Brain; tone: string }> = {
  plan: { label: "Planning", icon: Brain, tone: "bg-blue-50 text-blue-700 dark:bg-blue-500/15 dark:text-blue-300" },
  assign: { label: "Assignments", icon: Navigation, tone: "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/15 dark:text-emerald-300" },
  route: { label: "Reroutes", icon: RouteIcon, tone: "bg-violet-50 text-violet-700 dark:bg-violet-500/15 dark:text-violet-300" },
  decide: { label: "Decisions", icon: ClipboardCheck, tone: "bg-amber-50 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300" },
  alert: { label: "Alerts", icon: Bell, tone: "bg-fuchsia-50 text-fuchsia-700 dark:bg-fuchsia-500/15 dark:text-fuchsia-300" },
  gap: { label: "Gaps", icon: AlertTriangle, tone: "bg-red-50 text-red-700 dark:bg-red-500/15 dark:text-red-300" },
  intake: { label: "Reports", icon: Inbox, tone: "bg-sky-50 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300" },
}

export const time = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })
export const pretty = (s: string) => s.replace(/[._]/g, " ")
export const reasonOf = (e: DemoEvent) =>
  typeof e.payload?.reason === "string" ? (e.payload.reason as string) : ""

function keys(state: DemoState, zone: Zone) {
  const ids = new Set(zone.incidents.map((i) => i.id))
  const units = new Set([
    ...state.resources.filter((r) => r.incidentId && ids.has(r.incidentId)).map((r) => r.id),
    ...state.routes.filter((r) => ids.has(r.incidentId)).map((r) => r.resourceId),
  ])
  const hit = (v: unknown) => typeof v === "string" && (ids.has(v) || units.has(v) || v === zone.id)
  return { ids, units, hit }
}

/** The zone's audit log, newest first. Plans are city-wide, so they are kept. */
export function zoneEvents(state: DemoState, zone: Zone): DemoEvent[] {
  const { hit } = keys(state, zone)
  const mine = (e: DemoEvent) => {
    if (e.wardId === zone.id || hit(e.subjectId)) return true
    const p = e.payload ?? {}
    return ["incident_id", "to_incident", "from_incident", "resource_id", "ward_id", "unit_id"].some((k) => hit(p[k]))
  }
  return state.events
    .filter((e) => mine(e) || e.kind.startsWith("plan."))
    .sort((a, b) => Date.parse(b.occurredAt) - Date.parse(a.occurredAt))
}

/** The runner's narration for this zone, newest first. */
export function zoneBeats(state: DemoState, zone: Zone): Beat[] {
  const { hit } = keys(state, zone)
  return state.beats
    .filter((b) => {
      const d = b.detail ?? {}
      return ["wardId", "incidentId", "resourceId", "subjectId", "fromIncidentId"].some((k) => hit(d[k]))
    })
    .sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : b.tick - a.tick))
}
