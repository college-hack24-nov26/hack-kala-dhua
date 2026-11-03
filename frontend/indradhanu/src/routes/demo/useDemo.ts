import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { request } from "@/api/httpClient"
import { useLiveSync, pollInterval } from "@/hooks/useLiveSync"
import * as localCache from "@/lib/localCache"

const SNAPSHOT_KEY = "demo:snapshot:v1"
const GEOMETRY_KEY = "demo:geometry:v1"

export type Ward = {
  id: string; name: string; number: string; centroid: [number, number]
  boundary: [number, number][] | null; population: number
  score: number | null; severity: number | null; populationAtRisk: number | null
}
export type Resource = {
  id: string; kind: string; label: string; operator: string; agencyId: string | null
  capacity: number; status: string; location: [number, number]
  capabilities: string[]; assignedTo: string | null
  incidentId: string | null; etaMinutes: number | null
  assignmentStatus?: string | null; distanceKm?: number | null
  statusNote?: string | null; unavailableReason?: string | null
  updatedAt?: string | null
}
export type Incident = {
  id: string; title: string; category: string; wardId: string; severity: number
  status: string; reportCount: number; confidence: number; street: string | null
  location: [number, number]; createdAt: string; unitsEnRoute: number
}
export type RouteStep = { instruction: string; street: string; distanceM: number }
export type UnitRoute = {
  id: string; resourceId: string; incidentId: string
  resourceLabel: string; resourceKind: string; incidentTitle: string
  status: string; etaMinutes: number | null; distanceKm: number | null
  engine: string | null; progress: number
  steps: RouteStep[]; path: [number, number][]
}
export type CitizenRoute = {
  path: [number, number][]; headline: string; destination: string | null
  km: number; minutes: number; engine: string; steps: RouteStep[]
  from: [number, number]
}
export type Alert = {
  id: string; wardId: string; wardName: string | null; hazard: string
  severity: number; headline: string; action: string
  safeLocation: { name: string; location: [number, number]; distance_km: number } | null
  channels: string[]; language: string; reach: number
  decisionId: string | null; issuedAt: string | null
}
export type RawReport = {
  id: string; text: string; category: string
  classifiedAs: string | null; classificationConfidence: number | null
  source: string; deviceId: string | null; reporter: string | null
  street: string | null; hasPhoto: boolean
  trust: number | null; trustBreakdown: Record<string, unknown>
  status: string; wardId: string; wardName: string | null
  location: [number, number]; createdAt: string
  incidentId: string | null; incidentTitle: string | null
  incidentSeverity: number | null; incidentReportCount: number | null
  opened: boolean
  linkScore: number | null; linkReason: string | null; linkDecidedBy: string | null
  /** What somebody found out. `null` means nobody has ruled yet, which is not
   *  the same as ruling that it was false.
   *
   *  Named `verdict` rather than `outcome` because the inbox already uses
   *  "outcome" for what the system did with the report — opened, merged, held.
   *  These are different questions and must not share a word. */
  verdict: "confirmed" | "false" | null
  verdictBy: string | null
  verdictAt: string | null
  reporterId: string | null
  /** Wilson lower bound over this reporter's history. `reporterHumanVerdicts`
   *  says how much of it is actual ground truth rather than the trust scorer
   *  agreeing with itself — the two must never be shown as the same number. */
  reporterReliability: number | null
  reporterHumanVerdicts: number | null
  reporterTotal: number | null
  /** Severity read from the words (keywords → classifier → LLM, bounded). */
  assessedSeverity?: number | null
  severityRead?: { severity: number; method: string; reason: string; life_threat?: boolean
                   people_at_risk?: number | null; injection?: boolean; redacted?: string[] } | null
}
export type Need = {
  incidentId: string; capability: string; required: number; met: number
}
export type Decision = {
  id: string; action: string; target: string; wardId: string | null
  rationale: string; confidence: number; status: string
  clause: string | null; delegatedTo: string | null
  withinDelegation: boolean | null; createdAt: string
  /** What the decision would carry out, and to what: `resource_id`,
   *  `incident_id`, `ward_id`, depending on the action. Stored by the gate from
   *  the start so a decision approved twenty minutes later moves the unit it
   *  named — and it never left the database, so the console could say "move a
   *  pump to Kothrud" and not which pump. */
  actionKey?: string | null
  params?: Record<string, unknown>
}
export type DemoEvent = {
  id: number; kind: string; actor: string
  subjectType: string | null; subjectId: string | null
  wardId: string | null; payload: Record<string, unknown>
  text: string; occurredAt: string; causationId: number | null
}
export type Beat = {
  tick: number; at: string; kind: string; text: string
  detail: Record<string, unknown>
}
export type Facility = {
  id: string; name: string; kind: string; kindLabel: string; status: string
  capacity: number | null; occupancy: number | null
  acceptsCasualties: boolean; location: [number, number]
  wardId: string | null
  /** Relief stock on hand: food packets, litres of water, medical kits. */
  supplies: Record<string, number>
  servedPerHour: number | null
}
export type RoadBlock = {
  id: string; reason: string; reportedBy: string; radiusM: number
  location: [number, number]
}
export type BacktestScore = { mae: number; brier: number; top5: number | null }
export type ForecastState = {
  horizonHours: number; generatedAt: string
  incidentsSeen: number; historyHours: number; confidenceNote: string
  error?: string
  /** The same method replayed on this run's history and checked against what
   *  followed, next to two baselines. */
  backtest?: {
    available: boolean; reason?: string; horizonHours?: number; origins?: number
    incidents?: number; wards?: number; verdict?: string
    model?: BacktestScore; persistence?: BacktestScore; uniform?: BacktestScore
  }
  recurrence: {
    wardId: string; wardName: string; category: string
    ratePerHour: number; expected: number; pAtLeastOne: number
    observed: number; observedHours: number; evidence: number
    hazardMultiplier: number; explanation: string
  }[]
  facilities: {
    id: string; name: string; kind: string
    capacity: number | null; occupancy: number | null; status: string
    spare: number | null; arrivalsPerHour: number; expectedArrivals: number
    hoursToFull: number | null; pressure: string
    fromWards: { ward: string; rate: number }[]
    explanation: string
  }[]
  demand: {
    capability: string; expectedUnits: number
    availableNow: number; committedNow: number; shortfall: number
  }[]
}
export type AgencyRequest = {
  id: string; incidentId: string | null; wardId: string; wardName: string | null
  fromAgency: string; fromName: string | null
  toAgency: string; toName: string | null
  capability: string; quantity: number; status: string; note: string | null
  incidentTitle: string | null; incidentSeverity: number | null
  requestedAt: string | null; respondedAt: string | null
  respondedBy: string | null
  /** Migration 038: the other agency's answer and what the agent did. */
  reply?: string | null; replyKind?: string | null; nextStep?: string | null
  replies?: AgencyReply[]; replyDueAt?: string | null
  followupOf?: string | null; units?: string[]
}
export type AgencyReply = {
  at: string; who: "agency" | "agent"; by: string; kind: string; text: string
}
export type AgencyRef = {
  id: string; name: string; kind: string; capabilities: string[]
}
export type Duplicate = {
  kind: string; incidentIds: string[]; wardId: string | null
  agencies: string[]; detail: string; wastedUnits: number
}
export type RawChange = {
  kind: string; resource_id: string; resource_label: string
  incident_id: string | null; incident_title: string; ward_id: string
  from_incident_id: string | null; from_incident_title: string
  eta_minutes: number; reason: string
}
export type Plan = {
  headline: string; engine: string; coverage: number
  assigned: RawChange[]; reassigned: RawChange[]
  released: RawChange[]; kept: RawChange[]
  uncovered: { ward_id?: string; incident_id?: string; capability?: string; reason: string }[]
}
export type DemoState = {
  running: boolean; tick: number; simNow: string | null; error: string | null
  /** Demand pressure the live run keeps on the front-line fleet. */
  pressure?: { utilisation: number; backlog: number; target: number | null }
  /** Set only while showing the copy cached on this browser, before the first
   *  live snapshot has arrived. Never set on live data. */
  cachedAt?: string | null
  citizen: {
    lng: number; lat: number; wardId: string | null
    wardName: string; inside: boolean; note?: string
  }
  wards: Ward[]; resources: Resource[]; incidents: Incident[]
  needs: Need[]; decisions: Decision[]; events: DemoEvent[]
  duplicates: Duplicate[]; facilities: Facility[]; roadBlocks: RoadBlock[]
  routes: UnitRoute[]; alerts: Alert[]; reports: RawReport[]
  citizenRoute: CitizenRoute | null
  forecast: ForecastState | null
  agencyRequests: AgencyRequest[]; agencies: AgencyRef[]
  plan: Plan | null
  beats: Beat[]
}

