import { useEffect, useRef, useState } from "react"
import { apiBaseUrl, request } from "@/api/httpClient"
import { accessToken } from "@/lib/supabase"

/** One drone camera frame placed on the map by the image localizer. */
export type DroneFix = {
  id: string
  at: string
  source: "finder" | "upload" | "demo" | "console" | string
  drone: string
  accepted: boolean
  lat: number | null
  lon: number | null
  tile: string | null
  inliers: number | null
  errorPx: number | null
  processingMs: number | null
  swarmTask?: { ok: boolean; incidentId?: string | null; requestId?: string | null; reason?: string | null } | null
  mode: string | null
  reason: string | null
  thumb: string | null
  wardId: string | null
  /** Mapbox reverse geocode, e.g. "Upper Indira Nagar, Bibwewadi, Pune". */
  place: string | null
  address: string | null
}

export function useDroneFixes(everyMs = 3000) {
  const [items, setItems] = useState<DroneFix[]>([])
  const [error, setError] = useState<string | null>(null)
  const busy = useRef(false)
  useEffect(() => {
    let alive = true
    const poll = async () => {
      if (busy.current) return
      busy.current = true
      try {
        const r = await request<{ items: DroneFix[] }>("/drone/recent", { toast: false })
        if (alive) { setItems(r.items ?? []); setError(null) }
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      } finally {
        busy.current = false
      }
    }
    void poll()
    const id = setInterval(() => void poll(), everyMs)
    return () => { alive = false; clearInterval(id) }
  }, [everyMs])
  return { items, error }
}

/** Upload a frame through the API, which forwards it to the localizer. */
export async function localizeFrame(file: File, drone = "drone-1"): Promise<DroneFix> {
  const form = new FormData()
  form.append("query", file)
  form.append("drone", drone)
  const token = await accessToken()
  const res = await fetch(`${apiBaseUrl}/drone/localize`, {
    method: "POST",
    body: form,
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error((body as { detail?: string }).detail ?? `Localizer failed (${res.status})`)
  return body as DroneFix
}
