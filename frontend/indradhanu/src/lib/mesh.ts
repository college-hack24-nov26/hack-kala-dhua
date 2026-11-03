/** Mesh mode: talking to bitchat on this same phone when the API is out of reach.
 *
 *  The browser cannot do Bluetooth mesh. The bitchat app on the phone can, and
 *  it runs a small HTTP API on the loopback address (the Indradhanu fork adds
 *  `/inbox` so we can read what the mesh delivered). A page served over HTTPS
 *  may call `http://127.0.0.1` in Chrome after the person allows "local network
 *  access" once; `targetAddressSpace: "local"` tells Chrome that is what this is.
 *
 *  Outbound: a report becomes one IDX1 "R" packet — words and a GPS fix, no
 *  photo — broadcast on the mesh. Any phone with signal running gateway mode
 *  (or a laptop running scripts/mesh_bridge.py) forwards it to the API, where it
 *  enters the same intake as a typed report, scored as an unsigned mesh report.
 *
 *  Inbound: alerts (A), road closures (B) and dispatches (D) the control room
 *  broadcast are read from the inbox and filtered to the ones near this phone.
 *
 *  Nothing here is signed: a key shipped in a public web page is not a secret.
 *  The API scores unsigned packets lower and never lets one dispatch on its own.
 */

import { nativeMesh } from "./native"

export const BITCHAT = "http://127.0.0.1:8765"

type LocalInit = RequestInit & { targetAddressSpace?: "local" | "private" }

async function local<T>(path: string, init: LocalInit = {}, timeoutMs = 2500): Promise<T> {
  // Inside the BiChat phone app the mesh is one call away, no loopback HTTP needed.
  const native = nativeMesh()
  if (native) {
    if (path === "/status") return native.status() as T
    if (path === "/send/text") {
      const { text } = JSON.parse(String(init.body ?? "{}")) as { text?: string }
      if (!text || !native.send(text)) throw new Error("BiChat did not accept the message")
      return { ok: true } as T
    }
    if (path.startsWith("/inbox")) {
      const since = Number(new URLSearchParams(path.split("?")[1] ?? "").get("since") ?? 0)
      return native.inbox(since) as T
    }
  }
  const ctl = new AbortController()
  const t = setTimeout(() => ctl.abort(), timeoutMs)
  try {
    const r = await fetch(`${BITCHAT}${path}`, {
      ...init,
      targetAddressSpace: "local",
      signal: ctl.signal,
      headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    } as LocalInit)
    const body = (await r.json().catch(() => ({}))) as T & { error?: string }
    if (!r.ok) throw new Error(body.error || `bitchat answered ${r.status}`)
    return body
  } finally {
    clearTimeout(t)
  }
}

export type MeshStatus = {
  reachable: boolean
  enabled: boolean
  peers: number
  error?: string
}

export async function meshStatus(): Promise<MeshStatus> {
  try {
    const s = await local<{ api_enabled?: boolean; peers_count?: number }>("/status")
    return { reachable: true, enabled: !!s.api_enabled, peers: s.peers_count ?? 0 }
  } catch (e) {
    return {
      reachable: false, enabled: false, peers: 0,
      error: e instanceof Error ? e.message : String(e),
    }
  }
}

function packet(type: string, body: Record<string, unknown>, human: string): string {
  const clean: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(body)) {
    if (v === null || v === undefined || v === "") continue
    clean[k] = (k === "la" || k === "lo") && typeof v === "number" ? Math.round(v * 1e5) / 1e5 : v
  }
  return `${human.slice(0, 240).trim()} IDX1|${type}|${JSON.stringify(clean)}|-`
}

function nodeId(): string {
  try {
    const k = "indradhanu.meshNode"
    let v = localStorage.getItem(k)
    if (!v) {
      v = `web-${Math.random().toString(36).slice(2, 10)}`
      localStorage.setItem(k, v)
    }
    return v
  } catch {
    return "web-anon"
  }
}

/** Send a report into the mesh. Resolves when bitchat accepted it. */
export async function sendReportViaMesh(r: {
  lat: number; lng: number; text: string; category?: string
}): Promise<string> {
  const id = `${nodeId()}-${Date.now().toString(36)}`
  const text = packet(
    "R",
    { id, n: nodeId(), k: r.category, la: r.lat, lo: r.lng, x: r.text.slice(0, 280),
      t: Math.floor(Date.now() / 1000) },
    `[SOS] ${r.text.slice(0, 60)}`,
  )
  await local("/send/text", { method: "POST", body: JSON.stringify({ text }) })
  return id
}

export type MeshNotice = {
  seq: number
  kind: "alert" | "block" | "dispatch" | "cancel" | "other"
  text: string
  lat?: number
  lng?: number
  radiusM?: number
  distanceM?: number
  at: number
}

const KIND: Record<string, MeshNotice["kind"]> = { A: "alert", B: "block", D: "dispatch", C: "cancel" }

function metres(a: [number, number], b: [number, number]): number {
  const R = 6371000, rad = Math.PI / 180
  const dLat = (b[1] - a[1]) * rad, dLng = (b[0] - a[0]) * rad
  const h = Math.sin(dLat / 2) ** 2 +
    Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLng / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(h))
}

/** Read what the mesh delivered since `since`, keep what concerns this spot. */
export async function readMeshInbox(
  since: number, here: { lng: number; lat: number },
): Promise<{ next: number; notices: MeshNotice[] }> {
  const box = await local<{
    messages: { seq: number; text: string; at_ms: number }[]; next: number
  }>(`/inbox?since=${since}`)
  const notices: MeshNotice[] = []
  for (const m of box.messages ?? []) {
    const i = m.text.indexOf("IDX1|")
    if (i < 0) continue
    const parts = m.text.slice(i).split("|")
    const type = parts[1]
    let body: Record<string, unknown> = {}
    try { body = JSON.parse(parts.slice(2, -1).join("|")) } catch { continue }
    const kind = KIND[type] ?? "other"
    if (kind === "other") continue
    const lat = typeof body.la === "number" ? body.la : undefined
    const lng = typeof body.lo === "number" ? body.lo : undefined
    const radiusM = typeof body.r === "number" ? body.r : kind === "block" ? 150 : 3000
    const distanceM = lat !== undefined && lng !== undefined
      ? Math.round(metres([here.lng, here.lat], [lng, lat])) : undefined
    // Alerts for somewhere else are not for this phone. A block matters if it
    // is anywhere near where a person on foot might go (5 km).
    if (distanceM !== undefined) {
      if (kind === "alert" && distanceM > radiusM + 1000) continue
      if (kind === "block" && distanceM > 5000) continue
    }
    notices.push({
      seq: m.seq, kind, lat, lng, radiusM, distanceM, at: m.at_ms,
      text: m.text.slice(0, i).trim() || String(body.x ?? ""),
    })
  }
  return { next: box.next ?? since, notices }
}

/** Compass words for a heading from a to b. */
export function bearing(a: { lng: number; lat: number }, b: { lng: number; lat: number }): string {
  const dx = (b.lng - a.lng) * Math.cos(((a.lat + b.lat) / 2) * Math.PI / 180)
  const dy = b.lat - a.lat
  const deg = (Math.atan2(dx, dy) * 180 / Math.PI + 360) % 360
  return ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"][
    Math.floor(((deg + 22.5) % 360) / 45)
  ]
}

export function distanceM(a: { lng: number; lat: number }, b: { lng: number; lat: number }): number {
  return Math.round(metres([a.lng, a.lat], [b.lng, b.lat]))
}
