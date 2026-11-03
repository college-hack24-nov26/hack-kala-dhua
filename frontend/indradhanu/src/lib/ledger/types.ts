/** Local DAG ledger: a tamper-evident, append-only record kept in this browser.
 *
 *  Every block names its parents by hash, so changing anything in an old block
 *  changes its hash and breaks every block after it. Blocks are also signed
 *  with this device's ECDSA P-256 key, so "who wrote this" is checkable too.
 *  Branches written while a node was cut off are merged later, never
 *  overwritten: a DAG, not a chain. */

export type BlockPayloadType =
  | "GENESIS"
  | "SOS_REQUEST"
  | "RESOURCE_ALLOCATION"
  | "INCIDENT_VERIFICATION"
  | "APPROVAL"
  | "FIELD_UPDATE"
  | "SENSOR_EVENT"
  | "SYSTEM_EVENT"
  | "MERGE_BLOCK"

/** Where a block came from. Only `hash`-covered fields are evidence; this is a label. */
export type BlockSource = "seed" | "local" | "peer" | "event"

/** Public half of the signing key, as JWK coordinates (P-256). */
export type PublicKeyJwk = { kty: "EC"; crv: "P-256"; x: string; y: string }

export interface DAGBlock {
  /** SHA-256 (hex) of the canonical JSON of every field below except hash/signature/source/eventId. */
  hash: string
  parents: string[]
  /** Longest path from genesis; drives the layout. */
  height: number
  timestamp: string
  /** Node that minted the block, e.g. "control-room-pune" or "ward-team-hadapsar". */
  issuer: string
  payloadType: BlockPayloadType
  payload: Record<string, unknown>
  /** Content id of an attachment kept in the local content store (see lib/ipfs/client). */
  payloadCID?: string
  publicKey: PublicKeyJwk
  /** ECDSA P-256 / SHA-256 signature over `hash`, base64url. */
  signature: string
  source: BlockSource
  /** Backend event id, for blocks anchored from the live event log. */
  eventId?: number
}

export interface DAGState {
  blocks: Record<string, DAGBlock>
  /** Blocks no other block points at yet. */
  tips: string[]
  genesis: string | null
}

export interface VerificationResult {
  hash: string
  hashOk: boolean
  signatureOk: boolean
  parentsKnown: boolean
  ok: boolean
  reason?: string
}
