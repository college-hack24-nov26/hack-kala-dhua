/** Offline content store with IPFS-compatible content identifiers.
 *
 *  The id is a real CIDv1 (raw codec, sha2-256), base32: the same string
 *  `ipfs add --cid-version 1 --raw-leaves` gives for a small file with the
 *  same bytes, so a CID minted offline can be checked against IPFS later.
 *  (Raw-codec CIDs start "bafkrei"; "bafybei" is the dag-pb codec, which
 *  hashes a wrapper rather than the bytes themselves.)
 *  Content lives in this browser's IndexedDB until a gateway is reachable. */
import { sha256Bytes } from "@/lib/ledger/crypto"
import { idbGet, idbPut, STORES } from "@/lib/ledger/idb"

const B32 = "abcdefghijklmnopqrstuvwxyz234567"

function base32(bytes: Uint8Array): string {
  let bits = 0, value = 0, out = ""
  for (const b of bytes) {
    value = (value << 8) | b
    bits += 8
    while (bits >= 5) { out += B32[(value >>> (bits - 5)) & 31]; bits -= 5 }
  }
  if (bits > 0) out += B32[(value << (5 - bits)) & 31]
  return out
}

export async function cidFor(bytes: Uint8Array): Promise<string> {
  const digest = await sha256Bytes(bytes)
  // version 1, codec raw (0x55), multihash sha2-256 (0x12), length 32
  return "b" + base32(new Uint8Array([0x01, 0x55, 0x12, 0x20, ...digest]))
}

type Stored = { kind: "blob"; type: string; bytes: ArrayBuffer } | { kind: "json"; value: unknown }

export async function storeMediaOffline(fileOrBlob: Blob | Record<string, unknown>): Promise<string> {
  let bytes: Uint8Array, rec: Stored
  if (fileOrBlob instanceof Blob) {
    const buf = await fileOrBlob.arrayBuffer()
    bytes = new Uint8Array(buf)
    rec = { kind: "blob", type: fileOrBlob.type || "application/octet-stream", bytes: buf }
  } else {
    const text = JSON.stringify(fileOrBlob)
    bytes = new TextEncoder().encode(text)
    rec = { kind: "json", value: fileOrBlob }
  }
  const cid = await cidFor(bytes)
  if (!(await idbGet(STORES.media, cid))) await idbPut(STORES.media, rec, cid)
  return cid
}

/** A Blob for stored files, the object for stored JSON, undefined if absent. */
export async function retrieveMediaByCID(cid: string): Promise<Blob | unknown | undefined> {
  const rec = await idbGet<Stored>(STORES.media, cid)
  if (!rec) return undefined
  return rec.kind === "blob" ? new Blob([rec.bytes], { type: rec.type }) : rec.value
}

export async function countMedia(): Promise<number> {
  const { idbCount } = await import("@/lib/ledger/idb")
  return idbCount(STORES.media)
}
