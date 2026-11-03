import { toast } from "sonner"

import { accessToken } from "@/lib/supabase"

/** The real HTTP client.
 *
 *  `client.ts` has had a `VITE_API_MODE=http` branch since the beginning that
 *  fell back to the mock with a console warning, because this file did not
 *  exist. It exists now.
 *
 *  Every request carries the Supabase access token as a bearer, read fresh from
 *  the session each time rather than captured once: an access token lasts about
 *  an hour and a flood shift does not, so `supabase-js` refreshes it underneath
 *  us and we always send the current one.
 */

export const apiBaseUrl =
  (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8000/api/v1"
// https://disruptionops.onrender.com/
export class ApiError extends Error {
  // Written out rather than as constructor parameter properties: this project
  // builds with `erasableSyntaxOnly`, which forbids the shorthand because it is
  // TypeScript that emits runtime code rather than type syntax that erases.
  readonly status: number
  readonly detail?: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.status = status
    this.detail = detail
    this.name = "ApiError"
  }

  /** The caller is not signed in, or the session expired. */
  get isUnauthorised() {
    return this.status === 401
  }

  /** Signed in, but this role may not do it. Shown as the clause that blocked
   *  them rather than a generic refusal, because the API says which one. */
  get isForbidden() {
    return this.status === 403
  }
}

/** What the response was, apart from its body.
 *
 *  `stale` is the service worker saying "the network failed and this is the
 *  copy I had". That distinction is the entire value of caching here: a map
 *  from four minutes ago is useful, and a map from four minutes ago presented
 *  as current is dangerous, because roads close. The worker has always set the
 *  header; until now nothing read it, so every cached answer was shown as if it
 *  had just arrived. */
export type ResponseMeta = { stale: boolean; status: number }

/** What to say while a request is in flight, and after.
 *
 *  `false` means say nothing, for the calls that fire on their own rather than
 *  because somebody pressed something. */
export type ToastSpec =
  | false
  | {
      loading?: string
      success?: string | ((data: unknown) => string | undefined)
      error?: string | ((error: ApiError) => string)
    }

type RequestOptions = {
  method?: "GET" | "POST" | "PATCH" | "DELETE"
  body?: unknown
  query?: Record<string, string | number | boolean | undefined | null>
  signal?: AbortSignal
  /** Called before the body is returned, for callers that need to know whether
   *  they are looking at live data or the last copy on the phone. */
  onMeta?: (meta: ResponseMeta) => void
  /** Override the announcement. Omit to get the default for this endpoint. */
  toast?: ToastSpec
}

/* ----------------------------------------------------------------- toasts -- */

/** Endpoints that fire without anybody asking, and so must stay quiet.
 *
 *  The citizen route re-solves every time the phone drifts more than 150 m or a
 *  hazard changes — roughly every few seconds while somebody is walking. An
 *  announcement per solve would bury the one toast that matters under a hundred
 *  that do not, and the Copilot renders its own answer in the transcript, so a
 *  toast for it is a second copy of something already on screen. */
const SILENT = [
  /^\/citizen\/guide$/,
  /^\/copilot\//,
  /^\/citizen\/vision\/analyse$/,
]

/** True when the service worker answered from the outbox rather than the
 *  network. Offline, "the city has re-planned around it" is false — the report
 *  is on the phone and nothing in the control room has seen it yet, and a crew
 *  who is told otherwise will act on a coordination that has not happened. */
const queued = (data: unknown) =>
  !!data && typeof data === "object" && (data as { queued?: boolean }).queued === true

const OUTBOX = "Saved on this phone. It goes the moment there is signal."

/** The response carries a report id, so a report actually exists. */
const filed = (data: unknown) =>
  !!data && typeof data === "object" && !!(data as { reportId?: string }).reportId

/** What each endpoint should say. First match wins.
 *
 *  Written out rather than derived from the path because "Done" is not feedback.
 *  An officer who presses Approve wants to be told the decision issued, and a
 *  resident who presses Send wants to be told a human will see it — those are
 *  different promises and a generic success toast makes neither. */
