import type { DemoState, Incident } from "@/routes/demo/useDemo"

/** A "zone" is a ward with at least one open incident: one screen on the wall.
 *
 *  Grouping by ward rather than by distance clustering is deliberate. A ward is
 *  a unit someone is responsible for, it has a name an officer recognises, and
 *  it does not reshuffle when one more report lands 200 m away, so the wall is
 *  stable: screens are added when a new ward is hit and removed when its last
 *  incident closes, and never jump between positions otherwise.
 */
export type Zone = {
  id: string
  name: string
  incidents: Incident[]
  severity: number
  reports: number
  unitsEnRoute: number
  unattended: number
  center: [number, number]
  radiusKm: number
  since: number
  /** Hazard groups present among the zone's open incidents. */
  hazards: Hazard[]
  /** Open incidents where a life is directly at stake: casualties, people
   *  stranded, fire, collapse, evacuation. */
  lifeSafety: number
  /** The ward's own estimate of people at risk (from the hazard agent), or null
   *  when the ward has not been scored. */
  exposed: number | null
  needsTotal: number
  needsMet: number
  /** Units working this zone that are already on scene. */
  onScene: number
  allocation: Allocation
  /** 0-100, how urgently this zone needs attention. See `riskIndex`. */
  risk: number
}

/** Where the allocation for a zone stands, in the order an officer worries. */
export type Allocation = "unassigned" | "partial" | "enroute" | "covered"
export const ALLOCATION: Record<Allocation, { label: string; tone: string; dot: string }> = {
  unassigned: { label: "Nobody assigned", tone: "bg-red-50 text-red-700 ring-red-200 dark:bg-red-500/15 dark:text-red-300 dark:ring-red-500/30", dot: "bg-red-500" },
  partial: { label: "Partly covered", tone: "bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-500/15 dark:text-amber-300 dark:ring-amber-500/30", dot: "bg-amber-500" },
  enroute: { label: "Units en route", tone: "bg-blue-50 text-blue-700 ring-blue-200 dark:bg-blue-500/15 dark:text-blue-300 dark:ring-blue-500/30", dot: "bg-blue-500" },
  covered: { label: "Covered", tone: "bg-emerald-50 text-emerald-700 ring-emerald-200 dark:bg-emerald-500/15 dark:text-emerald-300 dark:ring-emerald-500/30", dot: "bg-emerald-500" },
}

/** Incident categories, grouped the way an officer filters them. */
export type Hazard = "flood" | "fire" | "structure" | "trees" | "heat" | "people" | "other"
export const HAZARD_OF: Record<string, Hazard> = {
  flooded_road: "flood", waterlogging: "flood", blocked_drain: "flood", flood: "flood",
  fire: "fire",
  structural_damage: "structure", earthquake_damage: "structure", collapse: "structure",
  fallen_tree: "trees", power_line: "trees", tree: "trees",
  heat_casualty: "heat", air_quality: "heat",
  person_stranded: "people", evacuation: "people", shelter_full: "people", traffic_corridor: "people",
}
export const hazardOf = (category: string): Hazard => HAZARD_OF[category] ?? "other"
export const HAZARD_LABEL: Record<Hazard, string> = {
  flood: "Flood", fire: "Fire", structure: "Structural", trees: "Trees & power",
  heat: "Heat & air", people: "People", other: "Other",
}
const LIFE = new Set([
  "heat_casualty", "person_stranded", "fire", "structural_damage", "earthquake_damage", "evacuation", "collapse",
])
export const isLifeSafety = (category: string) => LIFE.has(category)

/** How urgently a zone needs attention, 0-100.
 *
 *    50  worst severity (S5 = 50)
 *    25  incidents where a life is at stake (5 or more = 25)
 *    15  people the ward estimates are at risk (20,000 or more = 15)
 *    10  incidents nobody is on the way to yet (3 or more = 10)
 *
 *  Fixed weights, not learned: an officer can check the arithmetic. */
export function riskIndex(z: Pick<Zone, "severity" | "lifeSafety" | "exposed" | "unattended">): number {
  return Math.round(
    (z.severity / 5) * 50 +
    (Math.min(z.lifeSafety, 5) / 5) * 25 +
    Math.min((z.exposed ?? 0) / 20000, 1) * 15 +
    (Math.min(z.unattended, 3) / 3) * 10,
  )
}

export const isOpen = (status: string) => !/resolved|closed|cancel/i.test(status)

export function km(a: [number, number], b: [number, number]): number {
  const R = 6371
  const dLat = ((b[1] - a[1]) * Math.PI) / 180
  const dLng = ((b[0] - a[0]) * Math.PI) / 180
  const la = (a[1] * Math.PI) / 180
  const lb = (b[1] * Math.PI) / 180
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(la) * Math.cos(lb) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}

