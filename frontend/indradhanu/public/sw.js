/* Indradhanu service worker.
 *
 * This exists for one situation: somebody opens the app during a flood, on a
 * phone, on a network that is congested or gone. What it has to do in that
 * situation is not "be fast" — it is **not lose the report**.
 *
 * Three behaviours, in order of how much they matter:
 *
 *   1. **A report survives having no signal.** A failed POST of a report is
 *      written to IndexedDB and replayed when connectivity returns, through
 *      Background Sync where the browser supports it and on the next load where
 *      it does not. The report carries its own `occurredAt`, and the intake
 *      pipeline already accepts one, so a report that syncs an hour late is
 *      still scored against the moment it was made rather than the moment it
 *      arrived.
 *   2. **The last known world is readable offline.** Guidance and surroundings
 *      are cached as they are fetched, and served stale with a header saying so
 *      when the network fails. Stale information clearly marked as stale beats a
 *      spinner.
 *   3. **The shell loads.** Standard precache, so the app opens on a dead
 *      network instead of showing the browser's offline page.
 *
 * Deliberately *not* cached: anything under /api/v1/demo or /config. A control
 * room screen must never show a stale world silently, and a configuration
 * screen must never write against a stale one.
 */

const VERSION = "indradhanu-v1"
const SHELL = `${VERSION}-shell`
const DATA = `${VERSION}-data`
const MAPBOX = `${VERSION}-mapbox`

/* Mapbox's style JSON, sprites and font glyphs: the same few hundred files on
 * every visit, and on a cold load they are what the map waits for before it can
 * draw anything. Served from cache immediately and refreshed in the background.
 * Vector tiles are left to Mapbox GL's own tile cache and the HTTP cache, which
 * honour Mapbox's cache headers. */
const MAPBOX_STATIC = [/^\/styles\/v1\//, /^\/fonts\/v1\//, /\/sprite(@2x)?\.(json|png)$/]
const MAX_MAPBOX_ENTRIES = 600

/* Enough to boot. Vite's hashed assets are picked up by the runtime cache
 * below rather than listed here, because their names change every build. */
const SHELL_URLS = ["/", "/citizen", "/field", "/manifest.webmanifest"]

/* How many runtime entries the shell cache may hold on top of SHELL_URLS.
 *
 * Vite's asset names are content-hashed, so every deploy produces a new URL,
 * which `cacheFirst` stores and nothing ever removed: `activate` only deletes
 * caches whose name does not start with VERSION, and VERSION is a constant
 * nobody remembers to bump. A phone that has visited across ten deploys was
 * keeping ten JS bundles, ten stylesheets and ten copies of the font — growth
 * with no ceiling, on exactly the cheap handset this app is meant for.
 *
 * A ceiling is the right fix rather than a version bump, because it needs no
 * discipline at deploy time. One build's assets are a handful of files, so this
 * holds the current build comfortably and lets the one before it age out.
 * Entries are evicted oldest-first: `cache.keys()` returns insertion order. */
const MAX_SHELL_ENTRIES = 24

/* Requests whose responses are worth keeping to show offline. */
const CACHEABLE_GET = [
  "/api/v1/citizen/state",
  "/api/v1/field/state",
  "/api/v1/field/status-kinds",
  "/api/v1/taxonomy",
]

/* Posts that must not be lost. */
const QUEUEABLE_POST = [
  "/api/v1/citizen/report",
  "/api/v1/citizen/report/voice",
  "/api/v1/field/status",
]

// --------------------------------------------------------------- lifecycle --
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL)
      .then((cache) => cache.addAll(SHELL_URLS))
      // A shell URL that 404s during install would otherwise abort the whole
      // installation and leave the app with no worker at all.
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  )
})

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k))
        )
      )
      .then(() => self.clients.claim())
  )
})

// -------------------------------------------------------------------- auth --
/* The freshest access token a page has handed us.
 *
 *  This is what makes the queue survive a long outage rather than quietly
 *  emptying itself into the bin. A queued request carries the `Authorization`
 *  header it was made with, and Supabase access tokens last about an hour — so
 *  a report written at the start of a blackout and replayed ninety minutes
 *  later goes out with a token the API correctly refuses. The old `drain` read
 *  that 401 as "the server has seen it and refused it" and deleted the report.
 *
 *  A report lost because a token expired while the phone had no signal is the
 *  precise failure this whole file exists to prevent, so: the page sends its
 *  current token whenever it asks for a drain, and the replay uses that instead
 *  of the stale one. The worker never stores it anywhere; it lives in this
 *  variable for as long as the worker is alive and is gone when it is not. */
