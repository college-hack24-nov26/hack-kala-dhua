/** Installing the app, and surviving without signal.
 *
 *  The service worker does the work; this is the page's half of the contract.
 *  It registers the worker on the two routes that need it, tracks how many
 *  reports are waiting in the outbox, and exposes the install prompt so the
 *  citizen and field apps can offer "add to home screen" rather than hoping the
 *  browser offers it on its own.
 *
 *  Deliberately not registered on `/admin`: a control room must never read a
 *  cached world, and offering to install a dashboard on a phone is noise.
 */

import { useCallback, useEffect, useState } from "react"
import { accessToken } from "@/lib/supabase"

/** Ask the worker to empty the outbox, handing it a current access token.
 *
 *  The token matters more than it looks. A queued request carries the
 *  `Authorization` header it was written with, and those expire in about an
 *  hour — so without this, a report made at the start of a blackout is replayed
 *  with a dead token, refused, and (before the worker was fixed) thrown away.
 *  The page is the only side that can get a fresh one, so the page sends it.
 */
export async function drainOutbox(): Promise<void> {
  const worker = navigator.serviceWorker?.controller
  if (!worker) return
  let token: string | null = null
  try {
    token = await accessToken()
  } catch {
    /* Signed out, or Supabase unreachable. The worker replays what it has. */
  }
  worker.postMessage({ type: "drain-outbox", token })
}

type InstallPrompt = Event & {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>
}

let deferred: InstallPrompt | null = null

if (typeof window !== "undefined") {
  window.addEventListener("beforeinstallprompt", (e) => {
    // Chrome fires this instead of showing its own bar once we preventDefault,
    // which is what lets the app ask at a sensible moment rather than on load.
    e.preventDefault()
    deferred = e as InstallPrompt
    window.dispatchEvent(new Event("indradhanu-installable"))
  })
}

export async function registerWorker(): Promise<void> {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return
  // Vite serves the dev bundle unhashed and a worker caching it makes every
  // edit look like it did not apply.
  if (import.meta.env.DEV) return
  try {
    await navigator.serviceWorker.register("/sw.js", { scope: "/" })
  } catch {
    /* An unavailable worker costs offline support, nothing else. */
  }
}

/** How many reports are sitting on this phone waiting for signal. */
export function useOutbox() {
  const [queued, setQueued] = useState(0)
  /** Queued items the server refused, or that aged out. Shown once, then
   *  dismissed: it is news, not a state to sit in. */
  const [dropped, setDropped] = useState<{ reason: string; url: string }[]>([])
  const [online, setOnline] = useState(
    typeof navigator === "undefined" ? true : navigator.onLine
  )

  const count = useCallback(async () => {
    if (typeof indexedDB === "undefined") return
    try {
      const db = await new Promise<IDBDatabase>((resolve, reject) => {
        const open = indexedDB.open("indradhanu-outbox", 1)
        open.onupgradeneeded = () => {
          const d = open.result
          if (!d.objectStoreNames.contains("requests")) {
            d.createObjectStore("requests", { keyPath: "id", autoIncrement: true })
          }
        }
        open.onsuccess = () => resolve(open.result)
        open.onerror = () => reject(open.error)
      })
      const n = await new Promise<number>((resolve) => {
        const tx = db.transaction("requests", "readonly")
        const req = tx.objectStore("requests").count()
        req.onsuccess = () => resolve(req.result || 0)
        req.onerror = () => resolve(0)
      })
      setQueued(n)
    } catch {
      setQueued(0)
    }
  }, [])

  useEffect(() => {
    void count()
    const onOnline = () => {
      setOnline(true)
      void drainOutbox().then(count)
    }
    const onOffline = () => setOnline(false)
    const onMessage = (e: MessageEvent) => {
      if (e.data?.type !== "outbox-sent") return
      // A report the server refused outright is gone, and the person who wrote
      // it is owed that sentence. Silently emptying somebody's outbox is the
      // one failure worse than not sending.
      if (Array.isArray(e.data.dropped) && e.data.dropped.length) {
        setDropped((d) => [...d, ...e.data.dropped])
      }
      void count()
    }
    window.addEventListener("online", onOnline)
    window.addEventListener("offline", onOffline)
    navigator.serviceWorker?.addEventListener("message", onMessage)
    const id = setInterval(() => void count(), 8000)
    return () => {
      window.removeEventListener("online", onOnline)
      window.removeEventListener("offline", onOffline)
      navigator.serviceWorker?.removeEventListener("message", onMessage)
      clearInterval(id)
    }
  }, [count])

  return {
    queued, online, dropped,
    refresh: count,
    clearDropped: useCallback(() => setDropped([]), []),
  }
}

/** The "add to home screen" affordance, when the browser has offered one. */
export function useInstall() {
  const [available, setAvailable] = useState(deferred !== null)
  const [installed, setInstalled] = useState(
    typeof window !== "undefined" &&
      window.matchMedia?.("(display-mode: standalone)").matches
  )

  useEffect(() => {
    const onAvailable = () => setAvailable(true)
    const onInstalled = () => { setInstalled(true); setAvailable(false) }
    window.addEventListener("indradhanu-installable", onAvailable)
    window.addEventListener("appinstalled", onInstalled)
    return () => {
      window.removeEventListener("indradhanu-installable", onAvailable)
      window.removeEventListener("appinstalled", onInstalled)
    }
  }, [])

  const install = useCallback(async () => {
    if (!deferred) return false
    await deferred.prompt()
    const { outcome } = await deferred.userChoice
    deferred = null
    setAvailable(false)
    return outcome === "accepted"
  }, [])

  return { available, installed, install }
}

/** Swap the manifest so the field app installs as its own icon.
 *
 *  One origin, two installable apps. The link tag is rewritten per route rather
 *  than shipping two builds, because a crew and a resident need different home
 *  screen entries and nothing else about the two apps differs at the origin.
 */
export function useManifest(href: string) {
  useEffect(() => {
    const link = document.querySelector<HTMLLinkElement>('link[rel="manifest"]')
    if (!link) return
    const previous = link.href
    link.href = href
    return () => { link.href = previous }
  }, [href])
}
