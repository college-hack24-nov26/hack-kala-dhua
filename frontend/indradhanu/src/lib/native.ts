/** Running inside the BiChat phone app.
 *
 *  BiChat (the Android mesh app) shows this PWA full screen while the phone is
 *  online and switches to its own mesh screens when it is not. It exposes
 *  `window.BiChatNative`, which lets the page:
 *
 *   - sign in with the phone's account instead of asking again,
 *   - hand its place over (route, draft, position, crew unit) so the mesh side
 *     picks up where the person was, and read back what the mesh side changed,
 *   - send and read mesh packets directly, without bitchat's loopback HTTP API
 *     (which an HTTPS page inside a WebView cannot reach),
 *   - ask to switch to the mesh now.
 *
 *  In a normal browser none of this exists and every helper is a no-op.
 */

type Bridge = {
  isNative(): boolean
  session(): string
  getState(): string
  saveState(json: string): void
  switchToMesh(): void
  signedOut(): void
  meshStatus(): string
  sendText(text: string): boolean
  inbox(since: number): string
}

declare global {
  interface Window { BiChatNative?: Bridge }
}

function bridge(): Bridge | null {
  try {
    return typeof window !== "undefined" && window.BiChatNative?.isNative() ? window.BiChatNative : null
  } catch {
    return null
  }
}

export const inBiChat = (): boolean => bridge() !== null

export type NativeSession = { accessToken: string; refreshToken: string; expiresAt: number; email: string }

/** The phone app's signed-in session, when there is one. */
export function nativeSession(): NativeSession | null {
  const b = bridge()
  if (!b) return null
  try {
    const raw = b.session()
    return raw ? (JSON.parse(raw) as NativeSession) : null
  } catch {
    return null
  }
}

/** What the person was doing, shared with the mesh side of the app. */
export type Handoff = {
  destName?: string
  destKind?: string
  destLat?: number
  destLng?: number
  intent?: string
  navigating?: boolean
  selectedIncidentId?: string
  draft?: string
  lat?: number
  lng?: number
  unitId?: string
  updatedAt?: number
}

export function readHandoff(): Handoff {
  const b = bridge()
  if (!b) return {}
  try {
    return JSON.parse(b.getState() || "{}") as Handoff
  } catch {
    return {}
  }
}

/** Partial update; only the keys given change. Cheap enough to call on every change. */
export function saveHandoff(patch: Handoff): void {
  const b = bridge()
  if (!b) return
  try {
    b.saveState(JSON.stringify(patch))
  } catch {
    /* the phone app went away mid-call; nothing to keep */
  }
}

export function switchToMesh(): void {
  bridge()?.switchToMesh()
}

export function notifySignedOut(): void {
  try { bridge()?.signedOut() } catch { /* ignore */ }
}

/** Mesh calls answered by the phone app itself, mirroring bitchat's local API. */
export function nativeMesh(): {
  status(): { api_enabled?: boolean; peers_count?: number }
  send(text: string): boolean
  inbox(since: number): { messages: { seq: number; text: string; at_ms: number }[]; next: number }
} | null {
  const b = bridge()
  if (!b) return null
  return {
    status: () => JSON.parse(b.meshStatus() || "{}"),
    send: (text) => b.sendText(text),
    inbox: (since) => JSON.parse(b.inbox(since) || `{"messages":[],"next":${since}}`),
  }
}