export const EMPTY_DEMO: DemoState = {
  running: false, tick: 0, simNow: null, error: null,
  citizen: { lng: 73.8989, lat: 18.6773, wardId: null, wardName: "", inside: true },
  wards: [], resources: [], incidents: [], needs: [], decisions: [],
  events: [], duplicates: [], facilities: [], roadBlocks: [],
  routes: [], alerts: [], reports: [], citizenRoute: null, forecast: null,
  agencyRequests: [], agencies: [],
  plan: null, beats: [],
}

/** Polls the one snapshot endpoint.
 *
 *  One request per second, not eight. A map assembled from several concurrent
 *  requests tears, and a tearing map during a live demo reads as a broken
 *  system even when every number in it is correct.
 *
 *  Overlapping responses are dropped rather than queued: if the network hiccups
 *  we want the newest world, not a backlog of stale ones replayed in order.
 *
 *  Ward polygons are asked for once. They are reference data, they were about
 *  nine tenths of the payload, and re-sending forty of them every second was a
 *  large part of why this endpoint took seconds rather than milliseconds. After
 *  the first response the poll asks for `geometry=0` and the boundaries are
 *  merged back in here from a ref.
 */
export function useDemoPoll(pollMs = 1000) {
  // Start from the last world this browser saw, marked as cached, so the wall
  // and the maps draw at once instead of waiting on a cold server.
  const [state, setState] = useState<DemoState>(() => {
    const cached = localCache.peek<DemoState & { savedAt: string }>(SNAPSHOT_KEY)
    return cached ? { ...cached, cachedAt: cached.savedAt } : EMPTY_DEMO
  })
  const [error, setError] = useState<string | null>(null)
  const [latencyMs, setLatencyMs] = useState<number | null>(null)
  const inFlight = useRef(false)
  const seq = useRef(0)
  const geometry = useRef<Map<string, [number, number][] | null>>(new Map())

  const refresh = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    const mine = ++seq.current
    const started = performance.now()
    const withGeometry = geometry.current.size === 0
    try {
      const next = await request<DemoState>(
        `/demo/state?geometry=${withGeometry ? 1 : 0}`
      )
      if (mine !== seq.current) return
      if (withGeometry) {
        for (const w of next.wards) geometry.current.set(w.id, w.boundary)
        void localCache.set(GEOMETRY_KEY, [...geometry.current.entries()])
      } else {
        // A ward we have no boundary for (added since the cache was written):
        // ask for geometry again next poll.
        if (next.wards.some((w) => !geometry.current.has(w.id))) geometry.current.clear()
        next.wards = next.wards.map((w) => ({
          ...w,
          boundary: geometry.current.get(w.id) ?? null,
        }))
      }
      setState(next)
      // A slim copy for the next cold start, once a minute. Without the event
      // log, narration, raw reports and ward outlines (cached separately):
      // cloning the whole world into IndexedDB every few seconds was a real
      // cost on the main thread.
      localCache.setThrottled(SNAPSHOT_KEY, () => ({
        ...next,
        wards: next.wards.map((w) => ({ ...w, boundary: null })),
        events: [], beats: [], reports: [],
        routes: next.routes.map((r) => ({ ...r, path: [], steps: [] })),
        savedAt: new Date().toISOString(),
      }), 60_000)
      setError(null)
      setLatencyMs(Math.round(performance.now() - started))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      inFlight.current = false
    }
  }, [])

  // The console watches everything an officer acts on. `decisions` and
  // `field_tasks` are the two that change because a *person* did something
  // elsewhere, and those are exactly the ones worth not waiting for.
  const { live } = useLiveSync(
    ["ward_risks", "incidents", "decisions", "alerts", "field_tasks", "citizen_reports", "assignments"],
    () => void refresh()
  )

  useEffect(() => {
    let alive = true
    let id: ReturnType<typeof setInterval> | undefined
    // Ward boundaries and the last snapshot from IndexedDB first (milliseconds),
    // so the first request can skip the geometry, which is most of its bytes.
    void (async () => {
      if (geometry.current.size === 0) {
        const geo = await localCache.get<[string, [number, number][] | null][]>(GEOMETRY_KEY)
        if (geo?.length) geometry.current = new Map(geo)
        localCache.release(GEOMETRY_KEY)
      }
      if (seq.current === 0) {
        const cached = await localCache.get<DemoState & { savedAt: string }>(SNAPSHOT_KEY)
        localCache.release(SNAPSHOT_KEY)
        if (alive && cached && seq.current === 0) {
          const geo = geometry.current
          setState({
            ...cached,
            wards: cached.wards.map((w) => ({ ...w, boundary: geo.get(w.id) ?? w.boundary })),
            cachedAt: cached.savedAt,
          })
        }
      }
      if (!alive) return
      void refresh()
      id = setInterval(() => void refresh(), pollInterval(live, pollMs))
    })()
    return () => {
      alive = false
      if (id) clearInterval(id)
    }
  }, [refresh, pollMs, live])

  const act = useCallback(
    async (path: string, body?: unknown) => {
      try {
        const r = await request<unknown>(path, { method: "POST", body: body ?? {} })
        await refresh()
        return r
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
        throw e
      }
    },
    [refresh]
  )

  return { state, error, latencyMs, refresh, act, live }
}

