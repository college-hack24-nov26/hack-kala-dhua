import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { shortHash } from "@/lib/ledger/crypto"
import type { DAGBlock, VerificationResult } from "@/lib/ledger/types"
import { TYPE_STYLE } from "./style"

const W = 168, H = 58, GX = 214, GY = 78, PAD = 24
const MIN_K = 0.2, MAX_K = 2.5
/** Pointer travel (px) below which a press is a click on a block, not a pan. */
const CLICK_SLOP = 4

type View = { x: number; y: number; k: number }

/** Blocks as nodes, parent -> child as edges, laid out by height (left to right).
 *  A grabbable canvas: drag to pan, pinch / Ctrl+wheel to zoom, wheel to pan, Fit to see everything. */
export default function DAGCanvas({ blocks, verified, selected, pending, onSelect }: {
  blocks: DAGBlock[]
  verified: Record<string, VerificationResult>
  selected: string | null
  /** Blocks held by a partitioned peer, not yet merged: drawn dashed. */
  pending?: Set<string>
  onSelect: (hash: string) => void
}) {
  const layout = useMemo(() => {
    const cols = new Map<number, DAGBlock[]>()
    for (const b of blocks) cols.set(b.height, [...(cols.get(b.height) ?? []), b])
    const pos: Record<string, { x: number; y: number }> = {}
    let rows = 1
    for (const [h, list] of cols) {
      list.sort((a, b) => a.timestamp.localeCompare(b.timestamp) || a.hash.localeCompare(b.hash))
      rows = Math.max(rows, list.length)
      list.forEach((b, i) => { pos[b.hash] = { x: PAD + h * GX, y: PAD + i * GY } })
    }
    const maxH = Math.max(0, ...blocks.map((b) => b.height))
    return { pos, width: PAD * 2 + maxH * GX + W, height: PAD * 2 + (rows - 1) * GY + H }
  }, [blocks])

  const box = useRef<HTMLDivElement>(null)
  const [view, setView] = useState<View>({ x: 0, y: 0, k: 1 })
  const [grabbing, setGrabbing] = useState(false)
  const pointers = useRef(new Map<number, { x: number; y: number }>())
  const drag = useRef<{ moved: number; pinch: number | null }>({ moved: 0, pinch: null })
  const fitted = useRef(false)

  const fit = useCallback(() => {
    const el = box.current
    if (!el) return
    const { clientWidth: cw, clientHeight: ch } = el
    if (!cw || !ch) return
    const k = Math.min(1, Math.max(MIN_K, Math.min(cw / layout.width, ch / layout.height)))
    setView({ k, x: (cw - layout.width * k) / 2, y: (ch - layout.height * k) / 2 })
  }, [layout.width, layout.height])

  /** Zoom by `factor` keeping the screen point (sx, sy) fixed. */
  const zoomAt = useCallback((factor: number, sx: number, sy: number) => {
    setView((v) => {
      const k = Math.min(MAX_K, Math.max(MIN_K, v.k * factor))
      const f = k / v.k
      return { k, x: sx - (sx - v.x) * f, y: sy - (sy - v.y) * f }
    })
  }, [])
  const zoomCentre = (factor: number) => {
    const el = box.current
    if (el) zoomAt(factor, el.clientWidth / 2, el.clientHeight / 2)
  }

  // First fit once there is something to show and a size to fit into.
  useEffect(() => {
    if (fitted.current || !blocks.length) return
    fitted.current = true
    requestAnimationFrame(fit)
  }, [blocks.length, fit])

  // Wheel: pan; Ctrl/Cmd+wheel or trackpad pinch (arrives as ctrl+wheel): zoom. Non-passive so the page does not scroll.
  useEffect(() => {
    const el = box.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const r = el.getBoundingClientRect()
      if (e.ctrlKey || e.metaKey) zoomAt(Math.exp(-e.deltaY * 0.01), e.clientX - r.left, e.clientY - r.top)
      else setView((v) => ({ ...v, x: v.x - e.deltaX, y: v.y - e.deltaY }))
    }
    el.addEventListener("wheel", onWheel, { passive: false })
    return () => el.removeEventListener("wheel", onWheel)
  }, [zoomAt])

  // Keep the selected block in view when it is picked from the inspector (parent / child links).
  useEffect(() => {
    const el = box.current, p = selected ? layout.pos[selected] : null
    if (!el || !p) return
    setView((v) => {
      const sx = v.x + p.x * v.k, sy = v.y + p.y * v.k, m = 24
      const inside = sx > m && sy > m && sx + W * v.k < el.clientWidth - m && sy + H * v.k < el.clientHeight - m
      return inside ? v : { ...v, x: el.clientWidth / 2 - (p.x + W / 2) * v.k, y: el.clientHeight / 2 - (p.y + H / 2) * v.k }
    })
  }, [selected, layout.pos])

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0 && e.pointerType === "mouse") return
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (pointers.current.size === 1) drag.current = { moved: 0, pinch: null }
    if (pointers.current.size === 2) {
      const [a, b] = [...pointers.current.values()]
      drag.current.pinch = Math.hypot(a.x - b.x, a.y - b.y)
    }
    setGrabbing(true)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const prev = pointers.current.get(e.pointerId)
    if (!prev) return
    const now = { x: e.clientX, y: e.clientY }
    pointers.current.set(e.pointerId, now)
    if (pointers.current.size >= 2 && drag.current.pinch) {
      const [a, b] = [...pointers.current.values()]
      const d = Math.hypot(a.x - b.x, a.y - b.y)
      const r = box.current!.getBoundingClientRect()
      zoomAt(d / drag.current.pinch, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top)
      drag.current.pinch = d
      drag.current.moved += CLICK_SLOP + 1
      return
    }
    const dx = now.x - prev.x, dy = now.y - prev.y
    drag.current.moved += Math.abs(dx) + Math.abs(dy)
    setView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }))
  }
  const onPointerUp = (e: React.PointerEvent) => {
    pointers.current.delete(e.pointerId)
    if (pointers.current.size < 2) drag.current.pinch = null
    if (!pointers.current.size) setGrabbing(false)
  }
  /** A press that did not travel is a click on the block under it. */
  const clickBlock = (hash: string) => { if (drag.current.moved <= CLICK_SLOP) onSelect(hash) }

  if (!blocks.length) {
    return <div className="text-muted-foreground grid h-full place-items-center text-sm">No blocks yet.</div>
  }
  return (
    <div ref={box} className={`relative h-full min-h-[360px] w-full touch-none select-none overflow-hidden ${grabbing ? "cursor-grabbing" : "cursor-grab"}`}
      onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
      onDoubleClick={(e) => { const r = box.current!.getBoundingClientRect(); zoomAt(1.5, e.clientX - r.left, e.clientY - r.top) }}>
      <svg className="absolute inset-0 block h-full w-full">
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0L10 5L0 10z" fill="var(--muted-foreground)" />
          </marker>
          <pattern id="dag-dots" width={24 * view.k} height={24 * view.k} patternUnits="userSpaceOnUse"
            x={view.x % (24 * view.k)} y={view.y % (24 * view.k)}>
            <circle cx={1} cy={1} r={1} fill="var(--muted-foreground)" opacity={0.25} />
          </pattern>
        </defs>
        <rect width="100%" height="100%" fill="url(#dag-dots)" />
        <g transform={`translate(${view.x},${view.y}) scale(${view.k})`}>
          {blocks.flatMap((b) => b.parents.map((p) => {
            const a = layout.pos[p], c = layout.pos[b.hash]
            if (!a || !c) return null
            const x1 = a.x + W, y1 = a.y + H / 2, x2 = c.x - 4, y2 = c.y + H / 2, mx = (x1 + x2) / 2
            return <path key={`${p}-${b.hash}`} d={`M${x1} ${y1}C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`} fill="none"
              stroke="var(--muted-foreground)" strokeOpacity={0.6} strokeWidth={1.4} vectorEffect="non-scaling-stroke"
              strokeDasharray={pending?.has(b.hash) ? "5 4" : undefined} markerEnd="url(#arrow)" />
          }))}
          {blocks.map((b) => {
            const p = layout.pos[b.hash], st = TYPE_STYLE[b.payloadType], v = verified[b.hash]
            const sel = b.hash === selected
            return (
              <g key={b.hash} transform={`translate(${p.x},${p.y})`} onClick={() => clickBlock(b.hash)} className="cursor-pointer">
                <rect width={W} height={H} rx={8} fill="var(--card)" stroke={sel ? "var(--primary)" : "var(--border)"}
                  strokeWidth={sel ? 2.5 : 1} strokeDasharray={pending?.has(b.hash) ? "5 4" : undefined} />
                <rect width={5} height={H} rx={2} fill={st.color} />
                <text x={14} y={19} fontSize={11} fontWeight={600} fill={st.color}>{st.label}</text>
                <text x={14} y={36} fontSize={11} fontFamily="ui-monospace,monospace" fill="var(--foreground)">{shortHash(b.hash)}</text>
                <text x={14} y={50} fontSize={9.5} fill="var(--muted-foreground)">
                  {b.issuer.length > 22 ? `${b.issuer.slice(0, 21)}…` : b.issuer}{b.payloadCID ? " · CID" : ""}
                </text>
                {v && (
                  <g transform={`translate(${W - 20},8)`}>
                    <circle cx={6} cy={6} r={7} fill={v.ok ? "#16a34a" : "#dc2626"} />
                    <text x={6} y={9.5} fontSize={10} fontWeight={700} textAnchor="middle" fill="#fff">{v.ok ? "✓" : "!"}</text>
                  </g>
                )}
              </g>
            )
          })}
        </g>
      </svg>
      <div className="bg-background/90 absolute right-2 top-2 flex items-center gap-1 rounded-md border p-1 text-xs"
        onPointerDown={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
        <button type="button" onClick={() => zoomCentre(1 / 1.25)} className="hover:bg-muted size-6 rounded" aria-label="Zoom out">−</button>
        <span className="w-10 text-center tabular-nums">{Math.round(view.k * 100)}%</span>
        <button type="button" onClick={() => zoomCentre(1.25)} className="hover:bg-muted size-6 rounded" aria-label="Zoom in">+</button>
        <button type="button" onClick={fit} className="hover:bg-muted rounded px-1.5 py-0.5">Fit</button>
      </div>
      <div className="text-muted-foreground bg-background/80 pointer-events-none absolute right-2 bottom-2 rounded px-1.5 py-0.5 text-[10px]">
        drag to pan · pinch or Ctrl+scroll to zoom · double-click to zoom in
      </div>
    </div>
  )
}
