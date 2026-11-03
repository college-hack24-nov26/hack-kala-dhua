/** The map's visual language, shared with the BiChat Android map.
 *
 *  The Android client (`ui/map/MapStyle.kt` in bitchat-android) uses the same
 *  hue for the same meaning, so a crew moving between the phone app and this
 *  PWA reads one map, not two:
 *
 *    * red is reserved for life safety — critical incidents here, SOS there —
 *      and only those pulse;
 *    * amber-to-orange is a reported hazard, getting warmer with severity;
 *    * each responder service keeps one hue; the glyph says what it is;
 *    * "you" is always the blue dot.
 *
 *  Colours are plain hex because Mapbox paint expressions do not parse oklch.
 */

export const MAP = {
  critical: "#ef4444",
  sev4: "#f59e0b",
  sev3: "#eab308",
  you: "#0a84ff",
  route: "#22c55e",
  caution: "#f59e0b",
  uncertain: "#f59e0b",
  // responder services
  medical: "#0ea5e9",
  fire: "#f97316",
  rescue: "#a855f7",
  logistics: "#64748b",
  // places
  shelter: "#10b981",
  infra: "#64748b",
  block: "#dc2626",
} as const

/** Severity 5 is critical: never clustered away, always pulsing. */
export const CRITICAL_SEVERITY = 5

export const incidentColour = (severity: number) =>
  severity >= CRITICAL_SEVERITY ? MAP.critical : severity >= 4 ? MAP.sev4 : MAP.sev3

export type Service = "medical" | "fire" | "rescue" | "logistics"

export const serviceOf = (kind: string): Service =>
  kind === "ambulance" ? "medical"
  : kind === "fire_engine" ? "fire"
  : kind === "rescue_team" || kind === "boat" ? "rescue"
  : "logistics"

export const SERVICE_LABEL: Record<Service, string> = {
  medical: "Ambulance",
  fire: "Fire",
  rescue: "Rescue",
  logistics: "Logistics",
}

export const serviceColour = (kind: string) => MAP[serviceOf(kind)]

export type PlaceGroup = "medical" | "shelter" | "infra"

export const placeGroupOf = (kind: string): PlaceGroup =>
  kind === "hospital" || kind === "medical_camp" ? "medical"
  : kind === "shelter" || kind === "relief_centre" || kind === "food_kitchen" ||
    kind === "water_point" || kind === "school" ? "shelter"
  : "infra"

export const placeColour = (kind: string) => {
  const g = placeGroupOf(kind)
  return g === "medical" ? MAP.medical : g === "shelter" ? MAP.shelter : MAP.infra
}

/** Unit status, drawn as a ring around the unit's badge. It is the thing a
 *  crew and a dispatcher both read first, so it keeps its own colours. */
export const statusRing = (status: string) =>
  status === "on_site" ? "#10b981"
  : status === "en_route" ? "#f59e0b"
  : status === "assigned" ? "#0ea5e9"
  : status === "offline" ? "#dc2626"
  : "#94a3b8"

/** The floating surfaces every control over the map sits on. */
export const SURFACE =
  "border border-border/80 bg-card/95 text-foreground shadow-[0_4px_16px_rgb(16_24_40/0.10)] backdrop-blur-md"

/** Metres between two lng/lat points, haversine. */
export function metresBetween(a: [number, number], b: [number, number]): number {
  const R = 6371000
  const rad = Math.PI / 180
  const dLat = (b[1] - a[1]) * rad
  const dLng = (b[0] - a[0]) * rad
  const h = Math.sin(dLat / 2) ** 2 +
    Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}

export function formatMetres(m: number): string {
  if (!Number.isFinite(m)) return "—"
  if (m < 50) return `${Math.max(1, Math.round(m))} m`
  if (m < 1000) return `${Math.round(m / 10) * 10} m`
  if (m < 10_000) return `${(m / 1000).toFixed(1)} km`
  return `${Math.round(m / 1000)} km`
}

export function compass(a: [number, number], b: [number, number]): string {
  const dx = (b[0] - a[0]) * Math.cos(((a[1] + b[1]) / 2) * Math.PI / 180)
  const dy = b[1] - a[1]
  const deg = (Math.atan2(dx, dy) * 180 / Math.PI + 360) % 360
  return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.floor(((deg + 22.5) % 360) / 45)]
}

export function ageWords(ms: number): string {
  const s = Math.max(0, ms) / 1000
  if (s < 45) return "just now"
  if (s < 90) return "1 min ago"
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86_400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86_400)} d ago`
}

/** Where along a line a point sits (metres from the start) and how far off it
 *  is. Flat projection; good to a metre over the distances routed here. */
export function alongLine(line: number[][], p: [number, number]) {
  let best = { along: 0, off: Infinity }
  let run = 0
  for (let i = 0; i < line.length - 1; i++) {
    const a = line[i] as [number, number]
    const b = line[i + 1] as [number, number]
    const seg = metresBetween(a, b)
    const dx = b[0] - a[0]
    const dy = b[1] - a[1]
    const len2 = dx * dx + dy * dy
    const t = len2 === 0 ? 0 : Math.max(0, Math.min(1,
      ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2))
    const foot: [number, number] = [a[0] + t * dx, a[1] + t * dy]
    const off = metresBetween(p, foot)
    if (off < best.off) best = { along: run + seg * t, off }
    run += seg
  }
  return { ...best, total: run }
}

/** Universal "open in maps" link: Google Maps' directions URL opens the
 *  installed maps app on Android and iOS and the website elsewhere. */
export const externalMapsUrl = (lng: number, lat: number) =>
  `https://www.google.com/maps/dir/?api=1&destination=${lat.toFixed(6)},${lng.toFixed(6)}`

export const osmUrl = (lng: number, lat: number) =>
  `https://www.openstreetmap.org/?mlat=${lat.toFixed(5)}&mlon=${lng.toFixed(5)}#map=17/${lat.toFixed(5)}/${lng.toFixed(5)}`

/** Share a place with the platform share sheet, or copy it where there is none. */
export async function sharePlace(title: string, lng: number, lat: number): Promise<"shared" | "copied" | "failed"> {
  const text = `${title}\n${lat.toFixed(5)}, ${lng.toFixed(5)}`
  const url = osmUrl(lng, lat)
  try {
    if (navigator.share) {
      await navigator.share({ title, text, url })
      return "shared"
    }
  } catch (e) {
    // The person closed the sheet. Not a failure worth a message.
    if (e instanceof DOMException && e.name === "AbortError") return "shared"
  }
  try {
    await navigator.clipboard.writeText(`${text}\n${url}`)
    return "copied"
  } catch {
    return "failed"
  }
}