export function zonesOf(state: DemoState): Zone[] {
  const wardName = new Map(state.wards.map((w) => [w.id, w.name]))
  const wardExposed = new Map(state.wards.map((w) => [w.id, w.populationAtRisk]))
  const wardCentre = new Map(state.wards.map((w) => [w.id, w.centroid]))
  const byWard = new Map<string, Incident[]>()
  for (const i of state.incidents) {
    if (!isOpen(i.status)) continue
    const list = byWard.get(i.wardId) ?? []
    list.push(i)
    byWard.set(i.wardId, list)
  }
  const zones: Zone[] = []
  for (const [id, incidents] of byWard) {
    const n = incidents.length
    const lng = incidents.reduce((s, i) => s + i.location[0], 0) / n
    const lat = incidents.reduce((s, i) => s + i.location[1], 0) / n
    const center: [number, number] =
      Number.isFinite(lng) && Number.isFinite(lat) ? [lng, lat] : (wardCentre.get(id) ?? [0, 0])
    const radiusKm = Math.max(0.6, ...incidents.map((i) => km(center, i.location)))
    const ids = new Set(incidents.map((i) => i.id))
    const needs = state.needs.filter((x) => ids.has(x.incidentId))
    const needsTotal = needs.reduce((s, x) => s + x.required, 0)
    const needsMet = needs.reduce((s, x) => s + Math.min(x.met, x.required), 0)
    const working = state.resources.filter((r) => r.incidentId && ids.has(r.incidentId))
    const onScene = working.filter((r) => /on_site|on_scene/.test(r.status) || r.assignmentStatus === "on_site").length
    const unitsEnRoute = incidents.reduce((s, i) => s + (i.unitsEnRoute || 0), 0)
    const busy = new Set(working.map((r) => r.incidentId))
    // No allocation: nobody driving to it and nobody on scene.
    const unattended = incidents.filter((i) => !i.unitsEnRoute && !busy.has(i.id)).length
    const allocation: Allocation =
      unitsEnRoute === 0 && onScene === 0 ? "unassigned"
      : unattended > 0 ? "partial"
      : needsTotal > 0 && needsMet >= needsTotal && onScene > 0 ? "covered"
      : "enroute"
    const lifeSafety = incidents.filter((i) => isLifeSafety(i.category)).length
    const exposed = wardExposed.get(id) ?? null
    const severity = Math.max(...incidents.map((i) => i.severity))
    zones.push({
      id,
      name: wardName.get(id) ?? id,
      incidents: [...incidents].sort((a, b) => b.severity - a.severity),
      severity,
      reports: incidents.reduce((s, i) => s + (i.reportCount || 0), 0),
      unitsEnRoute,
      unattended,
      center,
      radiusKm,
      since: Math.min(...incidents.map((i) => Date.parse(i.createdAt) || Date.now())),
      hazards: [...new Set(incidents.map((i) => hazardOf(i.category)))],
      lifeSafety,
      exposed,
      needsTotal,
      needsMet,
      onScene,
      allocation,
      risk: riskIndex({ severity, lifeSafety, exposed, unattended }),
    })
  }
  // Riskiest first: the index, then lives at stake, then people exposed; ties
  // by how long it has been going on, so positions are stable.
  return zones.sort(
    (a, b) =>
      b.risk - a.risk || b.lifeSafety - a.lifeSafety || (b.exposed ?? 0) - (a.exposed ?? 0) ||
      b.incidents.length - a.incidents.length || a.since - b.since,
  )
}

/** What a zone screen draws: the zone's incidents and whatever is working them
 *  or is near enough to matter, not the whole city. */
export function zoneLayers(state: DemoState, z: Zone) {
  const ids = new Set(z.incidents.map((i) => i.id))
  const near = (loc: [number, number]) => km(z.center, loc) <= Math.max(2.5, z.radiusKm * 2)
  return {
    incidents: z.incidents,
    resources: state.resources.filter(
      (r) => (r.incidentId && ids.has(r.incidentId)) || near(r.location),
    ),
    facilities: state.facilities.filter((f) => near(f.location)),
    blocks: state.roadBlocks.filter((b) => near(b.location)),
    needs: state.needs.filter((n) => ids.has(n.incidentId)),
    routes: state.routes.filter((r) => ids.has(r.incidentId)),
    wards: state.wards.filter((w) => w.id === z.id),
  }
}

/** Zoom that fits a zone's spread on a ~400 px screen. */
export function zoomFor(z: Zone, big = false): number {
  const base = z.radiusKm < 0.8 ? 14.6 : z.radiusKm < 1.6 ? 13.8 : z.radiusKm < 3 ? 13 : 12.2
  return big ? base + 0.6 : base
}

/** The world as seen from one zone: what the analytics under the wall show
 *  when that zone's screen is selected. `null` is the whole city. */
