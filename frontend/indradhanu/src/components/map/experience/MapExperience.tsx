import {
  useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode,
} from "react"
import type { MapPadding } from "../LiveMap"
import { useMediaQuery } from "./hooks"

/** The map-first layout the citizen and crew apps share.
 *
 *  Phone: the map is the screen. A floating top bar, floating controls on the
 *  right, and a draggable sheet. Collapsed, the sheet answers "what is this,
 *  how far, what do I do"; pulled up, it holds everything else. It stops short
 *  of the top so the map stays in view.
 *
 *  Tablet: the same sheet, as a floating card on the left rather than a full
 *  width slab, so the map is still most of the screen.
 *
 *  Desktop: MAP | CONTEXT PANEL. The map stays dominant; the panel shows the
 *  same content the sheet does, so the two layouts cannot drift.
 *
 *  The map is told how much of it is covered (`padding`), so centring on a
 *  selection or the person centres in the part they can actually see.
 */

export type Snap = "peek" | "half" | "full"

const DESKTOP = "(min-width: 1024px)"
const TABLET = "(min-width: 768px)"

function useViewportHeight(): number {
  const [h, setH] = useState(() => (typeof window === "undefined" ? 800 : window.innerHeight))
  useEffect(() => {
    const on = () => setH(window.visualViewport?.height ?? window.innerHeight)
    on()
    window.addEventListener("resize", on)
    window.visualViewport?.addEventListener("resize", on)
    return () => {
      window.removeEventListener("resize", on)
      window.visualViewport?.removeEventListener("resize", on)
    }
  }, [])
  return h
}