const SAYS: [RegExp, Exclude<ToastSpec, false>][] = [
  [/^\/demo\/start$/,    { loading: "Starting the world…",     success: "The world is running." }],
  [/^\/demo\/stop$/,     { loading: "Stopping…",               success: "Stopped. Nothing further will happen on its own." }],
  [/^\/demo\/reset$/,    { loading: "Resetting the world…",    success: "Back to the opening position. Units are home and available." }],
  [/^\/demo\/replan$/,   { loading: "Re-solving…",             success: "Re-planned against the current picture." }],
  [/\/incidents\/[^/]+\/corroborate$/, {
    loading: "Asking the neighbours…",
    // The numbers are the whole point of the button, so the toast carries them
    // rather than saying "done" over the top of the thing you wanted to see.
    success: (d) => {
      const r = d as {
        before?: { reports?: number }
        after?: { reports?: number; autoConfirmed?: number; severity?: number }
      }
      const from = r.before?.reports ?? 0
      const to = r.after?.reports ?? 0
      const confirmed = r.after?.autoConfirmed ?? 0
      return (
        `${from} report became ${to}` +
        (confirmed > 0
          ? " — independent corroboration cleared the auto-confirm floor."
          : " — still short of auto-confirm.")
      )
    },
  }],
  [/\/decisions\/[^/]+\/approve$/,  { loading: "Approving…",   success: "Approved. The decision is issued and the log has your name on it." }],
  [/\/decisions\/[^/]+\/reject$/,   { loading: "Rejecting…",   success: "Rejected. Nothing was dispatched." }],
  [/\/decisions\/[^/]+\/override$/, { loading: "Overriding…",  success: "Overridden — recorded as an override, not as an approval." }],
  [/^\/agency-requests$/,           { loading: "Sending the request…", success: "Sent. The other agency sees it now." }],
  [/\/agency-requests\/[^/]+\/acknowledge$/, { loading: "Acknowledging…", success: "Acknowledged. They know you have it." }],
  [/\/agency-requests\/[^/]+\/fulfil$/,      { loading: "Confirming…",    success: "Marked fulfilled, and recorded on both sides." }],
  [/\/agency-requests\/[^/]+\/decline$/,     { loading: "Declining…",     success: "Declined, with the reason recorded." }],
  [/^\/runs$/,           { loading: "Scoring the hazard…",     success: "Run complete. The board is current." }],
  [/^\/forecast\/preposition$/, { loading: "Reading the forecast…", success: "The forecast has been asked. Any proposals are in the gate." }],
  [/^\/citizen\/report\/voice$/, {
    loading: "Sending what you said…",
    // The same endpoint does two different things. With `fileIt: false` it only
    // transcribes, so that the person can read what was heard before it becomes
    // a report — and telling them it reached the control room at that point
    // would be false, and the kind of false somebody stops reporting over.
    success: (d) =>
      queued(d)
        ? OUTBOX
        : filed(d)
          ? "Sent. Someone in the control room can see it."
          : "Heard you. Check the words below, then send.",
  }],
  [/^\/citizen\/report$/, {
    loading: "Sending your report…",
    success: (d) => (queued(d) ? OUTBOX : "Sent. Someone in the control room can see it."),
  }],
  [/^\/citizen\/arrived$/,{ loading: "Letting them know…",     success: "Recorded. The shelter count is updated." }],
  [/^\/reports$/,        { loading: "Filing the report…",      success: "Report filed." }],
  [/\/reports\/[^/]+\/verdict$/, { loading: "Recording your verdict…", success: "Recorded. The reporter's history moves with it." }],
  [/^\/field\/status$/, {
    loading: "Updating…",
    success: (d) =>
      queued(d) ? OUTBOX : "Status updated, and the city has re-planned around it.",
  }],
  [/^\/tasks\//,         { loading: "Updating the task…",      success: "Task updated." }],
  [/^\/config\//,        { loading: "Saving…",                 success: "Saved." }],
]

function announce(method: string, path: string, override?: ToastSpec): ToastSpec {
  if (override !== undefined) return override
  if (method === "GET") return false           // polling must never speak
  if (SILENT.some((re) => re.test(path))) return false
  const hit = SAYS.find(([re]) => re.test(path))
  if (hit) return hit[1]
  // Anything mutating that is not listed still announces itself. A new button
  // added tomorrow gets feedback without anybody remembering to add it here,
  // which is the whole reason this lives at the HTTP layer rather than in the
  // fifteen components that press it.
  return method === "DELETE"
    ? { loading: "Deleting…", success: "Deleted." }
    : { loading: "Working…", success: "Done." }
}

/** Sonner needs a stable id to replace the spinner with the result rather than
 *  stacking a second toast beside it. Per endpoint, not per call, so hammering
 *  one button collapses into one line instead of a column of them. */
const toastId = (method: string, path: string) => `${method} ${path}`

function errorText(error: unknown, spec: Exclude<ToastSpec, false>): string {
  if (typeof spec.error === "string") return spec.error
  if (error instanceof ApiError) {
    if (typeof spec.error === "function") return spec.error(error)
    if (error.isUnauthorised) return "Your session has expired. Sign in again."
    if (error.isForbidden) return "Your role is not permitted to do that."
    if (error.status >= 500) return "The server could not complete that."
    return error.message
  }
  // fetch itself failed: no network, DNS, CORS, the server not listening.
  return "Could not reach the server. Check your connection."
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(
    `${apiBaseUrl}${path.startsWith("/") ? path : `/${path}`}`,
    window.location.origin
  )
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v))
    }
  }
  return url.toString()
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = opts.method ?? "GET"
  const spec = announce(method, path, opts.toast)
  if (spec === false) return send<T>(path, opts)

  const id = toastId(method, path)
  if (spec.loading) toast.loading(spec.loading, { id })
  try {
    const data = await send<T>(path, opts)
    const done =
      typeof spec.success === "function" ? spec.success(data) : spec.success
    // A success message of undefined means "this one succeeded quietly" —
    // the spinner still has to go, or it hangs there forever.
    if (done) toast.success(done, { id })
    else toast.dismiss(id)
    return data
  } catch (error) {
    // An aborted request is not a failure, it is a component unmounting or a
    // newer request superseding this one. Telling somebody their report failed
    // because they navigated away would be a lie.
    if (error instanceof DOMException && error.name === "AbortError") {
      toast.dismiss(id)
      throw error
    }
    toast.error(errorText(error, spec), {
      id,
      description:
        error instanceof ApiError && error.status >= 500
          ? "Nothing was changed. It is safe to try again."
          : undefined,
    })
    throw error
  }
}

