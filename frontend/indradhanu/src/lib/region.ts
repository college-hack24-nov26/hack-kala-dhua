import { useSyncExternalStore } from "react"

/** Which operating region the whole console shows.
 *
 *  Pune and Ghaziabad share one deployment but are 1,200 km apart. Mixing them
 *  put Ghaziabad incidents in Pune's queue, allocation tables and wall. One
 *  choice, made in the header or when a simulation starts, now scopes every
 *  screen that reads the shared world (see DemoProvider). "all" shows both.
 */
export type RegionPick = "pune" | "ncr" | "all"
const KEY = "indradhanu:region:v1"
const listeners = new Set<() => void>()

function read(): RegionPick {
  try {
    const v = localStorage.getItem(KEY)
    if (v === "pune" || v === "ncr" || v === "all") return v
  } catch { /* storage blocked */ }
  return "pune"
}
let current: RegionPick = read()

export function setRegion(r: RegionPick) {
  current = r
  try { localStorage.setItem(KEY, r) } catch { /* storage blocked */ }
  listeners.forEach((l) => l())
}
export function getRegion(): RegionPick {
  return current
}
export function useRegion(): [RegionPick, (r: RegionPick) => void] {
  const r = useSyncExternalStore(
    (cb) => { listeners.add(cb); return () => { listeners.delete(cb) } },
    () => current,
    () => current,
  )
  return [r, setRegion]
}
