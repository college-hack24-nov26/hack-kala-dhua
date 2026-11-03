import {
  createContext, useCallback, useContext, useMemo, useState, type ReactNode,
} from "react"
import { useActivityIndex, useDemoPoll } from "./useDemo"
import { REGIONS, regionState } from "@/routes/admin/zones"
import { useRegion, type RegionPick } from "@/lib/region"

/** One world, shared by every screen that looks at it.
 *
 *  This used to be a hook called inside the console. React unmounts a route when
 *  you navigate away from it, so switching to the risk board tore the poll down,
 *  threw the snapshot away, and coming back showed the pre-start empty state for
 *  a second before the first response landed. Worse, it meant the other screens
 *  had no access to the live world at all and were still reading fixtures, which
 *  is why they looked empty while reports were visibly arriving next door.
 *
 *  Mounted once, above the admin routes: the poll survives navigation, every
 *  screen reads the same tick, and no screen can disagree with the map about
 *  what is happening.
 */

type Ctx = ReturnType<typeof useDemoPoll> & {
  activity: Map<string, { at: string; text: string; kind: string }[]>
  /** The incident the operator last picked, shared across screens. */
  selected: string | null
  setSelected: (id: string | null) => void
  busy: string | null
  run: (key: string, path: string, body?: unknown) => Promise<unknown>
  /** The operating region every screen is scoped to ("all" = both). */
  region: RegionPick
  setRegion: (r: RegionPick) => void
  /** The unscoped world, for the rare screen that must see every region. */
  world: ReturnType<typeof useDemoPoll>["state"]
}

const DemoContext = createContext<Ctx | null>(null)

export function DemoProvider({ children }: { children: ReactNode }) {
  const poll = useDemoPoll(1000)
  const [region, setRegion] = useRegion()
  // One region at a time: a Pune run never shows Ghaziabad's incidents, units
  // or decisions, and the reverse. Every screen reads this scoped copy.
  const scoped = useMemo(
    () => (region === "all" ? poll.state : regionState(poll.state, REGIONS.find((r) => r.id === region) ?? null)),
    [poll.state, region],
  )
  const activity = useActivityIndex(scoped)
  const [selected, setSelected] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  const { act } = poll
  const run = useCallback(
    async (key: string, path: string, body?: unknown) => {
      setBusy(key)
      try {
        return await act(path, body)
      } finally {
        setBusy(null)
      }
    },
    [act]
  )

  const value = useMemo<Ctx>(
    () => ({ ...poll, state: scoped, world: poll.state, activity, selected, setSelected, busy, run, region, setRegion }),
    [poll, scoped, activity, selected, busy, run, region, setRegion]
  )

  return <DemoContext.Provider value={value}>{children}</DemoContext.Provider>
}

/** Reading the world outside the provider is a bug, not a fallback, so this
 *  says so rather than quietly rendering an empty console. */
export function useDemo(): Ctx {
  const ctx = useContext(DemoContext)
  if (!ctx) throw new Error("useDemo must be used inside <DemoProvider>")
  return ctx
}
