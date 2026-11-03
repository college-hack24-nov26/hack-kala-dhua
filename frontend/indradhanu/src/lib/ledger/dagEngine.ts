import { computeBlockHash, exportPublicKey, generateNodeKeys, signHash, verifyBlockHash, verifySignature } from "./crypto"
import { idbAll, idbClear, idbGet, idbPut, STORES } from "./idb"
import type { BlockPayloadType, BlockSource, DAGBlock, DAGState, VerificationResult } from "./types"

/** Tips a new block links to: one keeps the graph a chain, two knit branches together. */
const MAX_PARENTS = 2

/** A signing identity for one node. The private key never leaves IndexedDB. */
type NodeKey = { name: string; pair: CryptoKeyPair }

export class DAGEngine {
  state: DAGState = { blocks: {}, tips: [], genesis: null }
  private keys = new Map<string, NodeKey>()

  async load(): Promise<DAGState> {
    const blocks = await idbAll<DAGBlock>(STORES.blocks)
    this.state = DAGEngine.index(blocks)
    return this.state
  }

  static index(list: DAGBlock[]): DAGState {
    const blocks: Record<string, DAGBlock> = {}
    for (const b of list) blocks[b.hash] = b
    const referenced = new Set(list.flatMap((b) => b.parents))
    const tips = list.filter((b) => !referenced.has(b.hash))
      .sort((a, b) => b.height - a.height || b.timestamp.localeCompare(a.timestamp)).map((b) => b.hash)
    const genesis = list.find((b) => b.payloadType === "GENESIS")?.hash ?? null
    return { blocks, tips, genesis }
  }

  get list(): DAGBlock[] {
    return Object.values(this.state.blocks).sort((a, b) => a.height - b.height || a.timestamp.localeCompare(b.timestamp))
  }

  /** The signing key for a node name, created once and kept in IndexedDB. */
  private async key(issuer: string): Promise<NodeKey> {
    const hit = this.keys.get(issuer)
    if (hit) return hit
    let pair = await idbGet<CryptoKeyPair>(STORES.keys, issuer)
    if (!pair) {
      pair = await generateNodeKeys()
      await idbPut(STORES.keys, pair, issuer)
    }
    const k = { name: issuer, pair }
    this.keys.set(issuer, k)
    return k
  }

  /** Mint a block on top of the current tips (or the given parents) and persist it. */
  async addBlock(payloadType: BlockPayloadType, payload: Record<string, unknown>, payloadCID?: string, opts: {
    issuer?: string; parents?: string[]; timestamp?: string; source?: BlockSource; eventId?: number
    /** Use the full list of tips (a merge), not just the newest one or two. */
    allTips?: boolean
    /** false: keep in memory only (a simulated peer that has not synced yet). */
    persist?: boolean
  } = {}): Promise<DAGBlock> {
    const parents = opts.parents ?? (opts.allTips ? this.state.tips : this.state.tips.slice(0, MAX_PARENTS))
    const issuer = opts.issuer ?? "control-room-pune"
    const k = await this.key(issuer)
    const height = parents.length ? Math.max(...parents.map((p) => this.state.blocks[p]?.height ?? 0)) + 1 : 0
    const draft = {
      parents: [...new Set(parents)], height, timestamp: opts.timestamp ?? new Date().toISOString(), issuer,
      payloadType, payload, payloadCID, publicKey: await exportPublicKey(k.pair.publicKey),
    }
    const hash = await computeBlockHash(draft)
    const block: DAGBlock = { ...draft, hash, signature: await signHash(k.pair.privateKey, hash),
      source: opts.source ?? "local", ...(opts.eventId != null ? { eventId: opts.eventId } : {}) }
    if (this.state.blocks[hash]) return this.state.blocks[hash]
    if (opts.persist !== false) await idbPut(STORES.blocks, block)
    this.state = DAGEngine.index([...Object.values(this.state.blocks), block])
    return block
  }

  /** Bring in blocks written elsewhere (a peer that was cut off). Nothing local is
   *  overwritten: blocks are keyed by hash, so a duplicate is the same block, and a
   *  block that fails verification is refused rather than stored. */
  async mergeRemoteDAG(remoteBlocks: DAGBlock[]): Promise<{ added: number; duplicates: number; rejected: number }> {
    let added = 0, duplicates = 0, rejected = 0
    const pending = [...remoteBlocks].sort((a, b) => a.height - b.height)
    for (const b of pending) {
      if (this.state.blocks[b.hash]) { duplicates++; continue }
      const v = await DAGEngine.verify(b, { ...this.state.blocks, ...Object.fromEntries(pending.map((x) => [x.hash, x])) })
      if (!v.ok) { rejected++; continue }
      await idbPut(STORES.blocks, b)
      this.state.blocks[b.hash] = b
      added++
    }
    this.state = DAGEngine.index(Object.values(this.state.blocks))
    return { added, duplicates, rejected }
  }

  static async verify(b: DAGBlock, known: Record<string, DAGBlock>): Promise<VerificationResult> {
    const hashOk = await verifyBlockHash(b)
    const signatureOk = await verifySignature(b.publicKey, b.hash, b.signature)
    const parentsKnown = b.parents.every((p) => !!known[p])
    const ok = hashOk && signatureOk && parentsKnown
    const reason = !hashOk ? "Content does not match its hash: the block was changed after it was written."
      : !signatureOk ? "Signature does not match the issuer's key."
      : !parentsKnown ? "A parent block is missing from this ledger." : undefined
    return { hash: b.hash, hashOk, signatureOk, parentsKnown, ok, reason }
  }

  async verifyAll(): Promise<Record<string, VerificationResult>> {
    const out: Record<string, VerificationResult> = {}
    for (const b of this.list) out[b.hash] = await DAGEngine.verify(b, this.state.blocks)
    return out
  }

  /** Demo only: change a stored block's content without re-hashing it, the way a
   *  tamperer would. Verification then flags it; re-seeding restores the ledger. */
  async tamper(hash: string): Promise<void> {
    const b = this.state.blocks[hash]
    if (!b) return
    const changed: DAGBlock = { ...b, payload: { ...b.payload, tampered_note: "edited after the fact" } }
    await idbPut(STORES.blocks, changed)
    this.state.blocks[hash] = changed
  }

  async reset(): Promise<void> {
    await idbClear(STORES.blocks)
    this.state = { blocks: {}, tips: [], genesis: null }
  }
}

/** An in-memory copy of a ledger: what a peer node holds when the mesh splits. */
export function forkPeer(from: DAGEngine): DAGEngine {
  const peer = new DAGEngine()
  peer.state = { blocks: { ...from.state.blocks }, tips: [...from.state.tips], genesis: from.state.genesis }
  return peer
}

export const ledger = new DAGEngine()