let freshToken = null

/* How a replay response is treated.
 *
 *  The old rule was "2xx or any 4xx means done". That is right for the refusals
 *  that are about the *report* — a malformed body or a ward that does not exist
 *  will be just as wrong on the tenth attempt — and wrong for the ones that are
 *  about the *moment*: an expired token, a timeout, a rate limit. Those say
 *  nothing about whether the report is worth keeping.
 */
function verdictFor(status) {
  if (status >= 200 && status < 300) return "sent"
  // 401 expired credentials, 408 timeout, 429 slow down, 5xx server trouble.
  if (status === 401 || status === 408 || status === 429 || status >= 500) return "retry"
  return "refused"
}

/* A queued request is given up on eventually, but on its own terms rather than
 * on the first refusal: enough attempts to outlast a bad hour, and an age cap
 * so a phone that was off for three days does not replay a flood that is over.
 */
const MAX_ATTEMPTS = 12
const MAX_AGE_MS = 24 * 60 * 60 * 1000

// ------------------------------------------------------------------- queue --
/* A tiny IndexedDB queue. No library: this file must work before anything else
 * on the page has loaded, and a dependency here is a dependency that can fail
 * exactly when it is needed. */
const DB_NAME = "indradhanu-outbox"
const STORE = "requests"

function openDb() {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open(DB_NAME, 1)
    open.onupgradeneeded = () => {
      const db = open.result
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: "id", autoIncrement: true })
      }
    }
    open.onsuccess = () => resolve(open.result)
    open.onerror = () => reject(open.error)
  })
}

async function enqueue(entry) {
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite")
    tx.objectStore(STORE).add(entry)
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
  })
}

async function drain() {
  const db = await openDb()
  const all = await new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly")
    const req = tx.objectStore(STORE).getAll()
    req.onsuccess = () => resolve(req.result || [])
    req.onerror = () => reject(req.error)
  })

  let sent = 0
  const dropped = []

  for (const entry of all) {
    // Too old to be worth sending. A report about water on a road three days
    // ago helps nobody and would be scored against a flood that has ended.
    const age = Date.now() - Date.parse(entry.queuedAt || 0)
    if (Number.isFinite(age) && age > MAX_AGE_MS) {
      await remove(db, entry.id)
      dropped.push({ reason: "too old", url: entry.url })
      continue
    }

    // Replay with the current token rather than the one it was written with.
    const headers = { ...entry.headers }
    if (freshToken && headers.Authorization) {
      headers.Authorization = `Bearer ${freshToken}`
    }

    let response
    try {
      response = await fetch(entry.url, {
        method: entry.method, headers, body: entry.body,
      })
    } catch {
      // Still offline. Leave this and everything after it for the next attempt.
      break
    }

    const verdict = verdictFor(response.status)
    if (verdict === "sent") {
      await remove(db, entry.id)
      sent += 1
      continue
    }
    if (verdict === "refused") {
      // The server saw it and will not take it, and would not on the tenth try.
      // Dropped, but *said out loud*: a report that silently disappeared is the
      // one thing worse than one that failed loudly.
      await remove(db, entry.id)
      dropped.push({ reason: `refused (${response.status})`, url: entry.url })
      continue
    }

    // Retryable. Count the attempt, and give up only after a long time trying.
    const attempts = (entry.attempts || 0) + 1
    if (attempts >= MAX_ATTEMPTS) {
      await remove(db, entry.id)
      dropped.push({ reason: `gave up after ${attempts} attempts`, url: entry.url })
      continue
    }
    await update(db, { ...entry, attempts, lastStatus: response.status })
    // A 401 will hit every remaining entry the same way; stop rather than
    // burning the whole queue's attempt budget on one expired token.
    if (response.status === 401) break
  }

  if (sent > 0 || dropped.length) {
    const clients = await self.clients.matchAll({ includeUncontrolled: true })
    for (const client of clients) {
      client.postMessage({ type: "outbox-sent", count: sent, dropped })
    }
  }
  return sent
}

function update(db, entry) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite")
    tx.objectStore(STORE).put(entry)
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
  })
}

function remove(db, id) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite")
    tx.objectStore(STORE).delete(id)
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
  })
}

async function queued() {
  const db = await openDb()
  return new Promise((resolve) => {
    const tx = db.transaction(STORE, "readonly")
    const req = tx.objectStore(STORE).count()
    req.onsuccess = () => resolve(req.result || 0)
    req.onerror = () => resolve(0)
  })
}

