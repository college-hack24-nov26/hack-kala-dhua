import { useEffect, useRef, useState } from "react"
import { request } from "@/api/httpClient"

/** LoRa field sensor nodes, as `GET /analytics/*` returns them (snake_case). */

export type Evidence = Partial<Record<
  "gas_mq2" | "gas_mq135" | "heat" | "audio" | "tapping" | "warmth" | "breath" | "pir" |
  "lean" | "tilt_switch" | "shock" | "shaking", number>>

export type Raw = {
  mq2: number | null
  mq135: number | null
  temp_c: number | null
  tilt_deg: number | null
  gyro_dps: number | null
  vib_g: number | null
  mic: number | null
  piezo: number | null
  knocks: number | null
  tilt_sw?: number | null
  pir?: number | null
}

export type Scores = {
  human: number | null
  structural: number | null
  environmental: number | null
  overall: number | null
}

export type NodeKind = "field" | "gas" | "struct" | "rescue"

export type SensorNode = Raw & Scores & {
  id: string
  label: string | null
  kind: NodeKind
  kind_label: string
  kind_detail: string
  channels: string[]
  virtual: boolean
  is_new: boolean
  first_seen: string
  lat: number | null
  lon: number | null
  ward_id: string | null
  gateway_id: string | null
  simulated: boolean
  last_seen: string
  age_s: number
  online: boolean
  readings: number
  lost: number
  rssi: number | null
  snr: number | null
  seq: number | null
  uptime_s: number | null
  observed_at: string | null
  baseline: Partial<Record<keyof Raw, number>>
  evidence: Evidence | null
  flags: string[] | null
}

export type SensorEvent = Raw & Scores & {
  id: number
  node_id: string
  observed_at: string
  flags: string[]
}

export type Peak = { node: string; value: number } | null

export type Overview = {
  nodes: SensorNode[]
  summary: {
    total: number
    online: number
    human: Peak
    structural: Peak
    environmental: Peak
    overall: Peak
    escalated_1h: number
    simulated: number
    real: number
    new: string[]
    by_kind: Record<string, { label: string; total: number; online: number }>
  }
  events: SensorEvent[]
}

export type SeriesPoint = Raw & Scores & { observed_at: string; flags: string[] | null }
export type Series = { node: string; bucket_s: number; points: SeriesPoint[]; evidence: Evidence }

/** Poll `fetcher` every `everyMs`; overlapping polls are skipped, never stacked. */
export function usePoll<T>(fetcher: (() => Promise<T>) | null, everyMs: number, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const busy = useRef(false)
  useEffect(() => {
    if (!fetcher) {
      setData(null)
      return
    }
    let alive = true
    const poll = async () => {
      if (busy.current) return
      busy.current = true
      try {
        const d = await fetcher()
        if (alive) {
          setData(d)
          setError(null)
        }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      } finally {
        busy.current = false
      }
    }
    void poll()
    const id = setInterval(() => void poll(), everyMs)
    return () => {
      alive = false
      clearInterval(id)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return { data, error }
}

export const fetchOverview = (minutes = 30) =>
  request<Overview>(`/analytics/overview?minutes=${minutes}`)

export const fetchSeries = (node: string, minutes: number) =>
  request<Series>(`/analytics/timeseries?node=${encodeURIComponent(node)}&minutes=${minutes}&points=240`)

export type FleetStatus = {
  enabled: boolean
  period_s: number
  nodes: number
  by_kind: Partial<Record<NodeKind, number>>
  episodes: { node: string; code: string; major: boolean }[]
  deployed: { id: string; kind: NodeKind; label: string; reason: string; at: number }[]
  kinds: Record<NodeKind, { label: string; detail: string }>
}

export const fetchFleet = () => request<FleetStatus>("/iot/virtual", { toast: false })

export const setFleet = (enabled: boolean) =>
  request<FleetStatus>("/iot/virtual", {
    method: "POST", body: { enabled },
    toast: { success: enabled ? "Simulated sensor fleet on" : "Simulated sensor fleet off" },
  })

export const deployNode = (kind: NodeKind, region: string, episode?: "f" | "c" | "t") =>
  request<{ id: string; label: string }>("/iot/virtual/deploy", {
    method: "POST", body: { kind, region, episode },
    toast: {
      loading: "Deploying a node…",
      success: (d) => `Deployed ${(d as { id: string; label: string }).id}: ${(d as { label: string }).label}`,
    },
  })

/** Colour per node kind: the ring around a node on the map and its chip. */
export const KIND_STYLE: Record<NodeKind, { color: string; short: string }> = {
  field: { color: "#2a78d6", short: "Field" },
  gas: { color: "#fab219", short: "Gas" },
  struct: { color: "#ec835a", short: "Struct" },
  rescue: { color: "#8b5cf6", short: "Rescue" },
}