export function MapExperience({
  map, top, banner, controls, peek, body, panelTitle, snapRequest, className = "",
}: {
  /** The map, given the padding its floating UI covers. */
  map: (padding: MapPadding) => ReactNode
  /** Floating, top of the map: the bar, or the navigation panel while guiding. */
  top: ReactNode
  /** Under the top: a critical alert, a re-route notice. */
  banner?: ReactNode
  /** The floating control column. */
  controls?: ReactNode
  /** Always visible: the sheet's collapsed content, the panel's head. */
  peek: ReactNode
  /** The rest: pulled up on a phone, scrolled in the panel on a desktop. */
  body: ReactNode
  /** Desktop panel heading. */
  panelTitle?: ReactNode
  /** Ask the sheet to move, e.g. back to `peek` when something is selected. */
  snapRequest?: { snap: Snap; key: number } | null
  className?: string
}) {
  const desktop = useMediaQuery(DESKTOP)
  const tablet = useMediaQuery(TABLET)
  const vh = useViewportHeight()

  // ---- top overlay height, measured, for the map's top padding.
  const topRef = useRef<HTMLDivElement>(null)
  const [topH, setTopH] = useState(64)
  useLayoutEffect(() => {
    const el = topRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setTopH(el.getBoundingClientRect().height))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // ---- sheet geometry.
  const peekRef = useRef<HTMLDivElement>(null)
  const [peekH, setPeekH] = useState(150)
  useLayoutEffect(() => {
    const el = peekRef.current
    if (!el) return
    const ro = new ResizeObserver(() => setPeekH(Math.ceil(el.getBoundingClientRect().height)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [desktop])

  // The home indicator on a phone in standalone mode, so the collapsed sheet
  // is never hidden behind it. Read from CSS, which is the only place it lives.
  const safeProbe = useRef<HTMLDivElement>(null)
  const [safeBottom, setSafeBottom] = useState(0)
  useLayoutEffect(() => {
    const el = safeProbe.current
    if (!el) return
    const read = () => setSafeBottom(el.getBoundingClientRect().height)
    read()
    window.addEventListener("resize", read)
    return () => window.removeEventListener("resize", read)
  }, [])

  const HANDLE = 22
  const heights = useMemo(() => {
    const peekPx = Math.min(peekH + HANDLE + (tablet ? 8 : safeBottom + 4), vh * 0.45)
    return {
      peek: peekPx,
      half: Math.max(peekPx + 40, vh * 0.52),
      full: Math.max(peekPx + 80, vh * (tablet ? 0.8 : 0.86)),
    }
  }, [peekH, vh, tablet, safeBottom])

  const [snap, setSnap] = useState<Snap>("peek")
  const [dragH, setDragH] = useState<number | null>(null)
  const drag = useRef<{ y: number; h: number; t: number; lastY: number; lastT: number } | null>(null)

  // A new request moves the sheet once. Adjusted during render, from the key,
  // rather than in an effect, so the sheet never paints at the old height first.
  const [seenRequest, setSeenRequest] = useState<number | undefined>(undefined)
  if (snapRequest && snapRequest.key !== seenRequest) {
    setSeenRequest(snapRequest.key)
    setSnap(snapRequest.snap)
  }

  const height = dragH ?? heights[snap]

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    // Buttons inside the draggable area still work as buttons.
    if ((e.target as HTMLElement).closest("button, a, input, textarea, select, [data-no-drag]")) return
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    const now = performance.now()
    drag.current = { y: e.clientY, h: heights[snap], t: now, lastY: e.clientY, lastT: now }
    setDragH(heights[snap])
  }, [heights, snap])

  const onPointerMove = useCallback((e: React.PointerEvent) => {
    const d = drag.current
    if (!d) return
    const next = Math.max(heights.peek * 0.7, Math.min(heights.full, d.h + (d.y - e.clientY)))
    d.lastY = e.clientY
    d.lastT = performance.now()
    setDragH(next)
  }, [heights])

  const onPointerUp = useCallback((e: React.PointerEvent) => {
    const d = drag.current
    drag.current = null
    if (!d) return
    const h = Math.max(heights.peek * 0.7, Math.min(heights.full, d.h + (d.y - e.clientY)))
    const dt = Math.max(1, performance.now() - d.lastT)
    const velocity = (e.clientY - d.lastY) / dt // px/ms, + is downward
    const order: Snap[] = ["peek", "half", "full"]
    let target: Snap
    if (Math.abs(h - d.h) < 6) {
      // A tap on the handle steps the sheet up, or back down from the top.
      target = snap === "full" ? "peek" : order[order.indexOf(snap) + 1]
    } else if (velocity < -0.45) {
      // A flick up: the next stop above where it was let go.
      target = order.find((s) => heights[s] > h + 1) ?? "full"
    } else if (velocity > 0.45) {
      // A flick down: the next stop below.
      target = [...order].reverse().find((s) => heights[s] < h - 1) ?? "peek"
    } else {
      target = order.reduce((best, s) =>
        Math.abs(heights[s] - h) < Math.abs(heights[best] - h) ? s : best, "peek" as Snap)
    }
    setSnap(target)
    setDragH(null)
  }, [heights, snap])

  // ---- what the map is covered by.
  const padding: MapPadding = useMemo(() => {
    if (desktop) return { top: topH + 16, right: 72, bottom: 24, left: 16 }
    if (tablet) return { top: topH + 16, right: 72, bottom: 24, left: Math.min(560, window.innerWidth * 0.6) + 16 }
    return { top: topH + 16, right: 64, bottom: Math.min(heights[snap], heights.half) + 8, left: 16 }
  }, [desktop, tablet, topH, heights, snap])

  // The controls ride above the sheet on a phone, and sit low on the right otherwise.
  const controlsBottom = desktop || tablet ? 24 : Math.min(height, heights.half) + 12
  const controlsHidden = !desktop && !tablet && height > heights.half + 24

  const panelContent = (
    <>
      <div ref={peekRef}>{peek}</div>
      {body}
    </>
  )

  return (
    <div className={`fixed inset-0 overflow-hidden bg-background text-foreground ${className}`}>
      <div ref={safeProbe} aria-hidden className="pointer-events-none invisible fixed left-0 top-0 h-[env(safe-area-inset-bottom)] w-px" />
      <div className={desktop ? "grid h-full grid-cols-[minmax(0,1fr)_minmax(380px,420px)]" : "h-full"}>
        <div className="relative h-full min-w-0">
          {map(padding)}

          {/* Top: bar or navigation panel, then banners. */}
          <div
            ref={topRef}
            className="pointer-events-none absolute inset-x-0 top-0 z-20 flex flex-col gap-2 px-3 pt-[max(0.75rem,env(safe-area-inset-top))] sm:px-4"
          >
            {/* On wide screens the bar and the navigation panel stay a card,
                not a strip across the whole map. */}
            <div className="pointer-events-auto w-full lg:max-w-[640px]">{top}</div>
            {banner && <div className="pointer-events-auto w-full lg:max-w-[640px]">{banner}</div>}
          </div>

          {controls && (
            <div
              className="absolute right-3 z-20 transition-[bottom,opacity] duration-200 sm:right-4"
              style={{
                bottom: desktop || tablet
                  ? `calc(${controlsBottom}px + env(safe-area-inset-bottom))`
                  : controlsBottom,
                opacity: controlsHidden ? 0 : 1,
                pointerEvents: controlsHidden ? "none" : "auto",
                transition: dragH === null ? undefined : "none",
              }}
            >
              {controls}
            </div>
          )}

          {/* Tablet and phone: the sheet. */}
          {!desktop && (
            <section
              aria-label="Details"
              className={
                "absolute z-30 flex flex-col overflow-hidden border border-border bg-card/97 shadow-[0_-8px_28px_rgb(16_24_40/0.12)] backdrop-blur-md " +
                (tablet
                  ? "bottom-4 left-4 w-[min(560px,60vw)] rounded-3xl"
                  : "inset-x-0 bottom-0 rounded-t-3xl border-b-0")
              }
              style={{
                height: tablet ? Math.min(height, vh - topH - 48) : height,
                transition: dragH === null ? "height 280ms cubic-bezier(.2,.8,.2,1)" : "none",
              }}
            >
              <div
                className="shrink-0 touch-none select-none"
                onPointerDown={onPointerDown}
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
                onPointerCancel={onPointerUp}
              >
                <div className="flex h-[22px] items-center justify-center" aria-hidden>
                  <div className="h-1 w-9 rounded-full bg-foreground/15" />
                </div>
                <div ref={peekRef} className="px-4 pb-2">{peek}</div>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
                {body}
              </div>
            </section>
          )}
        </div>

        {/* Desktop: the context panel. */}
        {desktop && (
          <aside className="flex h-full min-h-0 flex-col border-l border-border bg-card">
            {panelTitle && (
              <div className="shrink-0 border-b border-border px-5 py-4">{panelTitle}</div>
            )}
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">{panelContent}</div>
          </aside>
        )}
      </div>
    </div>
  )
}
