import { useCallback, useEffect, useMemo, useState } from "react"
import { GitFork } from "lucide-react"
import { toast } from "sonner"
import BlockInspector from "@/components/ledger/BlockInspector"
import DAGCanvas from "@/components/ledger/DAGCanvas"
import { TYPE_STYLE } from "@/components/ledger/style"
import { Card } from "@/components/ui/card"
import { countMedia } from "@/lib/ipfs/client"
import { DAGEngine, forkPeer, ledger } from "@/lib/ledger/dagEngine"
import { anchorEvents } from "@/lib/ledger/eventAnchor"
import { seedLedger } from "@/lib/ledger/seedData"
import type { BlockPayloadType, DAGBlock, VerificationResult } from "@/lib/ledger/types"

/** Local DAG ledger: a tamper-evident record of the response, kept on this
 *  device, that keeps working through a network partition and merges cleanly
 *  afterwards. Seed blocks are illustrative; "Anchor live events" writes the
 *  command centre's real event log into it, linked by cause. */

const MINTABLE: BlockPayloadType[] = ["SOS_REQUEST", "RESOURCE_ALLOCATION", "APPROVAL", "INCIDENT_VERIFICATION", "FIELD_UPDATE"]

export default function Ledger() {
  const [, setTick] = useState(0)
  const refresh = () => setTick((t) => t + 1)
  const [ready, setReady] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [verified, setVerified] = useState<Record<string, VerificationResult>>({})
  const [cids, setCids] = useState(0)
  const [peer, setPeer] = useState<DAGEngine | null>(null)
  const [busy, setBusy] = useState(false)
  const [mintType, setMintType] = useState<BlockPayloadType>("FIELD_UPDATE")
  const [mintText, setMintText] = useState("")

  const verifyAll = useCallback(async () => {
    setVerified(await ledger.verifyAll())
    setCids(await countMedia())
  }, [])

  useEffect(() => {
    void (async () => {
      try {
        await ledger.load()
        if (!Object.keys(ledger.state.blocks).length) await seedLedger(ledger)
        await verifyAll()
        setSelected(ledger.state.tips[0] ?? null)
      } catch (e) {
        toast.error(`Ledger storage unavailable: ${e instanceof Error ? e.message : e}`)
      }
      setReady(true)
    })()
  }, [verifyAll])

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    try { await fn() } catch (e) { toast.error(e instanceof Error ? e.message : String(e)) }
    finally { setBusy(false); refresh() }
  }

  // Main ledger plus, while partitioned, the peer's unmerged blocks.
  const pendingSet = useMemo(() => new Set(peer ? Object.keys(peer.state.blocks).filter((h) => !ledger.state.blocks[h]) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [peer, verified, busy])
  const shown: DAGBlock[] = useMemo(() => {
    const all = { ...ledger.state.blocks, ...(peer ? peer.state.blocks : {}) }
    return Object.values(all).sort((a, b) => a.height - b.height)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [verified, peer, busy])
  const known = useMemo(() => Object.fromEntries(shown.map((b) => [b.hash, b])), [shown])
  const total = Object.keys(ledger.state.blocks).length
  const okCount = Object.values(verified).filter((v) => v.ok).length
  const issuers = new Set(Object.values(ledger.state.blocks).map((b) => b.issuer)).size

  const mint = () => run(async () => {
    const payload = { note: mintText.trim() || `${TYPE_STYLE[mintType].label} recorded`, by: "console" }
    if (peer) {
      // Both sides keep writing while cut off: this console, and the peer ward team.
      await ledger.addBlock(mintType, payload)
      await peer.addBlock("FIELD_UPDATE", { note: "Ward team update written offline", offline: true },
        undefined, { issuer: "ward-team-offline", source: "peer", persist: false })
    } else {
      await ledger.addBlock(mintType, payload)
    }
    setMintText("")
    setSelected(ledger.state.tips[0])
    await verifyAll()
  })

  const partition = () => run(async () => {
    if (!peer) {
      setPeer(forkPeer(ledger))
      toast("Partition: a ward team is now offline", { description: "Mint blocks on both sides, then rejoin to merge." })
      return
    }
    const remote = Object.values(peer.state.blocks).filter((b) => !ledger.state.blocks[b.hash])
    const res = await ledger.mergeRemoteDAG(remote)
    if (ledger.state.tips.length > 1) {
      await ledger.addBlock("MERGE_BLOCK", { note: "Mesh reconnected; branches merged", added: res.added,
        duplicates: res.duplicates, rejected: res.rejected }, undefined, { allTips: true })
    }
    setPeer(null)
    setSelected(ledger.state.tips[0])
    await verifyAll()
    toast.success(`Merged ${res.added} peer block(s); ${res.duplicates} duplicate(s), ${res.rejected} rejected`)
  })

  const anchor = () => run(async () => {
    const r = await anchorEvents(ledger)
    await verifyAll()
    setSelected(ledger.state.tips[0])
    toast.success(r.added ? `Anchored ${r.added} live event(s), linked by cause` : "Ledger already holds the latest events")
  })

  const reseed = () => run(async () => {
    setPeer(null)
    await ledger.reset()
    await seedLedger(ledger)
    await verifyAll()
    setSelected(ledger.state.tips[0])
  })

  const tamper = (hash: string) => run(async () => {
    await ledger.tamper(hash)
    await verifyAll()
    toast.warning("Block content changed without re-hashing. Verification now flags it.")
  })

  const btn = "rounded-md border px-2.5 py-1 text-xs hover:bg-muted disabled:opacity-50"
  return (
    <div className="flex h-[calc(100vh-4rem)] flex-col gap-3 p-5 md:p-8 xl:px-10">
      {/* Pane 1: status */}
      <Card className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
        <div className="flex items-center gap-2">
          <GitFork className="size-4" />
          <span className="font-semibold">Local DAG Ledger</span>
          <span className={`rounded-full px-2 py-0.5 text-[11px] ${peer ? "bg-amber-500/15 text-amber-600" : "bg-emerald-500/15 text-emerald-600"}`}>
            {peer ? `Partitioned · ${pendingSet.size} peer block(s) unmerged` : `DAG branch: local mesh · ${issuers} signing node(s)`}
          </span>
        </div>
        <div className="text-muted-foreground flex gap-4 text-xs tabular-nums">
          <span><b className="text-foreground">{total}</b> blocks</span>
          <span><b className={okCount === total ? "text-emerald-600" : "text-red-600"}>{total ? Math.round((okCount / total) * 100) : 0}%</b> verified</span>
          <span><b className="text-foreground">{cids}</b> content ids stored</span>
          <span>{ledger.state.tips.length} tip(s)</span>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <select value={mintType} onChange={(e) => setMintType(e.target.value as BlockPayloadType)} className="bg-background rounded-md border px-2 py-1 text-xs">
            {MINTABLE.map((t) => <option key={t} value={t}>{TYPE_STYLE[t].label}</option>)}
          </select>
          <input value={mintText} onChange={(e) => setMintText(e.target.value)} placeholder="note (optional)"
            className="bg-background w-40 rounded-md border px-2 py-1 text-xs" />
          <button type="button" disabled={busy || !ready} onClick={mint} className="bg-primary text-primary-foreground rounded-md px-2.5 py-1 text-xs disabled:opacity-50">Mint block</button>
          <button type="button" disabled={busy || !ready} onClick={partition} className={btn}>{peer ? "Rejoin & merge" : "Simulate offline partition"}</button>
          <button type="button" disabled={busy || !ready} onClick={() => run(verifyAll)} className={btn}>Verify all hashes</button>
          <button type="button" disabled={busy || !ready} onClick={anchor} className={btn}>Anchor live events</button>
          <button type="button" disabled={busy || !ready} onClick={reseed} className={btn} title="Clear this device's ledger and rebuild the seed history">Reset to seed</button>
        </div>
      </Card>

      <div className="grid min-h-0 flex-1 gap-3 lg:grid-cols-[1fr_380px]">
        {/* Pane 2: graph */}
        <Card className="relative min-h-[360px] overflow-hidden p-0">
          {ready ? <DAGCanvas blocks={shown} verified={verified} selected={selected} pending={pendingSet} onSelect={setSelected} />
            : <div className="text-muted-foreground grid h-full place-items-center text-sm">Opening ledger…</div>}
          <div className="bg-background/90 absolute bottom-2 left-2 flex flex-wrap gap-x-3 gap-y-1 rounded-md border px-2 py-1 text-[10px]">
            {Object.entries(TYPE_STYLE).map(([k, s]) => (
              <span key={k} className="flex items-center gap-1"><span className="size-2 rounded-sm" style={{ background: s.color }} />{s.label}</span>
            ))}
          </div>
        </Card>
        {/* Pane 3: inspector */}
        <Card className="min-h-0 overflow-y-auto p-0">
          <BlockInspector block={selected ? known[selected] ?? null : null} verification={selected ? verified[selected] : undefined}
            known={known} onSelect={setSelected} onTamper={tamper} />
        </Card>
      </div>
      <p className="text-muted-foreground text-[11px]">
        Stored in this browser (IndexedDB); each device keeps its own copy. Blocks marked "seed" are illustrative; "event"
        blocks are the command centre's real event log. Content ids are CIDv1 (raw, sha2-256), checkable against IPFS.
      </p>
    </div>
  )
}
