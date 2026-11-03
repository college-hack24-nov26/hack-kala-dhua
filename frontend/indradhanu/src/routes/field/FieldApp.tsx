import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  CheckCircle2, ExternalLink, Loader2, MapPin, Navigation, Radio, Send, Share2, X,
} from "lucide-react"
import { request } from "@/api/httpClient"
import { LiveMap, type CameraRequest, type MapSelection } from "@/components/map/LiveMap"
import { MapExperience, type Snap } from "@/components/map/experience/MapExperience"
import { useCanHover, useMediaQuery } from "@/components/map/experience/hooks"
import {
  ActionButton, AlertBanner, ChoicePills, ConnectivityPill, DetailRow, FilterControl,
  LegendRow, MapControls, NavPanel, PlaceRow, SectionLabel, TopBar, type FilterOption, type Tone,
} from "@/components/map/experience/parts"
import { ItemDisc } from "@/components/map/experience/ItemDisc"
import {
  facilityItem, incidentItem, kindLabel, unitItem, words, type MapItem,
} from "@/components/map/experience/items"
import {
  MAP, alongLine, compass, externalMapsUrl, formatMetres, metresBetween, placeGroupOf,
  serviceOf, sharePlace,
} from "@/components/map/mapTheme"
import { useOutbox } from "@/lib/pwa"
import { OfflineBar } from "@/components/common/OfflineBar"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { DemoCredentials } from "@/auth/DemoCredentials"
import { useLiveSync, pollInterval } from "@/hooks/useLiveSync"
import { REPORT_STATUS, trustWords } from "@/lib/plain"
import { inBiChat, saveHandoff } from "@/lib/native"

/** The crew's interface.
 *
 *  Its own URL, scoped to one agency. A driver does not need the city's fleet,
 *  and the row level security policy on `field_tasks` refuses it independently
 *  of anything this page does.
 *
 *  The point of this screen is the status buttons. A puncture reported here
 *  takes the vehicle out of the fleet and releases its task back into unmet
 *  need; a hospital declaring itself full stops being somewhere the citizen
 *  agent will send anyone. Each one changes the next plan, which is the
 *  difference between a status board and a system.
 */

type Unit = {
  id: string; kind: string; label: string; operator: string; status: string
  statusNote: string | null; unavailableReason: string | null
  location: [number, number]; capacity: number
  assignedTo: string | null; incidentId: string | null
  etaMinutes: number | null; incidentLocation: [number, number] | null
  /** Response-time model for what is left of the trip: P50 to plan, P90 to promise. */
  etaModel?: { p50: number; p90: number; riskMax: number } | null
  /** The road the crew is meant to drive, not a bearing to the incident. */
  route?: number[][] | null
  routeEngine?: string | null
  steps?: { instruction: string; street: string; distanceM: number }[]
  distanceKm?: number | null
  progress?: number
}
type Facility = {
  id: string; name: string; kind: string; status: string
  capacity: number | null; occupancy: number | null; location: [number, number]
}
type StatusKind = {
  id: string; label: string; makesOffline: boolean
  severity: string; appliesTo: string
}
type FieldState = {
  operator: string | null
  units: Unit[]
  tasks: { id: string; title: string; instruction: string; status: string
           priority: number; wardId: string }[]
  facilities: Facility[]
  recent: { subjectId: string; statusKind: string; note: string
            reportedBy: string; at: string }[]
  /** Undefined on an older backend; the card below simply does not render. */
  myReports?: MyReport[]
  /** Every open incident in the city, so a crew sees what they just reported
   *  land on their own map instead of taking it on faith. */
  incidents?: {
    id: string; title: string; category: string; severity: number
    status: string; wardId: string; reportCount: number
    verification: "confirmed" | "unconfirmed" | "held"
    location: [number, number]
  }[]
}

type Category = { id: string; displayName: string }

/** A hazard this crew filed, and what became of it.
 *
 *  The crew had exactly one chance to learn the fate of a report: the card that
 *  appeared for a few seconds after pressing send. Reload, or drive to the next
 *  street, and "did anyone act on that?" was answered by faith. These rows
 *  carry the consequence rather than the status — which incident it landed on,
 *  how many other reports are on that incident, whether a unit is coming and
 *  how far out. */
type MyReport = {
  id: string
  text: string
  readAs: string
  trust: number | null
  status: string
  outcome: string | null
  at: string
  incidentId: string | null
  incidentTitle: string | null
  incidentStatus: string | null
  incidentSeverity: number | null
  reportCount: number
  unitsOnIt: number
  etaMinutes: number | null
}

/** The live fix, and why there isn't one.
 *
 *  `stale` is the state that did not exist before and matters most: a fix we
 *  still have and still believe, from a watch that has since stopped answering.
 *  Collapsing that into "no GPS" is what sent reports to the depot.
 */
type Gps = {
  state: "locating" | "ok" | "stale" | "denied" | "failed" | "unsupported"
  fix?: { lng: number; lat: number; accuracy: number; at: number }
}

/** Accuracy at or below which a fix names a street. A phone with a real GPS
 *  lock answers in 5 to 20 m; 250 m still puts a report on the right block. */
const PRECISE_FIX_M = 250
/** Accuracy beyond which a fix is a neighbourhood rather than a place, and the
 *  unit's recorded position is the better of two bad options.
 *
 *  This bound replaced a single 120 m gate that was **too strict and, worse, a
 *  hard block**: a crew standing at a hazard with a ±200 m cell fix — which is
 *  what a phone returns for the first few seconds before GPS refines, and
 *  permanently if the person granted Android's "approximate location" — was
 *  refused and sent to file at the depot instead. The depot can be kilometres
 *  away and carries no accuracy figure at all, so trading a known ±200 m for an
 *  unknown error was the wrong way round.
 *
 *  Three bands now: precise, approximate-but-used, and too coarse. The middle
 *  one is the important one, because it is where a real phone actually sits and
 *  the old code had no room for it. */
const COARSE_FIX_M = 2000
/** Seconds after which a fix is too old to file against. A crew in a truck
 *  covers a few hundred metres in a minute. */
const STALE_FIX_S = 120

/** What a filed hazard came back as. */
type Filed = {
  incidentId: string | null; createdIncident: boolean; linked: boolean
  wardName: string; readAsLabel: string; readHow: string
  trust: number; trustStatus: string; summary: string
}

const TONE: Record<string, "default" | "secondary" | "destructive" | "outline"> = {
  info: "secondary", warn: "default", blocking: "destructive",
}