/** How long a request may hang before it is abandoned.
 *
 *  `fetch` has no timeout of its own. Without one, a request on a congested
 *  venue network — or against a Render instance that has gone cold — stays
 *  pending forever, and *that* is what freezes a screen rather than any error:
 *  every polling screen in this app guards itself with an `inFlight` ref so
 *  polls do not stack, and that ref is only cleared in a `finally`. A promise
 *  that never settles never reaches the `finally`, so the poll stops for good
 *  and the operator is left looking at a city that has quietly stopped
 *  updating. One hung request used to end the demo.
 *
 *  Generous rather than snappy: a cold Render instance legitimately takes tens
 *  of seconds to answer the first call, and killing that would turn a slow
 *  start into a failure.
 */
const TIMEOUT_MS = 25_000
/** The vision call is a GPU on the other side of a tunnel — six seconds warm,
 *  up to a minute on a cold model load. It gets its own budget. */
const SLOW: [RegExp, number][] = [[/^\/citizen\/vision/, 130_000]]

const budgetFor = (path: string) =>
  SLOW.find(([re]) => re.test(path))?.[1] ?? TIMEOUT_MS

/** A stable id for this browser, for the calls nobody signs in to make.
 *
 *  Two jobs, and they want the same value. The trust scorer needs to tell one
 *  resident filing four reports from four angles apart from four residents, and
 *  the rate limiter needs to tell two phones apart when they are behind one
 *  municipal NAT — which, at a demo on venue wifi, is every phone in the room.
 *  Keeping one key for both is deliberate: two ids would drift, and the trust
 *  attribution is the one that must not.
 *
 *  Wrapped in try/catch because private browsing and blocked site data both
 *  throw on access, and a resident in water must still be able to file.
 */
