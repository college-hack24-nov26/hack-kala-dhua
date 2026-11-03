/** One small IndexedDB database for the ledger and its content store. */
const DB = "indradhanu-ledger"
const VERSION = 1
export const STORES = { blocks: "blocks", media: "media", keys: "keys", meta: "meta" } as const

let opening: Promise<IDBDatabase> | null = null

export function openDB(): Promise<IDBDatabase> {
  if (opening) return opening
  opening = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB, VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORES.blocks)) db.createObjectStore(STORES.blocks, { keyPath: "hash" })
      for (const s of [STORES.media, STORES.keys, STORES.meta]) {
        if (!db.objectStoreNames.contains(s)) db.createObjectStore(s)
      }
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => { opening = null; reject(req.error) }
  })
  return opening
}

function wrap<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((res, rej) => { req.onsuccess = () => res(req.result); req.onerror = () => rej(req.error) })
}

export async function idbGet<T>(store: string, key: IDBValidKey): Promise<T | undefined> {
  const db = await openDB()
  return wrap(db.transaction(store).objectStore(store).get(key)) as Promise<T | undefined>
}

export async function idbPut(store: string, value: unknown, key?: IDBValidKey): Promise<void> {
  const db = await openDB()
  await wrap(db.transaction(store, "readwrite").objectStore(store).put(value, key))
}

export async function idbAll<T>(store: string): Promise<T[]> {
  const db = await openDB()
  return wrap(db.transaction(store).objectStore(store).getAll()) as Promise<T[]>
}

export async function idbClear(store: string): Promise<void> {
  const db = await openDB()
  await wrap(db.transaction(store, "readwrite").objectStore(store).clear())
}

export async function idbCount(store: string): Promise<number> {
  const db = await openDB()
  return wrap(db.transaction(store).objectStore(store).count())
}
