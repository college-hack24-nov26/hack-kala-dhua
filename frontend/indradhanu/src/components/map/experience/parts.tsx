import { useEffect, useRef, useState, type ReactNode } from "react"
import { inBiChat, switchToMesh } from "@/lib/native"
import {
  AlertTriangle, ArrowLeft, Crosshair, Layers, LocateFixed, Minus, Navigation2, Plus,
  SlidersHorizontal, X,
} from "lucide-react"
import { MAP, SURFACE, ageWords } from "../mapTheme"

/* Small pieces of the map experience, shared by the citizen and crew apps so
 * both read as one product: the same rounded dark surfaces, the same controls,
 * the same way of saying "offline" and "this is not a road route". */

// ------------------------------------------------------------ controls ---

function useOutside(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const on = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose()
    }
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") onClose() }
    document.addEventListener("pointerdown", on)
    document.addEventListener("keydown", key)
    return () => {
      document.removeEventListener("pointerdown", on)
      document.removeEventListener("keydown", key)
    }
  }, [open, onClose])
  return ref
}

export function RoundButton({
  label, onClick, children, active = false, className = "", size = "md", disabled,
}: {
  label: string
  onClick: () => void
  children: ReactNode
  active?: boolean
  className?: string
  size?: "md" | "lg"
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className={
        `${SURFACE} grid place-items-center rounded-full transition-colors hover:bg-muted ` +
        `disabled:opacity-50 ${size === "lg" ? "size-12" : "size-11"} ` +
        `${active ? "ring-2 ring-primary/30" : ""} ${className}`
      }
    >
      {children}
    </button>
  )
}

/** The floating column: layers, zoom (where a wheel or pinch is not the norm),
 *  and locate / recentre. Never a toolbar. */