export function deviceId(): string | undefined {
  try {
    const KEY = "disruptionops.device"
    let id = localStorage.getItem(KEY)
    if (!id) {
      id = crypto.randomUUID()
      localStorage.setItem(KEY, id)
    }
    return id
  } catch {
    return undefined
  }
}

async function send<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const token = await accessToken()
  const headers: Record<string, string> = { Accept: "application/json" }
  if (token) headers.Authorization = `Bearer ${token}`
  if (opts.body !== undefined) headers["Content-Type"] = "application/json"
  // Sent on every request so the API can tell anonymous callers apart. Not a
  // credential and never treated as one — it labels a bucket, nothing more.
  const device = deviceId()
  if (device) headers["X-Indradhanu-Device"] = device

  // Our own controller, chained to the caller's signal rather than replacing
  // it, so a component unmounting still cancels and a timeout is still
  // distinguishable from it. `AbortSignal.timeout` is not used: its abort is
  // indistinguishable from an unmount at the catch site, and the two deserve
  // different sentences.
  const ctl = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    ctl.abort()
  }, budgetFor(path))
  const relay = () => ctl.abort()
  opts.signal?.addEventListener("abort", relay, { once: true })

  try {
    const res = await fetch(buildUrl(path, opts.query), {
      method: opts.method ?? "GET",
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: ctl.signal,
    })

    opts.onMeta?.({
      stale: res.headers.get("X-Indradhanu-Stale") === "1",
      status: res.status,
    })

    if (res.status === 204) return undefined as T

    // Inside the same budget as the fetch: headers can arrive promptly and the
    // body still stall, which hangs exactly as hard as no response at all.
    const text = await res.text()
    const payload = text ? safeJson(text) : null

    if (!res.ok) {
      // The API's error handler returns {detail: ...} or {message, clause, ...}.
      // Prefer whatever human sentence it gave us over "Request failed".
      const message =
        pick(payload, "message") ??
        pick(payload, "detail") ??
        `${res.status} ${res.statusText}`
      throw new ApiError(res.status, message, payload)
    }
    return payload as T
  } catch (e) {
    if (timedOut) {
      throw new ApiError(
        408,
        "The server took too long to answer. It may be waking up — try again."
      )
    }
    throw e
  } finally {
    clearTimeout(timer)
    opts.signal?.removeEventListener("abort", relay)
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

function pick(payload: unknown, key: string): string | undefined {
  if (payload && typeof payload === "object" && key in payload) {
    const v = (payload as Record<string, unknown>)[key]
    if (typeof v === "string") return v
  }
  return undefined
}

/* ------------------------------------------------------------------ auth -- */

export type Me = {
  authenticated: boolean
  userId: string | null
  role: "citizen" | "field_operator" | "ward_officer" | "commissioner" | "admin"
  fullName: string
  wardId: string | null
  operator: string | null
  isStaff: boolean
  canEscalate: boolean
  interfaces: string[]
  developmentIdentity: boolean
}

export const fetchMe = () => request<Me>("/auth/me")

/* -------------------------------------------------------------- taxonomy -- */

export type Taxonomy = {
  city: {
    id: string
    name: string
    languages: string[]
    adminUnitSingular: string
    adminUnitPlural: string
  }
  hazards: { id: string; displayName: string; maturity: string | null; sortOrder: number }[]
  capabilities: { id: string; label: string; description: string }[]
  resourceKinds: { id: string; displayName: string; capabilities: string[] }[]
  lifelineKinds: { id: string; displayName: string; sheltersPeople: boolean }[]
  incidentCategories: {
    id: string
    displayName: string
    hazardId: string | null
    dedupRadiusM: number
    dedupWindowMin: number
    baseSeverity: number
    lifeSafety: boolean
  }[]
  agencies: { id: string; name: string; shortName: string; capabilities: string[] }[]
}

/** Fetched once at boot. Nothing in the UI should hard-code a hazard list, a
 *  category list or a resource kind: a second city would invalidate all three. */
export const fetchTaxonomy = (cityId = "pune") =>
  request<Taxonomy>("/taxonomy", { query: { cityId } })

/* ------------------------------------------------------------------ runs -- */

export type RunResult = {
  runId: string
  agentRunId: string
  hazard: string
  engine: string
  mode: string
  wardsScored: number
  decisions: number
  autoIssued: number
  awaitingApproval: number
  assignments: number
  uncovered: number
  alerts: number
  fieldTasks: number
  events: number
  durationMs: number
}

export const startRun = (hazard = "flood", cityId = "pune", simRunId?: string) =>
  request<RunResult>("/runs", {
    method: "POST",
    body: { hazard, cityId, simRunId: simRunId ?? null },
  })

/* ----------------------------------------------------------------- events -- */

export type EventRow = {
  id: number
  cityId: string
  simRunId: string | null
  occurredAt: string
  recordedAt: string
  kind: string
  actor: string
  subjectType: string
  subjectId: string
  wardId: string | null
  payload: Record<string, unknown>
  causationId: number | null
}

export const fetchEvents = (params: {
  simRunId?: string
  subjectType?: string
  subjectId?: string
  limit?: number
} = {}) => request<EventRow[]>("/events", { query: params })

/** Why did this happen? Oldest cause first. */
export const fetchEventChain = (eventId: number) =>
  request<EventRow[]>(`/events/${eventId}/chain`)

/* ------------------------------------------------------- operational reads -- */

export const fetchWards = () => request<unknown[]>("/wards")
export const fetchIncidents = () => request<unknown[]>("/incidents")
export const fetchResources = () => request<unknown[]>("/resources")
export const fetchDecisions = () => request<unknown[]>("/decisions")
export const fetchLatestPlan = () => request<unknown>("/allocation/latest")
export const fetchAlerts = (wardId?: string) =>
  request<unknown[]>("/alerts", { query: { wardId } })
export const fetchTasks = (operator?: string) =>
  request<unknown[]>("/tasks", { query: { operator } })

const DECISION_SAID = {
  approve:  "Approved. The decision is issued and the log has your name on it.",
  reject:   "Rejected. Nothing was dispatched.",
  override: "Overridden — recorded as an override, not as an approval.",
} as const

export const actOnDecision = (
  decisionId: string,
  action: "approve" | "reject" | "override",
  note?: string
) =>
  request<unknown>(`/decisions/${decisionId}`, {
    method: "POST",
    body: { action, note },
    toast: { loading: "Recording your decision…", success: DECISION_SAID[action] },
  })

const TASK_SAID = {
  queued:   "Put back in the queue.",
  accepted: "Accepted. The control room can see you have it.",
  on_site:  "Marked on site.",
  complete: "Closed, with your proof attached.",
} as const

export const updateTask = (
  taskId: string,
  status: "queued" | "accepted" | "on_site" | "complete",
  proofNote?: string
) =>
  request<unknown>(`/tasks/${taskId}`, {
    method: "PATCH",
    body: { status, proofNote },
    // The status is the whole message. "Task updated" tells a crew standing in
    // water nothing they did not already know from pressing the button.
    toast: { loading: "Updating…", success: TASK_SAID[status] },
  })

export const submitReport = (report: {
  wardId: string
  category: string
  location: [number, number]
  note?: string
  photoUrl?: string | null
}) => request<unknown>("/reports", { method: "POST", body: report })

export const fetchSituation = (wardId: string) =>
  request<unknown>(`/situation/${wardId}`)
