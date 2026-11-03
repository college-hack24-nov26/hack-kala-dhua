import { useEffect, useState } from "react"
import { retrieveMediaByCID } from "@/lib/ipfs/client"
import { shortHash } from "@/lib/ledger/crypto"
import type { DAGBlock, VerificationResult } from "@/lib/ledger/types"
import { TYPE_STYLE } from "./style"

function JsonView({ value }: { value: unknown }) {
  const text = JSON.stringify(value, null, 2) ?? "null"
  // Light syntax colouring: keys, strings, numbers/booleans.
  const parts = text.split(/("(?:\\.|[^"\\])*"\s*:?|\b-?\d+(?:\.\d+)?\b|\btrue\b|\bfalse\b|\bnull\b)/g)
  return (
    <pre className="bg-muted/40 max-h-[260px] overflow-auto rounded-md border p-2 text-[11px] leading-relaxed">
      {parts.map((p, i) =>
        /^".*":?$/s.test(p.trim()) && p.trim().endsWith(":") ? <span key={i} className="text-sky-500">{p}</span>
        : /^"/.test(p) ? <span key={i} className="text-emerald-500">{p}</span>
        : /^(-?\d|true|false|null)/.test(p) ? <span key={i} className="text-amber-500">{p}</span>
        : <span key={i}>{p}</span>)}
    </pre>
  )
}

function Media({ cid }: { cid: string }) {
  const [state, setState] = useState<{ url?: string; json?: unknown; missing?: boolean }>({})
  useEffect(() => {
    let url: string | undefined, alive = true
    void retrieveMediaByCID(cid).then((v) => {
      if (!alive) return
      if (v instanceof Blob) { url = URL.createObjectURL(v); setState({ url }) }
      else if (v === undefined) setState({ missing: true })
      else setState({ json: v })
    })
    return () => { alive = false; if (url) URL.revokeObjectURL(url) }
  }, [cid])
  if (state.missing) return <p className="text-xs text-amber-600">Not in this device's content store.</p>
  if (state.url) return <img src={state.url} alt="Attachment" className="w-full rounded-md border" />
  if (state.json !== undefined) return <JsonView value={state.json} />
  return <p className="text-muted-foreground text-xs">Loading…</p>
}

export default function BlockInspector({ block, verification, onSelect, onTamper, known }: {
  block: DAGBlock | null
  verification?: VerificationResult
  known: Record<string, DAGBlock>
  onSelect: (hash: string) => void
  onTamper: (hash: string) => void
}) {
  if (!block) return <div className="text-muted-foreground p-4 text-sm">Select a block in the graph.</div>
  const st = TYPE_STYLE[block.payloadType]
  const children = Object.values(known).filter((b) => b.parents.includes(block.hash))
  return (
    <div className="space-y-4 p-4 text-xs">
      <div>
        <div className="flex items-center justify-between">
          <span className="font-semibold" style={{ color: st.color }}>{st.label}</span>
          <span className="text-muted-foreground rounded border px-1.5 py-0.5">{block.source}</span>
        </div>
        <div className="mt-1 break-all font-mono text-[11px]">{block.hash}</div>
      </div>

      {verification && (verification.ok ? (
        <div className="rounded-md border border-emerald-600/50 bg-emerald-600/10 p-3">
          <div className="font-semibold text-emerald-600">✓ Verified immutable</div>
          <div className="text-muted-foreground mt-1">Hash recomputed and matches; ECDSA signature valid for {block.issuer}; all parents present.</div>
        </div>
      ) : (
        <div className="rounded-md border border-red-600/50 bg-red-600/10 p-3">
          <div className="font-semibold text-red-600">! Cryptographic mismatch</div>
          <div className="mt-1">{verification.reason}</div>
        </div>
      ))}

      <dl className="grid grid-cols-[92px_1fr] gap-x-2 gap-y-1.5">
        <dt className="text-muted-foreground">Timestamp</dt><dd>{new Date(block.timestamp).toLocaleString()}</dd>
        <dt className="text-muted-foreground">Issuer</dt><dd className="break-all">{block.issuer}</dd>
        <dt className="text-muted-foreground">Height</dt><dd>{block.height}</dd>
        <dt className="text-muted-foreground">Parents</dt>
        <dd className="space-y-0.5">{block.parents.length ? block.parents.map((p) => (
          <button key={p} type="button" className="text-primary block font-mono underline" onClick={() => onSelect(p)}>{shortHash(p)}</button>
        )) : "none (genesis)"}</dd>
        <dt className="text-muted-foreground">Children</dt>
        <dd className="space-y-0.5">{children.length ? children.map((c) => (
          <button key={c.hash} type="button" className="text-primary block font-mono underline" onClick={() => onSelect(c.hash)}>{shortHash(c.hash)}</button>
        )) : "none (a tip)"}</dd>
        <dt className="text-muted-foreground">Signature</dt><dd className="break-all font-mono text-[10px]">{block.signature.slice(0, 44)}…</dd>
        {block.eventId != null && <><dt className="text-muted-foreground">Event</dt><dd>#{block.eventId} (live event log)</dd></>}
      </dl>

      <div>
        <div className="mb-1 font-medium">Payload</div>
        <JsonView value={block.payload} />
      </div>

      {block.payloadCID && (
        <div>
          <div className="mb-1 font-medium">Attachment</div>
          <div className="text-muted-foreground mb-1 break-all font-mono text-[10px]">{block.payloadCID}</div>
          <Media cid={block.payloadCID} />
        </div>
      )}

      <button type="button" onClick={() => onTamper(block.hash)}
        className="text-muted-foreground hover:text-red-600 rounded-md border px-2 py-1">
        Tamper with this block (demo)
      </button>
    </div>
  )
}