/** Everything that has happened to one thing, newest first.
 *
 *  Both halves are real: `events` is the append-only audit log the agents write
 *  to, `beats` is the narration the runner produces as it paces the world. The
 *  log knows what was decided; the narration knows what it looked like. A hover
 *  card wants both, in one list, in time order.
 */
export function useActivityIndex(state: DemoState) {
  return useMemo(() => {
    const index = new Map<string, { at: string; text: string; kind: string }[]>()
    const push = (id: unknown, entry: { at: string; text: string; kind: string }) => {
      if (typeof id !== "string" || !id) return
      const list = index.get(id)
      if (list) list.push(entry)
      else index.set(id, [entry])
    }

    for (const e of state.events) {
      const entry = { at: e.occurredAt, text: e.text || e.kind, kind: e.kind }
      push(e.subjectId, entry)
      push(e.wardId, entry)
      const p = e.payload ?? {}
      push((p as Record<string, unknown>).resource_id, entry)
      push((p as Record<string, unknown>).to_incident, entry)
      push((p as Record<string, unknown>).from_incident, entry)
    }
    for (const b of state.beats) {
      const entry = { at: b.at, text: b.text, kind: b.kind }
      const d = b.detail ?? {}
      push(d.incidentId, entry)
      push(d.resourceId, entry)
      push(d.wardId, entry)
      push(d.subjectId, entry)
      push(d.fromIncidentId, entry)
    }

    for (const [, list] of index) {
      list.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0))
      // Same line written by both the log and the narration is one line.
      const seen = new Set<string>()
      let n = 0
      for (let i = 0; i < list.length; i++) {
        if (seen.has(list[i].text)) continue
        seen.add(list[i].text)
        list[n++] = list[i]
      }
      list.length = Math.min(n, 6)
    }
    return index
  }, [state.events, state.beats])
}