export function scopeState(state: DemoState, z: Zone | null): DemoState {
  if (!z) return state
  const incidents = state.incidents.filter((i) => i.wardId === z.id)
  const ids = new Set(incidents.map((i) => i.id))
  const near = (loc: [number, number]) => km(z.center, loc) <= Math.max(3, z.radiusKm * 2)
  const inZone = (params: Record<string, unknown> | undefined) =>
    !!params && (params.ward_id === z.id || (typeof params.incident_id === "string" && ids.has(params.incident_id)))
  return {
    ...state,
    incidents,
    reports: state.reports.filter((r) => r.wardId === z.id || (r.incidentId != null && ids.has(r.incidentId))),
    resources: state.resources.filter((r) => (r.incidentId && ids.has(r.incidentId)) || near(r.location)),
    needs: state.needs.filter((n) => ids.has(n.incidentId)),
    decisions: state.decisions.filter((d) => d.wardId === z.id || inZone(d.params as Record<string, unknown> | undefined)),
    alerts: state.alerts.filter((a) => a.wardId === z.id),
    facilities: state.facilities.filter((f) => f.wardId === z.id || near(f.location)),
    wards: state.wards.filter((w) => w.id === z.id),
    roadBlocks: state.roadBlocks.filter((b) => near(b.location)),
    routes: state.routes.filter((r) => ids.has(r.incidentId)),
  }
}

/** Operating regions in this deployment. Pune and Ghaziabad are 1,200 km
 *  apart, so a single "city" view of both is two dots on a map of India: the
 *  wall shows one region at a time. Anything is placed in the nearest region. */
export type Region = { id: string; name: string; center: [number, number]; zoom: number }
export const REGIONS: Region[] = [
  { id: "ncr", name: "Ghaziabad · IPEC", center: [77.375, 28.662], zoom: 11.9 },
  { id: "pune", name: "Pune", center: [73.86, 18.55], zoom: 10.4 },
]
const REACH_KM = 150

export function regionOf(loc: [number, number] | null | undefined): Region | null {
  if (!loc || !Number.isFinite(loc[0]) || !Number.isFinite(loc[1])) return null
  let best: Region | null = null
  let d = Infinity
  for (const r of REGIONS) {
    const k = km(r.center, loc)
    if (k < d) { d = k; best = r }
  }
  return d <= REACH_KM ? best : null
}

/** The region where the newest report or incident is: where things are
 *  happening now. Falls back to the region with most open incidents. */
export function autoRegion(state: DemoState): Region {
  let latest: { t: number; loc: [number, number] } | null = null
  for (const x of [...state.reports, ...state.incidents]) {
    const t = Date.parse(x.createdAt) || 0
    if (!latest || t > latest.t) latest = { t, loc: x.location }
  }
  const r = regionOf(latest?.loc)
  if (r) return r
  const counts = REGIONS.map((g) => state.incidents.filter((i) => isOpen(i.status) && regionOf(i.location)?.id === g.id).length)
  return REGIONS[counts.indexOf(Math.max(...counts))] ?? REGIONS[0]
}

/** The world restricted to one region (`null` = everything). */
export function regionState(state: DemoState, region: Region | null): DemoState {
  if (!region) return state
  const inR = (loc: [number, number] | null | undefined) => regionOf(loc)?.id === region.id
  const wards = state.wards.filter((w) => inR(w.centroid))
  const wardIds = new Set(wards.map((w) => w.id))
  const incidents = state.incidents.filter((i) => inR(i.location))
  const ids = new Set(incidents.map((i) => i.id))
  const mine = (params: Record<string, unknown> | undefined, wardId: string | null) =>
    (wardId != null && wardIds.has(wardId)) || (!!params && (
      (typeof params.ward_id === "string" && wardIds.has(params.ward_id)) ||
      (typeof params.incident_id === "string" && ids.has(params.incident_id))))
  return {
    ...state,
    wards,
    incidents,
    reports: state.reports.filter((r) => inR(r.location)),
    resources: state.resources.filter((r) => inR(r.location)),
    needs: state.needs.filter((n) => ids.has(n.incidentId)),
    decisions: state.decisions.filter((d) => mine(d.params as Record<string, unknown> | undefined, d.wardId)),
    alerts: state.alerts.filter((a) => wardIds.has(a.wardId)),
    facilities: state.facilities.filter((f) => inR(f.location)),
    roadBlocks: state.roadBlocks.filter((b) => inR(b.location)),
    routes: state.routes.filter((r) => ids.has(r.incidentId)),
    agencyRequests: state.agencyRequests.filter((a) => wardIds.has(a.wardId)),
    duplicates: state.duplicates.filter((d) => d.wardId == null || wardIds.has(d.wardId)),
    events: state.events.filter((e) => e.wardId == null || wardIds.has(e.wardId)),
    beats: state.beats.filter((b) => {
      const w = b.detail?.wardId
      const i = b.detail?.incidentId
      if (typeof w === "string") return wardIds.has(w)
      if (typeof i === "string") return ids.has(i)
      return true
    }),
  }
}
