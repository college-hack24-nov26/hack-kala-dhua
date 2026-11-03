/** A small persistent key-value cache in IndexedDB, with an in-memory mirror.
 *
 *  Used for the things that make the console slow to *start* rather than slow
 *  to run: ward boundaries (most of the first snapshot's bytes), the last
 *  snapshot of the world (so the wall draws instantly on a cold Render and says
 *  it is cached until the live one lands), and a picture of each map screen (so
 *  a screen shows its last image while Mapbox builds the real one).
 *
 *  Everything is best-effort. Private browsing, blocked storage or a full disk
 *  make every call a quiet no-op; nothing here may break a screen.
 */

const DB = "indradhanu-cache"
const STORE = "kv"
const memory = new Map<string, unknown>()
let opening: Promise<IDBDatabase | null> | null = null

function open(): Promise<IDBDatabase | null> {
  if (opening) return opening
  opening = new Promise((resolve) => {
    try {
      if (typeof indexedDB === "undefined") return resolve(null)
      const req = indexedDB.open(DB, 1)
      req.onupgradeneeded = () => {
        if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE)
      }
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => resolve(null)
      req.onblocked = () => resolve(null)
    } catch {
      resolve(null)
    }
  })
  return opening
}

/** Synchronous read of whatever this tab has already loaded or written. */
export function peek<T>(key: string): T | undefined {
  return memory.get(key) as T | undefined
}

export async function get<T>(key: string): Promise<T | undefined> {
  if (memory.has(key)) return memory.get(key) as T
  const db = await open()
  if (!db) return undefined
  return new Promise((resolve) => {
    try {
      const req = db.transaction(STORE, "readonly").objectStore(STORE).get(key)
      req.onsuccess = () => {
        if (req.result !== undefined) memory.set(key, req.result)
        resolve(req.result as T | undefined)
      }
      req.onerror = () => resolve(undefined)
    } catch {
      resolve(undefined)
    }
  })
}

export async function set(key: string, value: unknown): Promise<void> {
  memory.set(key, value)
  const db = await open()
  if (!db) return
  try {
    db.transaction(STORE, "readwrite").objectStore(STORE).put(value, key)
  } catch {
    /* quota or a closed connection: the in-memory copy still serves this tab */
  }
}

/** `set`, at most once per `everyMs` per key, for values produced every poll.
 *  Takes a function so the value is only built when it will be written, and
 *  keeps no in-memory copy (the caller already holds the live one). */
const lastWrite = new Map<string, number>()
export function setThrottled(key: string, make: () => unknown, everyMs: number): void {
  const now = Date.now()
  if (now - (lastWrite.get(key) ?? 0) < everyMs) return
  lastWrite.set(key, now)
  const value = make()
  memory.delete(key)
  void open().then((db) => {
    if (!db) return
    try {
      db.transaction(STORE, "readwrite").objectStore(STORE).put(value, key)
    } catch {
      /* quota: skip this one */
    }
  })
}

/** Forget a key from the in-memory mirror (the IndexedDB copy stays). */
export function release(key: string): void {
  memory.delete(key)
}