export default function FieldApp() {
  const [state, setState] = useState<FieldState | null>(null)
  const [kinds, setKinds] = useState<StatusKind[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [note, setNote] = useState("")
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [effects, setEffects] = useState<string[]>([])
  // Reporting what is in front of the crew, as opposed to what is wrong with
  // their vehicle. See the `/field/report` route: same intake door as a
  // resident's report, but credited at 0.95 rather than 0.62 because a trained
  // crew standing at the thing is the best evidence this system gets.
  const [cats, setCats] = useState<Category[]>([])
  const [hazardText, setHazardText] = useState("")
  const [hazardCat, setHazardCat] = useState("")
  const [gps, setGps] = useState<Gps>({ state: "locating" })
  /** Bumped by "Try again", which restarts the watch. A permission granted
   *  after the first refusal does not reach a watch that was already running. */
  const [gpsAttempt, setGpsAttempt] = useState(0)
  const [now, setNow] = useState(() => Date.now())
  /** Whether the map rides with the crew. On by default; a drag releases it. */
  const [follow, setFollow] = useState(true)
  /** Bumped to force one re-centre even while `follow` is off. */
  const [recentre, setRecentre] = useState(0)
  const [filed, setFiled] = useState<Filed | null>(null)

  /** `selected` as a ref, read inside `load` without `load` depending on it.
   *
   *  It used to be a dependency, which made this poll re-enter itself: the
   *  first load auto-selects a unit, selecting changes `selected`, `load` gets
   *  a new identity, and both effects below tear down and re-run — an extra
   *  pair of requests and a restarted interval every time the operator so much
   *  as picks a different vehicle, on a three-second poll hitting two endpoints
   *  at once. */
  const selectedRef = useRef<string | null>(null)
  useEffect(() => { selectedRef.current = selected }, [selected])

  /** True while a poll is outstanding. `/field/state` on a cold instance can
   *  take longer than the three-second interval, and without this the requests
   *  stack up faster than they drain. */
  const inFlight = useRef(false)

  const load = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    try {
      const [s, k] = await Promise.all([
        request<FieldState>("/field/state"),
        request<StatusKind[]>("/field/status-kinds"),
      ])
      setState(s); setKinds(k); setError(null)
      if (!selectedRef.current && s.units.length) setSelected(s.units[0].id)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      inFlight.current = false
    }
  }, [])

  // A crew's screen changes because somebody in the control room tasked them.
  // Waiting up to three seconds to find that out is three seconds of a driver
  // sitting still, so the task arrives on the socket and the poll becomes the
  // safety net rather than the mechanism.
  const { live } = useLiveSync(["field_tasks", "incidents", "assignments"], () => void load())

  // One effect, not two: separately, every change of `load` fired an immediate
  // request *and* rebuilt the interval that was about to fire anyway.
  useEffect(() => {
    void load()
    const id = setInterval(() => void load(), pollInterval(live, 3000))
    return () => clearInterval(id)
  }, [load, live])

  // The hazard list comes from the taxonomy, never a hard-coded array: a second
  // city that adds "landslide" must get it on the crew screen without a deploy.
  useEffect(() => {
    void request<{ incidentCategories: Category[] }>("/taxonomy", {
      query: { cityId: "pune" },
    })
      .then((t) => setCats(t.incidentCategories ?? []))
      .catch(() => setCats([]))
  }, [])

  // Where the crew actually is. Their own GPS first — that is the whole point
  // of "report what is in front of me" — and the position of the unit they have
  // selected as the fallback, because a tablet bolted into a truck cab often
  // has no geolocation permission and a crew should not be blocked by that.
  //
  // The first version of this was three lines and wrong in four ways, all of
  // which ended with a hazard filed at the truck's registered depot rather than
  // where the crew was standing, and nothing on screen saying so.
  //
  //  1. **The error handler wiped a good fix.** `watchPosition` calls it on
  //     every failure, including a momentary timeout under a flyover, and
  //     `setMyPos(null)` threw away a perfectly good position from four seconds
  //     ago. One bad moment and the crew silently fell back to the depot.
  //  2. **A ten-second timeout with `enableHighAccuracy`.** A cold GPS fix
  //     routinely takes longer than that, so the very first callback on a phone
  //     that had just been unlocked was usually an error — see (1).
  //  3. **No accuracy gate.** A phone indoors answers from Wi-Fi with a radius
  //     of kilometres. Filing "wall collapsed here" against that is worse than
  //     filing nothing, because it is wrong with confidence.
  //  4. **No way to say any of this.** The card said "Filed at your GPS
  //     position" or "No GPS", with nothing about how good the fix was, how old
  //     it was, or how to fix a denied permission.
  //
  // What follows keeps the last good fix, states its accuracy and age, and only
  // treats a fix as usable for reporting when it is actually good enough to
  // report against.
  useEffect(() => {
    if (!navigator.geolocation) {
      setGps({ state: "unsupported" })
      return
    }
    setGps((g) => (g.fix ? g : { state: "locating" }))
    const id = navigator.geolocation.watchPosition(
      (p) =>
        setGps({
          state: "ok",
          fix: {
            lng: p.coords.longitude,
            lat: p.coords.latitude,
            accuracy: p.coords.accuracy,
            at: Date.now(),
          },
        }),
      (e) =>
        setGps((g) => ({
          // A denied permission is a decision and sticks. A timeout or a lost
          // signal is weather: keep the last fix and let the age counter say
          // how stale it has become.
          state: e.code === e.PERMISSION_DENIED ? "denied" : g.fix ? "stale" : "failed",
          fix: e.code === e.PERMISSION_DENIED ? undefined : g.fix,
        })),
      // 30s, because a cold fix is slow and a timeout used to cost the crew
      // their position. `maximumAge` small: a crew moves.
      { enableHighAccuracy: true, maximumAge: 10_000, timeout: 30_000 }
    )
    return () => navigator.geolocation.clearWatch(id)
  }, [gpsAttempt])

  /** Ages the fix on its own clock, so "42s ago" is the real age rather than
   *  the age it had when something else last re-rendered.
   *
   *  Ten seconds, not one. This re-renders the whole screen including the map's
   *  props, and the staleness gate is a two-minute line — a second of precision
   *  on it buys nothing and costs a redraw a second. It also only runs while
   *  there is a fix to age. */
  const hasFix = Boolean(gps.fix)
  useEffect(() => {
    if (!hasFix) return
    const id = setInterval(() => setNow(Date.now()), 10_000)
    return () => clearInterval(id)
  }, [hasFix])

  const fix = gps.fix ?? null
  const fixAgeS = fix ? Math.max(0, Math.round((now - fix.at) / 1000)) : null
  const fresh = (fixAgeS ?? Infinity) <= STALE_FIX_S
  /** Good enough to file against — which is a lower bar than "good enough to
   *  navigate by", and the two were conflated. The competition is not a perfect
   *  fix, it is the truck's recorded position. */
  const fixUsable = Boolean(fix && fix.accuracy <= COARSE_FIX_M && fresh)
  /** Good enough that nothing needs to be said about it. */
  const fixPrecise = Boolean(fix && fix.accuracy <= PRECISE_FIX_M && fresh)
  const myPos: [number, number] | null = fix ? [fix.lng, fix.lat] : null

  const unit = state?.units.find((u) => u.id === selected) ?? null
  /** The position a report is filed at, and the reason for it. Both, because a
   *  crew has to be able to see that it is about to file against the depot. */
  const reportAt = (fixUsable ? myPos : null) ?? unit?.location ?? null
  const reportSource: "gps" | "unit" | "none" =
    fixUsable && myPos ? "gps" : unit?.location ? "unit" : "none"

  // Inside the BiChat phone app: tell the mesh side which unit this crew is and
  // where its task is, so status updates and guidance carry on offline.
  const unitId = unit?.id ?? null
  const taskLng = unit?.incidentLocation?.[0]
  const taskLat = unit?.incidentLocation?.[1]
  const taskName = unit?.assignedTo ?? undefined
  const fixLng = fix?.lng
  const fixLat = fix?.lat
  useEffect(() => {
    if (!inBiChat()) return
    saveHandoff({
      unitId: unitId ?? undefined,
      navigating: taskLng !== undefined && taskLat !== undefined,
      destName: taskName,
      destKind: taskName ? "task" : undefined,
      destLng: taskLng,
      destLat: taskLat,
      lat: fixLat,
      lng: fixLng,
    })
  }, [unitId, taskLng, taskLat, taskName, fixLng, fixLat])

  /** Live position: while a crew has its unit selected and a usable fix, the
   *  unit's position goes to the control room every 15 s. The server moves the
   *  unit on the map, measures it against its road, redraws the road from where
   *  it is when it leaves it, and replans when a free unit has moved far. */
  const lastSent = useRef(0)
  const fixAcc = fix?.accuracy
  useEffect(() => {
    if (!unitId || !fixUsable || fixLng === undefined || fixLat === undefined) return
    const now = Date.now()
    if (now - lastSent.current < 15_000) return
    lastSent.current = now
    request(`/units/${encodeURIComponent(unitId)}/position`, {
      method: "POST", toast: false,
      body: { lng: fixLng, lat: fixLat, accuracyM: fixAcc ?? null, source: "gps" },
    }).catch(() => { /* next fix retries; the mesh path carries status offline */ })
  }, [unitId, fixUsable, fixLng, fixLat, fixAcc])

  /** File a hazard at the crew's own position, through the same intake door
   *  every other report goes through. */
  async function fileHazard() {
    if (!reportAt) {
      setError("No position yet. Allow location, or pick one of your units.")
      return
    }
    if (!hazardText.trim() && !hazardCat) {
      setError("Say what you can see, or pick a kind from the list.")
      return
    }
    setBusy("hazard")
    try {
      const r = await request<Filed>("/field/report", {
        method: "POST",
        body: {
          lng: reportAt[0], lat: reportAt[1],
          text: hazardText.trim(),
          category: hazardCat || undefined,
          cityId: "pune",
        },
      })
      setFiled(r)
      setHazardText(""); setHazardCat(""); setError(null)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }


  async function declare(subjectType: "resource" | "lifeline", subjectId: string, kind: string) {
    setBusy(kind)
    try {
      const r = await request<{
        effects?: string[]; label?: string; queued?: boolean; message?: string
      }>("/field/status", {
        method: "POST",
        body: {
          subjectType, subjectId, statusKind: kind, note,
          // The crew's own fix when it is good enough, the unit's record
          // position otherwise. A crew standing at a hospital declaring it full
          // is better evidence of where that happened than the row the truck
          // was last written to, which is where this used to send.
          lng: reportAt?.[0], lat: reportAt?.[1],
        },
      })
      // Offline, the service worker answers 202 with `queued` and none of the
      // effects a real status change produces. Reporting "no effects" for
      // something that has not reached the control room yet would be a lie a
      // crew acts on, so it says what actually happened instead.
      if (r.queued) {
        setEffects([
          r.message ??
            "No signal. This is saved on the phone and goes the moment there is.",
          "The control room has not seen it yet, so do not assume anyone is coming.",
        ])
        setNote("")
        return
      }
      setEffects(r.effects ?? [])
      setNote("")
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally { setBusy(null) }
  }

  const resourceKinds = kinds.filter((k) => k.appliesTo === "resource")
  const lifelineKinds = kinds.filter((k) => k.appliesTo === "lifeline")

  // ================================================================== map UI
  //
  // Presentation only. The map is the screen; the status buttons, the hazard
  // report and the unit list live in the sheet (phone), a floating card
  // (tablet) or the context panel (desktop).

  const desktop = useMediaQuery("(min-width: 1024px)")
  const canHover = useCanHover()
  const { online, queued } = useOutbox()
  const [clock, setClock] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setClock(Date.now()), 15_000)
    return () => clearInterval(id)
  }, [])
  const [lastLive, setLastLive] = useState<number | null>(null)
  const [seenState, setSeenState] = useState<FieldState | null>(null)
  if (state !== seenState) {
    // A state object only arrives from a successful poll, so this is "last live".
    setSeenState(state)
    if (state) setLastLive(clock)
  }
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [filter, setFilter] = useState("all")
  const [tab, setTab] = useState<"task" | "report" | "status" | "units">("task")
  const [lightPreset, setLightPreset] = useState<"night" | "dusk" | "day">("day")
  const [camera, setCamera] = useState<CameraRequest | null>(null)
  const cameraSeq = useRef(0)
  const fly = useCallback((req: Omit<CameraRequest, "key">) => {
    cameraSeq.current += 1
    setCamera({ ...req, key: cameraSeq.current })
  }, [])
  const [snapRequest, setSnapRequest] = useState<{ snap: Snap; key: number } | null>(null)
  const snapTo = useCallback((snap: Snap) => setSnapRequest({ snap, key: Date.now() }), [])
  /** Turn-by-turn for the selected unit's task is on. */
  const [navOn, setNavOn] = useState(false)
  const [flash, setFlash] = useState<string | null>(null)
  useEffect(() => {
    if (!flash) return
    const id = setTimeout(() => setFlash(null), 2500)
    return () => clearTimeout(id)
  }, [flash])

  const incidentsForMap = useMemo(() =>
    // Real incidents when the API offers them, the crew's own tasks as the
    // fallback so an older backend still draws something rather than an empty map.
    state?.incidents?.length
      ? state.incidents.map((i) => ({
          id: i.id,
          title: i.verification === "unconfirmed" ? `${i.title} · unconfirmed` : i.title,
          category: i.category,
          severity: i.severity,
          reportCount: i.reportCount,
          location: i.location,
        }))
      : state?.units
          .filter((u) => u.incidentLocation)
          .map((u) => ({
            id: u.incidentId ?? u.id, title: u.assignedTo ?? "Task",
            category: "", severity: 4, reportCount: 1,
            location: u.incidentLocation as [number, number],
          })) ?? [],
  [state])

  const items = useMemo<MapItem[]>(() => [
    ...incidentsForMap.map(incidentItem),
    ...(state?.units ?? []).map(unitItem),
    ...(state?.facilities ?? []).map(facilityItem),
  ], [incidentsForMap, state])

  const filterDef = FIELD_FILTERS.find((f) => f.id === filter) ?? FIELD_FILTERS[0]
  const unitKey = unit ? `unit:${unit.id}` : null
  const taskKey = unit?.incidentId ? `incident:${unit.incidentId}` : null
  const visible = useMemo(() => new Set(
    items
      .filter((i) => i.critical || filterDef.match(i) || i.key === selectedKey || i.key === unitKey || i.key === taskKey)
      .map((i) => i.key)
  ), [items, filterDef, selectedKey, unitKey, taskKey])
  const filterOptions: FilterOption[] = FIELD_FILTERS
    .map((f) => ({ id: f.id, label: f.label, colour: f.colour, count: items.filter(f.match).length }))
    .filter((o) => o.id === "all" || o.count > 0 || o.id === filter)

  const picked = items.find((i) => i.key === selectedKey) ?? null
  const origin: [number, number] | null = myPos ?? unit?.location ?? null

  function select(item: MapItem) {
    setSelectedKey(item.key)
    if (item.kind === "unit") setSelected(item.id)
    setFollow(false)
    fly({ center: item.location })
    snapTo("peek")
  }

  const onMapSelect = (hit: MapSelection | null) => {
    if (!hit) {
      setSelectedKey(null)
      return
    }
    const kind = hit.kind === "resource" ? "unit" : hit.kind
    const item = items.find((i) => i.key === `${kind}:${hit.id}`)
    if (item) select(item)
  }

  // ---- the selected unit's route, and how far to trust it.
  const unitRoute: number[][] | undefined =
    (unit?.route?.length ?? 0) > 1
      ? (unit!.route as number[][])
      : unit?.incidentLocation
        ? [unit.location, unit.incidentLocation]
        : undefined
  const roadRoute = (unit?.route?.length ?? 0) > 1 && unit?.routeEngine !== "straight-line-fallback"
  const routeStatus: "clear" | "uncertain" | undefined = unitRoute ? (roadRoute ? "clear" : "uncertain") : undefined

  /** The control room re-solved this unit's route: said, because a line that
   *  redraws itself under a driver otherwise reads as a glitch. */
  const routeSig = unitRoute
    ? `${unit?.id}:${unitRoute.length}:${unitRoute[unitRoute.length - 1]?.join(",")}`
    : ""
  const [seenRoute, setSeenRoute] = useState(routeSig)
  const [reroutedAt, setReroutedAt] = useState<number | null>(null)
  if (routeSig !== seenRoute) {
    const sameUnit = seenRoute.split(":")[0] === routeSig.split(":")[0]
    setSeenRoute(routeSig)
    if (sameUnit && seenRoute && routeSig) setReroutedAt(clock)
  }
  const rerouted = reroutedAt !== null && clock - reroutedAt < 30_000

  // Where the crew is along it, from their own fix when there is one.
  const along = unitRoute && myPos ? alongLine(unitRoute, myPos) : null
  const onRoute = along !== null && along.off < 150
  const travelledM = onRoute ? along!.along : null
  const totalM = along?.total ?? (unit?.distanceKm ? unit.distanceKm * 1000 : null)
  const remainingM = travelledM !== null && totalM ? Math.max(0, totalM - travelledM) : unit?.distanceKm ? unit.distanceKm * 1000 : null
  const progress = travelledM !== null && totalM ? travelledM / totalM : unit?.progress ?? 0

  // Both below are a few dozen arithmetic operations; recomputed per render.
  const step = (() => {
    const steps = unit?.steps ?? []
    if (!steps.length || travelledM === null) return null
    let acc = 0
    for (let i = 0; i < steps.length; i++) {
      const end = acc + steps[i].distanceM
      if (travelledM < end || i === steps.length - 1) {
        return { index: i, step: steps[i], next: steps[i + 1] ?? null, toNextM: Math.max(0, end - travelledM) }
      }
      acc = end
    }
    return null
  })()

  /** Incidents beside the route ahead of the crew — the things they will meet. */
  const hazardsAhead = (() => {
    if (!unitRoute || unitRoute.length < 2) return []
    const from = travelledM ?? 0
    const out: { tone: Tone; text: string; ahead: number }[] = []
    for (const i of incidentsForMap) {
      if (i.id === unit?.incidentId) continue
      const a = alongLine(unitRoute, i.location)
      if (a.off <= 80 && a.along > from) {
        out.push({
          tone: i.severity >= 5 ? "danger" : "caution", ahead: a.along - from,
          text: `${i.title} ${formatMetres(a.along - from)} ahead, beside the route`,
        })
      }
    }
    return out.sort((a, b) => a.ahead - b.ahead).slice(0, 2)
  })()

  const taskRef = unit?.incidentId ? ` #${unit.incidentId.replace(/[^a-z0-9]/gi, "").slice(-4).toUpperCase()}` : ""

  const share = async (title: string, at: [number, number]) => {
    const r = await sharePlace(title, at[0], at[1])
    if (r === "copied") setFlash("Location copied")
    else if (r === "failed") setFlash("Could not share from this browser")
  }

  const frameRoute = (route: number[][]) => {
    let w = Infinity, s2 = Infinity, e = -Infinity, n = -Infinity
    for (const [x, y] of route) { w = Math.min(w, x); e = Math.max(e, x); s2 = Math.min(s2, y); n = Math.max(n, y) }
    fly({ bounds: [[w, s2], [e, n]], zoom: 16.5 })
  }

  const startNav = () => {
    if (!unitRoute) return
    setNavOn(true)
    setSelectedKey(null)
    setFollow(false)
    frameRoute(unitRoute)
    snapTo("peek")
  }

  // ---- pieces

  const gpsShort =
    fix && fixUsable ? `GPS ±${Math.round(fix.accuracy)} m`
    : fix ? "GPS weak"
    : gps.state === "denied" ? "Location blocked"
    : gps.state === "locating" ? "Finding GPS…"
    : "No GPS"

  const top = navOn && unit && unitRoute ? (
    <NavPanel
      context={<>{unit.label} → {unit.assignedTo ?? "its task"}{taskRef}</>}
      distance={step ? formatMetres(step.toNextM) : remainingM !== null ? formatMetres(remainingM) : null}
      instruction={step ? step.step.instruction : unit.assignedTo ? `to ${unit.assignedTo}` : null}
      then={step?.next ? <>Then: {step.next.instruction}</> : null}
      remaining={
        <>
          {remainingM !== null ? `${formatMetres(remainingM)} left` : ""}
          {unit.etaModel ? ` · ${Math.round(unit.etaModel.p50)} min, 90% within ${Math.round(unit.etaModel.p90)}`
            : unit.etaMinutes ? ` · about ${unit.etaMinutes} min (dispatch estimate)` : ""}
          {travelledM === null && myPos ? " · you are not on this route" : ""}
          {!myPos ? " · from the unit's recorded position" : ""}
        </>
      }
      status={{
        tone: roadRoute ? "ok" : "uncertain",
        text: roadRoute
          ? "Road route from the control room, around every hazard it knows about."
          : (unit.route?.length ?? 0) > 1
            ? "Straight-line estimate: the router was unreachable, so roads and closures are unknown."
            : "No route yet — this is the direct line to the task, not a road.",
      }}
      hazards={hazardsAhead}
      notice={rerouted ? "Route updated by the control room." : null}
      arrived={unit.status === "on_site" ? <>On scene at {unit.assignedTo ?? "the task"}.</> : null}
      onExit={() => setNavOn(false)}
    />
  ) : (
    <TopBar
      title={<>Field · {state?.operator ?? "All agencies"}</>}
      subtitle={
        <span className="flex min-w-0 items-center gap-2">
          <ConnectivityPill online={online} reachable={!/failed to fetch|networkerror|load failed/i.test(error ?? "")} staleSince={null}
                            lastLive={lastLive} queued={queued} now={clock} />
          <span className="shrink-0 text-[11px] text-muted-foreground/80">· {gpsShort}</span>
        </span>
      }
      right={<FilterControl value={filter} options={filterOptions} onChange={(id) => { setFilter(id); if (id !== "all") setSelectedKey(null) }} />}
    />
  )

  const banner = (
    <div className="space-y-2">
      {error && (
        <AlertBanner tone="caution" title="Something did not go through" detail={error} onDismiss={() => setError(null)} />
      )}
      {flash && (
        <div className="mx-auto w-fit rounded-full border border-border bg-card/95 px-3 py-1.5 text-xs">
          {flash}
        </div>
      )}
    </div>
  )

  const controls = (
    <MapControls
      following={follow && Boolean(myPos)}
      hasFix={Boolean(myPos)}
      onLocate={() => {
        // Pressing it always re-centres, and turns following back on.
        setRecentre((n) => n + 1)
        setFollow(true)
        if (!myPos) setGpsAttempt((n) => n + 1)
      }}
      onZoomIn={() => fly({ zoomBy: 1 })}
      onZoomOut={() => fly({ zoomBy: -1 })}
      showZoom={desktop || canHover}
      layers={
        <div className="space-y-3">
          <SectionLabel>Basemap</SectionLabel>
          <ChoicePills
            value={lightPreset}
            onChange={setLightPreset}
            options={[{ id: "night", label: "Night" }, { id: "dusk", label: "Dusk" }, { id: "day", label: "Day" }]}
          />
          <SectionLabel>On this map</SectionLabel>
          <div className="grid gap-1.5">
            <LegendRow colour={MAP.critical} label="Critical incident — always shown, pulses" />
            <LegendRow colour={MAP.sev4} label="Incident, severity 4" />
            <LegendRow colour={MAP.sev3} label="Incident, severity 3 and below" />
            <LegendRow colour={MAP.medical} label="Ambulance, hospital, medical camp" />
            <LegendRow colour={MAP.fire} label="Fire crew" />
            <LegendRow colour={MAP.rescue} label="Rescue team, boat" />
            <LegendRow colour={MAP.logistics} label="Pump, bus, truck, tanker" />
            <LegendRow colour={MAP.shelter} label="Shelter, food, water" />
            <LegendRow colour="#f59e0b" ring label="Ring: unit status (amber en route, green on scene)" />
            <LegendRow colour={MAP.you} label="You" />
          </div>
        </div>
      }
    />
  )

  const unitSummary = unit && (
    <div className="flex items-center gap-3">
      <ItemDisc item={unitItem(unit)} size={40} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-[15px] font-semibold">{unit.label}</div>
        <div className="truncate text-xs text-muted-foreground">
          {words(unit.status)}
          {unit.assignedTo ? ` → ${unit.assignedTo}${taskRef}` : " · no task"}
          {unit.distanceKm ? ` · ${unit.distanceKm.toFixed(1)} km` : ""}
          {unit.etaMinutes ? ` · ~${unit.etaMinutes} min` : ""}
        </div>
      </div>
    </div>
  )

  const peek = (
    <div className="space-y-2.5">
      {navOn && unit && unitRoute ? (
        <div className="flex items-center gap-2">
          <ActionButton tone="danger" onClick={() => setNavOn(false)}>End</ActionButton>
          <ActionButton onClick={() => { setTab("task"); snapTo("half") }} className="flex-1">
            <Navigation className="size-4" /> Directions
          </ActionButton>
          <ActionButton href={externalMapsUrl(unitRoute[unitRoute.length - 1][0], unitRoute[unitRoute.length - 1][1])} className="w-11 px-0">
            <ExternalLink className="size-4" /><span className="sr-only">Open in external maps</span>
          </ActionButton>
        </div>
      ) : picked && picked.kind !== "unit" ? (
        <>
          <div className="flex items-center gap-3">
            <ItemDisc item={picked} size={40} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-[15px] font-semibold">{picked.title}</div>
              <div className="truncate text-xs text-muted-foreground">
                {kindLabel(picked)}
                {origin ? ` · ${formatMetres(metresBetween(origin, picked.location))} ${compass(origin, picked.location)}` : ""}
                {picked.status ? ` · ${words(picked.status)}` : ""}
                {picked.severity ? ` · severity ${picked.severity}` : ""}
              </div>
            </div>
            <button type="button" aria-label="Close" onClick={() => setSelectedKey(null)}
                    className="grid size-8 shrink-0 place-items-center rounded-full text-muted-foreground hover:bg-muted">
              <X className="size-4" />
            </button>
          </div>
          <div className="flex gap-2">
            {picked.key === taskKey && unitRoute ? (
              <ActionButton tone={picked.critical ? "critical" : "primary"} className="flex-1" onClick={startNav}>
                <Navigation className="size-4" /> Navigate
              </ActionButton>
            ) : (
              <ActionButton tone="primary" className="flex-1" href={externalMapsUrl(picked.location[0], picked.location[1])}>
                <ExternalLink className="size-4" /> Open in Maps
              </ActionButton>
            )}
            <ActionButton onClick={() => void share(picked.title, picked.location)} className="w-11 px-0">
              <Share2 className="size-4" /><span className="sr-only">Share location</span>
            </ActionButton>
            {picked.kind === "facility" && (
              <ActionButton onClick={() => { setTab("status"); snapTo("half") }}>Status</ActionButton>
            )}
          </div>
        </>
      ) : unit ? (
        <>
          {unitSummary}
          <div className="flex gap-2">
            {unitRoute ? (
              <ActionButton tone="primary" className="flex-1" onClick={startNav}>
                <Navigation className="size-4" /> Navigate
              </ActionButton>
            ) : (
              <ActionButton className="flex-1" onClick={() => { setTab("status"); snapTo("half") }}>
                <Radio className="size-4" /> Report status
              </ActionButton>
            )}
            <ActionButton onClick={() => { setTab("report"); snapTo("half") }}>
              <MapPin className="size-4" /> Hazard here
            </ActionButton>
          </div>
        </>
      ) : (
        <p className="py-1 text-xs text-muted-foreground">
          {state ? "No units for this operator. Sign in as a field operator." : "Loading your units…"}
        </p>
      )}
    </div>
  )

  const taskTab = (
    <div className="space-y-3">
      {unit && (unit.steps?.length ?? 0) > 0 ? (
        <div className="space-y-2">
          <div className="text-sm font-semibold">{unit.label} to {unit.assignedTo}</div>
          <div className="text-xs text-muted-foreground">
            {unit.distanceKm ? `${unit.distanceKm.toFixed(1)} km` : ""}
            {unit.etaModel ? ` · ${Math.round(unit.etaModel.p50)} min (90% within ${Math.round(unit.etaModel.p90)})`
              : unit.etaMinutes ? ` · about ${unit.etaMinutes} min` : ""}
            {unit.etaModel && unit.etaModel.riskMax >= 0.3 ? ` · flood risk on route ${Math.round(unit.etaModel.riskMax * 100)}%` : ""}
            {typeof unit.progress === "number" ? ` · ${Math.round(unit.progress * 100)}% of the way` : ""}
            {roadRoute
              ? " · routed around every hazard the control room knows about"
              : " · straight-line estimate, the router was unreachable"}
          </div>
          <div className="flex flex-wrap gap-2">
            {!navOn && unitRoute && (
              <ActionButton tone="primary" onClick={startNav}><Navigation className="size-4" /> Navigate</ActionButton>
            )}
            {unitRoute && (
              <ActionButton onClick={() => { setFollow(false); frameRoute(unitRoute) }}>View route</ActionButton>
            )}
            {unit.incidentLocation && (
              <>
                <ActionButton href={externalMapsUrl(unit.incidentLocation[0], unit.incidentLocation[1])}>
                  <ExternalLink className="size-4" /> Open in Maps
                </ActionButton>
                <ActionButton onClick={() => void share(unit.assignedTo ?? "Task", unit.incidentLocation as [number, number])}>
                  <Share2 className="size-4" /> Share
                </ActionButton>
              </>
            )}
          </div>
          <ol className="space-y-1 rounded-2xl bg-muted/70 p-3">
            {unit.steps!.slice(0, 12).map((st, i) => (
              <li key={i} className={`flex gap-2 text-xs ${step && step.index === i ? "font-semibold text-foreground" : step && i < step.index ? "text-muted-foreground/80" : ""}`}>
                <span className="w-14 shrink-0 tabular-nums text-muted-foreground">
                  {st.distanceM >= 1000 ? `${(st.distanceM / 1000).toFixed(1)} km` : `${st.distanceM} m`}
                </span>
                <span>{st.instruction}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : unit ? (
        <div className="space-y-2">
          {unitSummary}
          <p className="text-xs text-muted-foreground">
            {unit.assignedTo
              ? "No street directions for this task yet. The line on the map is the direct line, not a road."
              : "This unit has no task. The control room assigns one; it appears here and on the map."}
          </p>
          {unit.incidentLocation && (
            <div className="flex flex-wrap gap-2">
              <ActionButton tone="primary" onClick={startNav}><Navigation className="size-4" /> Navigate</ActionButton>
              <ActionButton href={externalMapsUrl(unit.incidentLocation[0], unit.incidentLocation[1])}>
                <ExternalLink className="size-4" /> Open in Maps
              </ActionButton>
            </div>
          )}
        </div>
      ) : null}

      {picked && picked.kind !== "unit" && (
        <div className="space-y-1.5 border-t border-border pt-3">
          <SectionLabel>Selected</SectionLabel>
          <DetailRow label="Type">{kindLabel(picked)}</DetailRow>
          <DetailRow label="Distance">
            {origin ? `${formatMetres(metresBetween(origin, picked.location))} ${compass(origin, picked.location)}, straight line${myPos ? "" : ", from the unit"}` : null}
          </DetailRow>
          {picked.kind === "incident" && (() => {
            const i = state?.incidents?.find((x) => x.id === picked.id)
            return i ? (
              <>
                <DetailRow label="Severity">{i.severity}{i.severity >= 5 ? " — critical" : ""}</DetailRow>
                <DetailRow label="State">{words(i.status)} · {i.verification}</DetailRow>
                <DetailRow label="Reports">{i.reportCount}</DetailRow>
              </>
            ) : null
          })()}
          {picked.kind === "facility" && (() => {
            const f = state?.facilities.find((x) => x.id === picked.id)
            return f ? (
              <>
                <DetailRow label="Status">{words(f.status)}</DetailRow>
                <DetailRow label="Places free">{f.capacity ? `${Math.max(0, f.capacity - (f.occupancy ?? 0))} of ${f.capacity}` : null}</DetailRow>
              </>
            ) : null
          })()}
          <DetailRow label="Position">
            <span className="tabular-nums">{picked.location[1].toFixed(5)}, {picked.location[0].toFixed(5)}</span>
          </DetailRow>
        </div>
      )}
    </div>
  )

  const reportTab = (
    <div className="space-y-2.5">
      {/* Report what is in front of you. The most credible reporters in the
          city could see things and had nowhere to put them. */}
      <div className="flex items-center gap-2 text-sm font-semibold"><MapPin className="size-4" /> Report a hazard here</div>
      <div className="text-xs text-muted-foreground">
        <GpsLine
          gps={gps}
          fixAgeS={fixAgeS}
          usable={fixUsable}
          precise={fixPrecise}
          source={reportSource}
          unitLabel={unit?.label}
          onRetry={() => setGpsAttempt((n) => n + 1)}
        />
      </div>
      <Textarea
        value={hazardText}
        onChange={(e) => setHazardText(e.target.value)}
        placeholder="Wall collapsed across the lane, nobody trapped"
        className="min-h-20 text-base"
      />
      <div className="flex flex-wrap gap-1.5">
        {cats.slice(0, 10).map((c) => (
          <button
            key={c.id}
            type="button"
            onClick={() => setHazardCat(hazardCat === c.id ? "" : c.id)}
            className={
              "rounded-full border px-3 py-1.5 text-sm " +
              (hazardCat === c.id ? "border-primary/40 bg-accent text-accent-foreground font-medium" : "border-border")
            }
          >
            {c.displayName}
          </button>
        ))}
      </div>
      <Button size="sm" className="w-full" disabled={busy !== null || !reportAt} onClick={() => void fileHazard()}>
        {busy === "hazard" ? <Loader2 className="mr-1 size-3 animate-spin" /> : <Send className="mr-1 size-3" />}
        File it
      </Button>
      {filed && (
        <div className="space-y-1 rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-2 text-xs">
          <div className="font-medium">
            {filed.readAsLabel} in {filed.wardName}
            {filed.createdIncident
              ? " — new incident on the map"
              : filed.linked
                ? " — merged into an incident already open there"
                : " — held, no incident"}
          </div>
          <div className="text-muted-foreground">
            Trust {filed.trust.toFixed(2)} · {filed.trustStatus.replace(/_/g, " ")}
          </div>
          <div className="text-muted-foreground">{filed.readHow}</div>
        </div>
      )}
    </div>
  )

  const statusTab = (
    <div className="space-y-4">
      {effects.length > 0 && (
        <Alert>
          <CheckCircle2 className="size-4" />
          <AlertDescription className="text-xs">
            {effects.map((e, i) => <div key={i}>{e}</div>)}
          </AlertDescription>
        </Alert>
      )}
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-sm font-semibold"><Radio className="size-4" /> Report status</div>
        <div className="text-xs text-muted-foreground">
          {unit ? `${unit.label} — ${unit.status.replace(/_/g, " ")}` : "Pick a unit"}
          {unit?.unavailableReason && ` (${unit.unavailableReason})`}
        </div>
        <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Anything to add" className="text-sm" />
        <div className="flex flex-wrap gap-1.5">
          {resourceKinds.map((k) => (
            <Button
              key={k.id} size="sm" variant={k.makesOffline ? "destructive" : "outline"}
              className="h-8 text-xs"
              disabled={!unit || busy !== null}
              onClick={() => unit && declare("resource", unit.id, k.id)}
            >
              {busy === k.id ? <Loader2 className="size-3 animate-spin" /> : null}
              {k.label}
            </Button>
          ))}
        </div>
      </div>
      <div className="space-y-2">
        <div className="text-sm font-semibold">Facilities</div>
        <p className="text-xs text-muted-foreground">Declaring one full stops the citizen agent sending anyone there.</p>
        {[...(state?.facilities ?? [])]
          .sort((a, b) => (picked?.key === `facility:${b.id}` ? 1 : 0) - (picked?.key === `facility:${a.id}` ? 1 : 0))
          .slice(0, 6)
          .map((f) => (
            <div key={f.id} className={`rounded-xl border p-2 text-xs ${picked?.key === `facility:${f.id}` ? "border-primary/50" : "border-border"}`}>
              <div className="flex items-center gap-2">
                <button type="button" className="font-medium hover:underline" onClick={() => select(facilityItem(f))}>{f.name}</button>
                <Badge variant={f.status === "full" ? "destructive" : "outline"}>{f.status}</Badge>
                {f.capacity ? (
                  <span className="ml-auto text-muted-foreground">
                    {Math.max(0, f.capacity - (f.occupancy ?? 0))}/{f.capacity} free
                  </span>
                ) : null}
              </div>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {lifelineKinds.map((k) => (
                  <Button key={k.id} size="sm" variant={TONE[k.severity] ?? "outline"}
                          className="h-6 px-2 text-[11px]" disabled={busy !== null}
                          onClick={() => declare("lifeline", f.id, k.id)}>
                    {k.label}
                  </Button>
                ))}
              </div>
            </div>
          ))}
      </div>
    </div>
  )

  const unitsTab = (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <SectionLabel>My units · {state?.units.length ?? 0}</SectionLabel>
        {state?.units.map((u) => (
          <PlaceRow
            key={u.id}
            active={selected === u.id}
            disc={<ItemDisc item={unitItem(u)} />}
            title={u.label}
            subtitle={`${words(u.status)}${u.assignedTo ? ` → ${u.assignedTo}` : ""}`}
            trailing={u.etaMinutes ? `${u.etaMinutes} min` : undefined}
            onClick={() => select(unitItem(u))}
          />
        ))}
        {state?.units.length === 0 && (
          <p className="text-xs text-muted-foreground">
            No units for this operator. Sign in as a field operator, or pass
            <code className="mx-1">?operator=</code>.
          </p>
        )}
      </div>

      {(state?.myReports?.length ?? 0) > 0 && (
        <div className="space-y-2">
          <SectionLabel>What you reported</SectionLabel>
          {state!.myReports!.map((r) => {
            const held = r.status === "quarantined" || r.status === "rejected"
            const closed = r.incidentStatus === "resolved"
            const onMap = r.incidentId ? items.find((i) => i.key === `incident:${r.incidentId}`) : undefined
            return (
              <div
                key={r.id}
                className={
                  "space-y-1.5 rounded-xl border p-2.5 " +
                  (closed ? "border-emerald-500/40 bg-emerald-500/5"
                    : held ? "border-amber-500/40 bg-amber-500/5" : "border-border")
                }
              >
                <p className="line-clamp-2 text-xs">{r.text}</p>
                <div className="flex flex-wrap items-center gap-1.5 text-xs">
                  <Badge variant="outline" className="font-normal">{r.readAs.replace(/_/g, " ")}</Badge>
                  {r.trust !== null && (
                    <span className="text-muted-foreground">
                      trust {trustWords(r.trust)}<span className="tabular-nums"> ({r.trust.toFixed(2)})</span>
                    </span>
                  )}
                  <span className="text-muted-foreground">
                    {new Date(r.at).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false })}
                  </span>
                  {onMap && (
                    <button type="button" className="ml-auto text-sky-400 underline underline-offset-2"
                            onClick={() => select(onMap)}>
                      View on map
                    </button>
                  )}
                </div>
                {held ? (
                  <p className="text-xs">
                    <span className="font-medium">{REPORT_STATUS[r.status]?.label ?? "Held"}.</span>{" "}
                    {REPORT_STATUS[r.status]?.hint ?? "Nothing has been dispatched for it."}{" "}
                    It has not opened an incident, so treat it as not yet acted on.
                  </p>
                ) : r.incidentTitle ? (
                  <div className="space-y-0.5 text-xs">
                    <p>
                      {closed ? "Closed: " : "On "}
                      <span className="font-medium">{r.incidentTitle}</span>
                      {r.incidentSeverity != null && <span className="text-muted-foreground"> · severity {r.incidentSeverity}</span>}
                    </p>
                    <p className="text-muted-foreground">
                      {r.reportCount > 1 ? `${r.reportCount} reports on it` : "the only report on it"}
                      {" · "}
                      {closed
                        ? "worked and closed"
                        : r.unitsOnIt > 0
                          ? `${r.unitsOnIt} unit${r.unitsOnIt === 1 ? "" : "s"} on the way` +
                            (r.etaMinutes != null ? `, ${r.etaMinutes} min out` : "")
                          : "nobody assigned yet"}
                    </p>
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground">Filed, not yet on an incident. The next plan will look at it.</p>
                )}
              </div>
            )
          })}
        </div>
      )}

      <div className="space-y-1">
        <SectionLabel>Unit status changes</SectionLabel>
        {state?.recent.slice(0, 10).map((r, i) => (
          <div key={i} className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{r.subjectId}</span> · {r.statusKind.replace(/_/g, " ")}
            {r.note && ` — ${r.note}`}
          </div>
        ))}
        {state?.recent.length === 0 && <p className="text-xs text-muted-foreground">Nothing reported yet.</p>}
      </div>

      {/* The two crew accounts, on the crew screen: Fire Brigade and PMC
          Drainage see different units, and switching is how anyone sees that. */}
      <DemoCredentials portal="field" title="Demo crew sign-ins" />
      <a href="/login" className="block text-xs text-muted-foreground underline">Sign in</a>
    </div>
  )

  const body = (
    <div className="space-y-4 pb-2 pt-1">
      <OfflineBar manifest="/field.webmanifest" />
      <ChoicePills
        value={tab}
        onChange={setTab}
        options={[
          { id: "task", label: navOn ? "Directions" : "Task" },
          { id: "report", label: "Report" },
          { id: "status", label: "Status" },
          { id: "units", label: "Units" },
        ]}
      />
      {tab === "task" ? taskTab : tab === "report" ? reportTab : tab === "status" ? statusTab : unitsTab}
    </div>
  )

  const selectionRing = picked
    ? { lng: picked.location[0], lat: picked.location[1], colour: picked.colour }
    : null

  const show = <T extends { id: string }>(kind: MapItem["kind"], list: T[] | undefined) =>
    (list ?? []).filter((x) => visible.has(`${kind}:${x.id}`))

  return (
    <MapExperience
      top={top}
      banner={banner}
      controls={controls}
      peek={peek}
      body={body}
      snapRequest={snapRequest}
      panelTitle={
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-base font-semibold">Field</div>
            <div className="text-xs text-muted-foreground">
              {state?.operator ?? "All agencies"} · {state?.units.length ?? 0} unit(s)
            </div>
          </div>
          <a href="/login" className="text-xs text-muted-foreground underline">Sign in</a>
        </div>
      }
      map={(padding) => (
        <LiveMap
          className="h-full w-full"
          basemap="night"
          lightPreset={lightPreset}
          markers="badge"
          cluster
          pulseCritical
          hoverCards={canHover}
          resources={show("unit", state?.units).map((u) => ({ ...u, capabilities: [] }))}
          facilities={show("facility", state?.facilities)}
          incidents={show("incident", incidentsForMap)}
          routes={
            state?.units
              .filter((u) => (u.route?.length ?? 0) > 1 && u.id !== unit?.id)
              .map((u) => ({
                id: u.id, resourceId: u.id, resourceLabel: u.label,
                incidentId: u.incidentId ?? u.id,
                incidentTitle: u.assignedTo ?? "Task", status: u.status,
                etaMinutes: u.etaMinutes ?? null,
                distanceKm: u.distanceKm ?? null,
                engine: u.routeEngine, progress: u.progress,
                steps: u.steps, path: u.route as [number, number][],
              })) ?? []
          }
          route={unitRoute}
          routeLabel={unit ? `${unit.label} to ${unit.assignedTo ?? "its task"}` : undefined}
          routeStatus={routeStatus}
          routeProgress={progress}
          // The crew on their own map, with how sure the GPS is.
          me={myPos ? { lng: myPos[0], lat: myPos[1], label: "You", accuracyM: fix?.accuracy ?? null } : null}
          // Locked to the crew by default, released the moment they drag, and
          // taken back by the locate button.
          followMe={follow}
          recentreKey={recentre}
          onUserMove={() => setFollow(false)}
          center={myPos ?? unit?.location ?? [73.88, 18.58]}
          zoom={13.2}
          onSelect={onMapSelect}
          selection={selectionRing}
          camera={camera}
          padding={padding}
        />
      )}
    />
  )
}

/** The crew map's filters: real kinds on this map only. */
const FIELD_FILTERS: { id: string; label: string; colour?: string; match: (i: MapItem) => boolean }[] = [
  { id: "all", label: "All", match: () => true },
  { id: "critical", label: "Critical", colour: MAP.critical, match: (i) => i.critical },
  { id: "incidents", label: "Incidents", colour: MAP.sev4, match: (i) => i.kind === "incident" },
  { id: "units", label: "My units", colour: MAP.logistics, match: (i) => i.kind === "unit" },
  {
    id: "medical", label: "Medical", colour: MAP.medical,
    match: (i) => (i.kind === "facility" && placeGroupOf(i.sub) === "medical") ||
      (i.kind === "unit" && serviceOf(i.sub) === "medical"),
  },
  { id: "shelter", label: "Shelter & relief", colour: MAP.shelter, match: (i) => i.kind === "facility" && placeGroupOf(i.sub) === "shelter" },
  { id: "infra", label: "Infrastructure", colour: MAP.infra, match: (i) => i.kind === "facility" && placeGroupOf(i.sub) === "infra" },
]

/** What the GPS is doing, in a sentence a driver can act on.
 *
 *  The rule this follows: never claim a position the fix does not support, and
 *  never make the crew guess which position a report is about to carry. Every
 *  branch below names the place the report will be filed, because that is the
 *  only fact on this card that changes what lands in the control room.
 */
function GpsLine({
  gps, fixAgeS, usable, precise, source, unitLabel, onRetry,
}: {
  gps: Gps
  fixAgeS: number | null
  usable: boolean
  precise: boolean
  source: "gps" | "unit" | "none"
  unitLabel?: string
  onRetry: () => void
}) {
  const fix = gps.fix
  const age =
    fixAgeS === null
      ? ""
      : fixAgeS < 15
        ? "just now"
        : fixAgeS < 90
          ? `${Math.round(fixAgeS / 10) * 10}s ago`
          : `${Math.round(fixAgeS / 60)} min ago`

  const fallback =
    source === "unit" && unitLabel
      ? ` Reports will be filed at ${unitLabel}'s recorded position instead.`
      : source === "none"
        ? " Nothing will file until there is a position — allow location, or pick one of your units."
        : ""

  const retry = (
    <button type="button" onClick={onRetry} className="underline underline-offset-2">
      Get a better fix
    </button>
  )

  if (gps.state === "unsupported") {
    return <span>This device has no location service.{fallback}</span>
  }
  if (gps.state === "denied") {
    return (
      <span>
        Location is blocked for this site. Allow it in the address bar, then{" "}
        {retry}.{fallback}
      </span>
    )
  }
  if (!fix) {
    return (
      <span>
        {gps.state === "locating"
          ? "Finding you — a first fix outdoors takes a few seconds."
          : "No position yet."}
        {gps.state === "failed" && <> {retry}.</>}
        {fallback}
      </span>
    )
  }

  const accuracy = `±${Math.round(fix.accuracy)} m`
  const coords = `${fix.lat.toFixed(5)}, ${fix.lng.toFixed(5)}`

  // Precise: say where, and nothing else. A line that explains a fix which is
  // working is noise.
  if (precise) {
    return (
      <span>
        Filed at where you are standing — {coords}, {accuracy}, {age}.
      </span>
    )
  }

  // Usable but coarse. **This is the band a real phone sits in**, and the old
  // code had none: it refused the fix and sent the report to the depot. The
  // fix is used, the figure is stated, and the offer to improve it is there
  // without being a precondition.
  if (usable) {
    return (
      <span>
        Filed at where you are standing, to {accuracy} — {coords}, {age}. Good
        enough for the block, not the doorway.{" "}
        {fix.accuracy > PRECISE_FIX_M ? (
          <>
            {retry} for a GPS-grade fix, or step outside.
          </>
        ) : null}
      </span>
    )
  }

  // Genuinely useless, or stale.
  return (
    <span>
      {fix.accuracy > COARSE_FIX_M
        ? `Your device can only place you to ${accuracy}, which is a neighbourhood rather than a place.`
        : `Your last fix is ${age} and too old to file against.`}{" "}
      {coords}. {retry}.{fallback}
    </span>
  )
}