export function MapControls({
  following, hasFix, onLocate, onZoomIn, onZoomOut, showZoom, layers,
}: {
  following: boolean
  hasFix: boolean
  onLocate: () => void
  onZoomIn: () => void
  onZoomOut: () => void
  showZoom: boolean
  layers?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const ref = useOutside(open, () => setOpen(false))
  return (
    <div className="flex flex-col items-end gap-2.5">
      {layers && (
        <div ref={ref} className="relative">
          <RoundButton label="Map layers and legend" onClick={() => setOpen((v) => !v)} active={open}>
            <Layers className="size-[18px]" />
          </RoundButton>
          {open && (
            <div className={`${SURFACE} absolute bottom-0 right-14 w-72 max-w-[calc(100vw-5rem)] rounded-2xl p-3.5`}>
              {layers}
            </div>
          )}
        </div>
      )}
      {showZoom && (
        <div className={`${SURFACE} flex w-11 flex-col overflow-hidden rounded-full`}>
          <button type="button" aria-label="Zoom in" title="Zoom in" onClick={onZoomIn}
                  className="grid h-11 place-items-center hover:bg-muted">
            <Plus className="size-[18px]" />
          </button>
          <div className="h-px bg-muted" />
          <button type="button" aria-label="Zoom out" title="Zoom out" onClick={onZoomOut}
                  className="grid h-11 place-items-center hover:bg-muted">
            <Minus className="size-[18px]" />
          </button>
        </div>
      )}
      <RoundButton
        size="lg"
        label={following ? "Following your location" : hasFix ? "Recentre on me" : "Find my location"}
        onClick={onLocate}
      >
        {following
          ? <LocateFixed className="size-5" style={{ color: MAP.you }} />
          : <Crosshair className={`size-5 ${hasFix ? "" : "text-muted-foreground/80"}`} />}
      </RoundButton>
    </div>
  )
}

export type FilterOption = { id: string; label: string; count: number; colour?: string }

/** One compact control in the top bar; the chips appear only while it is open. */
export function FilterControl({
  value, options, onChange,
}: {
  value: string
  options: FilterOption[]
  onChange: (id: string) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useOutside(open, () => setOpen(false))
  const current = options.find((o) => o.id === value)
  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={`Filter the map, showing ${current?.label ?? "all"}`}
        className={`${SURFACE} flex h-11 items-center gap-2 rounded-full px-3.5 text-xs font-medium`}
      >
        <SlidersHorizontal className="size-4" />
        {value !== "all" && current && <span>{current.label}</span>}
      </button>
      {open && (
        <div className={`${SURFACE} absolute right-0 top-[calc(100%+8px)] z-30 flex w-[min(92vw,360px)] flex-wrap gap-1.5 rounded-2xl p-2`}>
          {options.map((o) => {
            const active = o.id === value
            return (
              <button
                key={o.id}
                type="button"
                onClick={() => { onChange(o.id); setOpen(false) }}
                className={
                  "flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs transition-colors " +
                  (active ? "border-primary/40 bg-accent text-accent-foreground" : "border-border hover:bg-muted") +
                  (o.count === 0 && !active ? " text-muted-foreground/80" : "")
                }
              >
                {o.colour && <span className="size-2 rounded-full" style={{ background: o.colour }} />}
                {o.label}
                <span className="tabular-nums text-muted-foreground">{o.count}</span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

export function LegendRow({ colour, label, ring }: { colour: string; label: string; ring?: boolean }) {
  return (
    <div className="flex items-center gap-2 text-[11px] text-foreground/80">
      <span
        className="size-3 shrink-0 rounded-full"
        style={ring ? { border: `2px solid ${colour}` } : { background: colour, boxShadow: "0 0 0 1.5px #ffffff" }}
      />
      <span className="truncate">{label}</span>
    </div>
  )
}

export function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="text-[10.5px] font-medium uppercase tracking-[0.08em] text-muted-foreground/80">{children}</div>
  )
}

export function ChoicePills<T extends string>({
  value, options, onChange,
}: {
  value: T
  options: { id: T; label: string }[]
  onChange: (v: T) => void
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          onClick={() => onChange(o.id)}
          className={
            "rounded-full border px-3 py-1 text-xs " +
            (o.id === value ? "border-primary/40 bg-accent text-accent-foreground" : "border-border hover:bg-muted")
          }
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

// --------------------------------------------------------- connectivity ---

/** "Live", or exactly how old what you are looking at is.
 *
 *  Offline is a state, not an error: the map keeps its last data and the app
 *  keeps working, so this is one quiet line rather than a screen of red. What
 *  it must never do is let cached data pass for live. */
export function ConnectivityPill({
  online, reachable, staleSince, lastLive, queued = 0, now,
}: {
  online: boolean
  reachable: boolean
  staleSince: number | null
  lastLive: number | null
  queued?: number
  now: number
}) {
  const age = lastLive ? ageWords(now - lastLive).replace(" ago", "") : null
  let tone: "live" | "stale" | "off" = "live"
  let text = "Live"
  if (!online) {
    tone = "off"
    text = age ? `Offline · map data from ${age === "just now" ? "moments" : age} ago` : "Offline · no map data yet"
  } else if (!reachable) {
    tone = "off"
    text = age ? `Server unreachable · data ${age === "just now" ? "moments" : age} old` : "Server unreachable"
  } else if (staleSince !== null) {
    tone = "stale"
    text = `Cached copy · ${ageWords(now - staleSince).replace(" ago", "")} old`
  }
  if (queued > 0) text += ` · ${queued} waiting`
  const dot = tone === "live" ? "#32d74b" : "#f59e0b"
  return (
    <span className="flex min-w-0 items-center gap-1.5 text-[11px] text-muted-foreground" role="status">
      <span className="inline-flex size-2 shrink-0 rounded-full" style={{ background: dot }} />
      <span className="truncate">{text}</span>
      {inBiChat() && (
        // Inside the BiChat phone app: hand over to the offline mesh screens on request.
        <button
          type="button"
          onClick={switchToMesh}
          className="-my-2 shrink-0 cursor-pointer rounded-full px-2 py-2 font-medium text-sky-300 transition-colors duration-200 hover:text-sky-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400"
        >
          Use mesh
        </button>
      )}
    </span>
  )
}

/** The bar over the map: back/home, a title with live state under it, and
 *  whatever the page puts on the right (the filter). */
export function TopBar({
  title, subtitle, onBack, backLabel = "Back", right, badge,
}: {
  title: ReactNode
  subtitle?: ReactNode
  onBack?: () => void
  backLabel?: string
  right?: ReactNode
  badge?: ReactNode
}) {
  return (
    <div className="flex items-center gap-2">
      {onBack && (
        <RoundButton label={backLabel} onClick={onBack}>
          <ArrowLeft className="size-[18px]" />
        </RoundButton>
      )}
      <div className={`${SURFACE} flex h-11 min-w-0 flex-1 items-center gap-2.5 rounded-full px-4`}>
        <div className="min-w-0 flex-1 leading-tight">
          <div className="truncate text-[13px] font-semibold">{title}</div>
          {subtitle && <div className="-mt-px truncate">{subtitle}</div>}
        </div>
        {badge}
      </div>
      {right}
    </div>
  )
}

// ----------------------------------------------------------- navigation ---

export type Tone = "ok" | "caution" | "uncertain" | "danger"

const TONE: Record<Tone, string> = {
  ok: "#22c55e", caution: "#f59e0b", uncertain: "#f59e0b", danger: "#ef4444",
}

export function StatusLine({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <div className="flex items-start gap-2 text-[11.5px] leading-snug text-foreground/80">
      <span className="mt-[5px] size-1.5 shrink-0 rounded-full" style={{ background: TONE[tone] }} />
      <span className="min-w-0">{children}</span>
    </div>
  )
}

/** The compact panel while navigating: context, the one instruction that is
 *  true now, what is left, how far to trust it, and a way out. The map stays
 *  the biggest thing on the screen. */
export function NavPanel({
  context, distance, instruction, then, remaining, status, hazards, notice, onExit, arrived,
}: {
  /** "Ambulance A-12 → Fallen tree #AB12" */
  context: ReactNode
  /** Big: distance to the next turn, or to the destination. */
  distance?: string | null
  instruction?: ReactNode
  then?: ReactNode
  remaining?: ReactNode
  status: { tone: Tone; text: ReactNode }
  hazards?: { tone: Tone; text: ReactNode }[]
  notice?: ReactNode
  onExit: () => void
  arrived?: ReactNode
}) {
  return (
    <div className={`${SURFACE} rounded-[22px] px-4 pb-3.5 pt-3`} role="region" aria-label="Navigation">
      <div className="flex items-center gap-2">
        <Navigation2 className="size-3.5 shrink-0" style={{ color: MAP.you }} />
        <div className="min-w-0 flex-1 truncate text-[11.5px] font-medium text-muted-foreground">{context}</div>
        <button
          type="button"
          onClick={onExit}
          aria-label="End navigation"
          className="-mr-1.5 grid size-8 place-items-center rounded-full hover:bg-muted"
        >
          <X className="size-4" />
        </button>
      </div>
      {arrived ? (
        <div className="mt-1 text-lg font-semibold text-emerald-400">{arrived}</div>
      ) : (
        <>
          <div className="mt-0.5 flex items-baseline gap-2.5">
            {distance && <div className="text-[26px] font-semibold tabular-nums leading-none">{distance}</div>}
            {instruction && <div className="min-w-0 text-[15px] leading-snug">{instruction}</div>}
          </div>
          {then && <div className="mt-1 text-xs text-muted-foreground">{then}</div>}
          {remaining && <div className="mt-1 text-xs tabular-nums text-muted-foreground">{remaining}</div>}
        </>
      )}
      <div className="mt-2 space-y-1">
        <StatusLine tone={status.tone}>{status.text}</StatusLine>
        {hazards?.map((h, i) => <StatusLine key={i} tone={h.tone}>{h.text}</StatusLine>)}
        {notice && <StatusLine tone="caution">{notice}</StatusLine>}
      </div>
    </div>
  )
}

/** A critical event, announced over the map without taking the screen away. */
export function AlertBanner({
  title, detail, onView, viewLabel = "View", onDismiss, tone = "danger",
}: {
  title: ReactNode
  detail?: ReactNode
  onView?: () => void
  viewLabel?: string
  onDismiss?: () => void
  tone?: "danger" | "caution"
}) {
  const colour = tone === "danger" ? MAP.critical : MAP.caution
  return (
    <div
      role="alert"
      className="flex items-center gap-3 rounded-2xl border px-3 py-2.5 shadow-[0_6px_24px_rgb(0_0_0/0.35)] backdrop-blur-md"
      style={{ background: tone === "danger" ? "rgb(254 242 242 / 0.97)" : "rgb(255 251 235 / 0.97)", borderColor: `${colour}99` }}
    >
      <AlertTriangle className="size-4 shrink-0" style={{ color: colour }} />
      <div className="min-w-0 flex-1 leading-tight">
        <div className="text-[13px] font-semibold">{title}</div>
        {detail && <div className="mt-0.5 text-[11.5px] text-foreground/80">{detail}</div>}
      </div>
      {onView && (
        <button type="button" onClick={onView}
                className="rounded-full px-3 py-1.5 text-xs font-semibold hover:bg-muted"
                style={{ color: colour }}>
          {viewLabel}
        </button>
      )}
      {onDismiss && (
        <button type="button" onClick={onDismiss} aria-label="Dismiss"
                className="-mr-1 grid size-7 place-items-center rounded-full text-muted-foreground hover:bg-muted">
          <X className="size-3.5" />
        </button>
      )}
    </div>
  )
}

// --------------------------------------------------------------- lists ---

/** A disc in the marker's colour, so a list row and its pin read as one thing. */
export function Disc({ colour, children, size = 32, ring }: {
  colour: string; children?: ReactNode; size?: number; ring?: string
}) {
  return (
    <span
      className="grid shrink-0 place-items-center rounded-full text-white"
      style={{
        width: size, height: size, background: colour,
        boxShadow: ring ? `0 0 0 2px #0e1116, 0 0 0 4px ${ring}` : "0 0 0 2px #ffffff",
      }}
    >
      {children}
    </span>
  )
}

export function PlaceRow({
  disc, title, subtitle, trailing, onClick, active,
}: {
  disc: ReactNode
  title: ReactNode
  subtitle?: ReactNode
  trailing?: ReactNode
  onClick?: () => void
  active?: boolean
}) {
  const body = (
    <>
      {disc}
      <span className="min-w-0 flex-1 text-left">
        <span className="block truncate text-[13px] font-medium">{title}</span>
        {subtitle && <span className="block truncate text-[11.5px] text-muted-foreground">{subtitle}</span>}
      </span>
      {trailing && <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{trailing}</span>}
    </>
  )
  const cls =
    "flex w-full items-center gap-3 rounded-2xl px-2.5 py-2 transition-colors " +
    (active ? "bg-accent ring-1 ring-primary/25" : "bg-muted/60 hover:bg-muted")
  return onClick ? (
    <button type="button" className={cls} onClick={onClick}>{body}</button>
  ) : (
    <div className={cls}>{body}</div>
  )
}

export function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  if (children === null || children === undefined || children === "") return null
  return (
    <div className="flex gap-3 text-xs">
      <span className="w-28 shrink-0 text-muted-foreground/80">{label}</span>
      <span className="min-w-0 flex-1 text-foreground">{children}</span>
    </div>
  )
}

export function ActionButton({
  children, onClick, tone = "neutral", disabled, className = "", href,
}: {
  children: ReactNode
  onClick?: () => void
  tone?: "primary" | "critical" | "neutral" | "danger"
  disabled?: boolean
  className?: string
  href?: string
}) {
  const colours =
    tone === "primary" ? "bg-primary text-primary-foreground hover:bg-primary/90"
    : tone === "critical" ? "bg-[#ef4444] text-white hover:bg-[#ef4444]/90"
    : tone === "danger" ? "bg-red-500/15 text-red-300 hover:bg-red-500/25"
    : "bg-muted text-foreground hover:bg-secondary"
  const cls =
    `inline-flex h-11 items-center justify-center gap-2 rounded-full px-4 text-[13px] font-semibold ` +
    `transition-colors disabled:opacity-50 ${colours} ${className}`
  if (href) {
    return <a className={cls} href={href} target="_blank" rel="noreferrer">{children}</a>
  }
  return <button type="button" className={cls} onClick={onClick} disabled={disabled}>{children}</button>
}

export function BackLink({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick} className="text-xs text-muted-foreground underline-offset-2 hover:underline">
      {children}
    </button>
  )
}
