import type { DAGBlock, PublicKeyJwk } from "./types"

/** JSON with keys sorted at every level, so the same content always hashes the same. */
export function canonicalizeJSON(obj: unknown): string {
  if (obj === null || typeof obj !== "object") return JSON.stringify(obj ?? null)
  if (Array.isArray(obj)) return `[${obj.map(canonicalizeJSON).join(",")}]`
  const o = obj as Record<string, unknown>
  return `{${Object.keys(o).filter((k) => o[k] !== undefined).sort()
    .map((k) => `${JSON.stringify(k)}:${canonicalizeJSON(o[k])}`).join(",")}}`
}

const hex = (buf: ArrayBuffer) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("")

export async function sha256Bytes(data: ArrayBuffer | Uint8Array): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", data as BufferSource))
}

export async function generateSHA256(data: string): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(data)))
}

/** The fields a block's hash covers. Anything else on the object is a label. */
export function hashedFields(b: Omit<DAGBlock, "hash" | "signature" | "source" | "eventId"> | DAGBlock) {
  return {
    parents: [...b.parents].sort(), height: b.height, timestamp: b.timestamp, issuer: b.issuer,
    payloadType: b.payloadType, payload: b.payload, payloadCID: b.payloadCID, publicKey: b.publicKey,
  }
}

export async function computeBlockHash(b: Parameters<typeof hashedFields>[0]): Promise<string> {
  return generateSHA256(canonicalizeJSON(hashedFields(b)))
}

export async function verifyBlockHash(block: DAGBlock): Promise<boolean> {
  return (await computeBlockHash(block)) === block.hash
}

// --------------------------------------------------------------- signing ---
const b64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
const fromB64url = (s: string) =>
  Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), (c) => c.charCodeAt(0))

const ALG = { name: "ECDSA", namedCurve: "P-256" } as const
const SIG = { name: "ECDSA", hash: "SHA-256" } as const

export async function generateNodeKeys(): Promise<CryptoKeyPair> {
  // Private key non-extractable: it can sign, but cannot be copied out of the browser.
  return crypto.subtle.generateKey(ALG, false, ["sign", "verify"]) as Promise<CryptoKeyPair>
}

export async function exportPublicKey(key: CryptoKey): Promise<PublicKeyJwk> {
  const j = await crypto.subtle.exportKey("jwk", key)
  return { kty: "EC", crv: "P-256", x: j.x!, y: j.y! }
}

export async function signHash(priv: CryptoKey, hash: string): Promise<string> {
  return b64url(new Uint8Array(await crypto.subtle.sign(SIG, priv, new TextEncoder().encode(hash))))
}

export async function verifySignature(pub: PublicKeyJwk, hash: string, signature: string): Promise<boolean> {
  try {
    const key = await crypto.subtle.importKey("jwk", { ...pub, ext: true }, ALG, false, ["verify"])
    return await crypto.subtle.verify(SIG, key, fromB64url(signature) as BufferSource, new TextEncoder().encode(hash))
  } catch {
    return false
  }
}

export const shortHash = (h: string) => (h ? `0x${h.slice(0, 4)}…${h.slice(-3)}` : "—")
