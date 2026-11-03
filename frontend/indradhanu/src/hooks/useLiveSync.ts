import { useEffect, useRef, useState } from "react"
import { supabase, supabaseConfigured } from "@/lib/supabase"

/** Push, with polling underneath it.
 *
 *  Every screen used to converge on its own four-second timer, which meant an
 *  officer approving a decision and the crew seeing it were separated by up to
 *  four seconds of nothing — and during a flood the interesting moments are
 *  exactly the ones where two people are looking at the same thing.
 *
 *  Supabase Realtime carries the Postgres write-ahead log to the browser, so a
 *  row changing in `incidents` reaches every subscriber in tens of
 *  milliseconds. What it does **not** do is guarantee delivery: a websocket on a
 *  congested municipal network drops, and a screen that stopped polling because
 *  it trusted the socket would then sit there confidently showing a stale city.
 *
 *  So this is deliberately not a replacement for the poll. It is a **hint that
 *  something changed**, and the caller's existing `refresh()` remains the only
 *  thing that reads state. The poll stays, slowed down while the channel is
 *  genuinely subscribed and returning to its normal rate the moment it is not.
 *  The failure mode of the socket is therefore "back to how it worked before",
 *  which is the only failure mode worth having in a system somebody evacuates a
 *  street on.
 *
 *  Row-level security is the access boundary and it already says the right
 *  thing: `alerts`, `incidents` and `ward_risks` are readable by `anon`, so the
 *  resident app subscribes without an account; `decisions` and `field_tasks`
 *  are `authenticated` only, so a subscription from the public page receives
 *  nothing rather than being refused loudly. Realtime enforces the same
 *  policies as a query — there is no second place to get this wrong.
 */

/** Tables published to `supabase_realtime`. Naming them here rather than
 *  accepting any string keeps a typo from becoming a channel that silently
 *  never fires. */
export type LiveTable =
  | "ward_risks"
  | "incidents"
  | "decisions"
  | "alerts"
  | "field_tasks"
  | "citizen_reports"
  | "assignments"

type Options = {
  /** Collapse a burst into one refresh. A single agent run writes decisions,
   *  assignments and alerts within the same second; refetching per row would
   *  turn one plan into a dozen round trips. */
  debounceMs?: number
  /** Turn the subscription off without changing the call site. */
  enabled?: boolean
}

export function useLiveSync(
  tables: LiveTable[],
  onChange: () => void,
  { debounceMs = 400, enabled = true }: Options = {}
): { live: boolean } {
  const [live, setLive] = useState(false)
  /** The callback, held in a ref so a caller passing an inline arrow does not
   *  tear down and rebuild the websocket on every render. */
  const cb = useRef(onChange)
  useEffect(() => { cb.current = onChange }, [onChange])

  // Joined into a string so an inline array literal at the call site does not
  // read as a new dependency every render.
  const key = tables.join(",")

  useEffect(() => {
    if (!enabled || !supabaseConfigured || !key) {
      setLive(false)
      return
    }

    let timer: ReturnType<typeof setTimeout> | null = null
    const ping = () => {
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => cb.current(), debounceMs)
    }

    // One channel for however many tables. Channel names must be unique per
    // subscription or the second one silently joins the first.
    const channel = supabase.channel(`live:${key}`)
    for (const table of key.split(",")) {
      channel.on(
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        "postgres_changes" as any,
        { event: "*", schema: "public", table },
        ping
      )
    }

    channel.subscribe((status) => {
      // SUBSCRIBED is the only status that justifies slowing a poll down.
      // CHANNEL_ERROR, TIMED_OUT and CLOSED all mean the screen is on its own
      // again, and the caller reads `live` to decide how often to ask.
      setLive(status === "SUBSCRIBED")
    })

    return () => {
      if (timer) clearTimeout(timer)
      setLive(false)
      void supabase.removeChannel(channel)
    }
  }, [key, enabled, debounceMs])

  return { live }
}

/** How often to poll, given whether push is working.
 *
 *  Not zero when live. A websocket can be connected and still miss a change —
 *  a replication hiccup, a row written by a path that does not touch a
 *  published table — and a slow poll is what makes those self-correct instead
 *  of persisting until someone reloads.
 */
export function pollInterval(live: boolean, base: number): number {
  // Was max(base x 6, 20 s): with the socket up, everything realtime does not
  // carry (unit positions, the event log, reports joining an incident) reached
  // the console up to 20 s late. Push makes the common case instant; the poll
  // keeps the rest within a few seconds.
  return live ? Math.min(Math.max(base * 3, 3000), 8000) : base
}