// ------------------------------------------------------------------- fetch --
self.addEventListener("fetch", (event) => {
  const { request } = event
  const url = new URL(request.url)
  const sameOrigin = url.origin === self.location.origin

  if (request.method === "POST" && QUEUEABLE_POST.some((p) => url.pathname.endsWith(p))) {
    event.respondWith(postOrQueue(request))
    return
  }

  if (request.method !== "GET") return

  if (url.hostname === "api.mapbox.com" && MAPBOX_STATIC.some((re) => re.test(url.pathname))) {
    event.respondWith(staleWhileRevalidate(request))
    return
  }

  // Never serve a control room or configuration screen from cache.
  if (url.pathname.includes("/demo/") || url.pathname.includes("/config/")) return

  if (CACHEABLE_GET.some((p) => url.pathname.endsWith(p))) {
    event.respondWith(networkFirst(request))
    return
  }

  // Navigations: the app shell, so a cold start on a dead network still opens.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match("/citizen").then((r) => r || caches.match("/"))
      )
    )
    return
  }

  if (sameOrigin) event.respondWith(cacheFirst(request))
})

async function postOrQueue(request) {
  const clone = request.clone()
  try {
    return await fetch(request)
  } catch {
    const body = await clone.text()
    const headers = {}
    clone.headers.forEach((v, k) => { headers[k] = v })
    await enqueue({
      url: clone.url, method: clone.method, headers, body,
      queuedAt: new Date().toISOString(),
    })
    if ("sync" in self.registration) {
      try { await self.registration.sync.register("indradhanu-outbox") } catch { /* */ }
    }
    const n = await queued()
    // 202: accepted, not done. The client shows "saved, will send when you have
    // signal" rather than a success it cannot honour.
    return new Response(
      JSON.stringify({
        queued: true,
        queueLength: n,
        message:
          "No signal. This is saved on your phone and will be sent the moment " +
          "you are back online.",
      }),
      { status: 202, headers: { "Content-Type": "application/json" } }
    )
  }
}

async function networkFirst(request) {
  try {
    const response = await fetch(request)
    if (response.ok) {
      const cache = await caches.open(DATA)
      cache.put(request, response.clone())
    }
    return response
  } catch {
    const cached = await caches.match(request)
    if (!cached) throw new Error("offline and nothing cached")
    // Marked, so the interface can say how old this is rather than presenting
    // it as current.
    const headers = new Headers(cached.headers)
    headers.set("X-Indradhanu-Stale", "1")
    return new Response(cached.body, {
      status: cached.status, statusText: cached.statusText, headers,
    })
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(MAPBOX)
  const cached = await cache.match(request)
  const refresh = fetch(request)
    .then(async (response) => {
      if (response.ok) {
        await cache.put(request, response.clone())
        const keys = await cache.keys()
        for (let i = 0; i < keys.length - MAX_MAPBOX_ENTRIES; i++) await cache.delete(keys[i])
      }
      return response
    })
    .catch(() => cached)
  return cached || refresh
}

async function cacheFirst(request) {
  const cached = await caches.match(request)
  if (cached) return cached
  const response = await fetch(request)
  if (response.ok && new URL(request.url).origin === self.location.origin) {
    const cache = await caches.open(SHELL)
    await cache.put(request, response.clone())
    await trimShell(cache)
  }
  return response
}

/** Keep the shell cache bounded, oldest runtime entry first.
 *
 *  The four SHELL_URLS are pinned: they are what makes a cold start on a dead
 *  network open at all, and evicting one to make room for a stylesheet would
 *  trade the whole offline story for nothing. */
async function trimShell(cache) {
  const keys = await cache.keys()
  const pinned = new Set(SHELL_URLS.map((u) => new URL(u, self.location.origin).href))
  const evictable = keys.filter((r) => !pinned.has(r.url))
  const excess = evictable.length - MAX_SHELL_ENTRIES
  for (let i = 0; i < excess; i++) await cache.delete(evictable[i])
}

// -------------------------------------------------------------------- sync --
self.addEventListener("sync", (event) => {
  if (event.tag === "indradhanu-outbox") event.waitUntil(drain())
})

self.addEventListener("message", (event) => {
  // The page knows the current access token; this worker does not and must not
  // persist one. Taking it on the way in is what lets a replay authenticate
  // after the token the request was written with has expired.
  if (event.data?.token) freshToken = event.data.token
  if (event.data?.type === "drain-outbox") event.waitUntil(drain())
  if (event.data?.type === "skip-waiting") self.skipWaiting()
})
