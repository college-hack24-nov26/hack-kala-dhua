import type { ComponentType, ReactNode } from "react"
import { cn } from "@/lib/utils"

/** A rolling history of one number, kept per key for the life of the tab.
 *
 *  The console polls the world every few seconds; a KPI that only shows its
 *  current value hides whether it is rising or falling. This keeps the last
 *  `max` readings (at most one every `everyMs`) so a card can draw its trend
 *  without the API having to send one. */
const SERIES = new Map<string, { at: number; v: number[] }>()

export function useSeries(key: string, value: number | null | undefined, max = 30, everyMs = 4000): number[] {
  if (value == null || !Number.isFinite(value)) return SERIES.get(key)?.v ?? []
  const now = Date.now()
  const s = SERIES.get(key)
  if (!s) {
    SERIES.set(key, { at: now, v: [value] })
  } else if (now - s.at >= everyMs) {
    s.v = [...s.v, value].slice(-max)
    s.at = now
  } else if (s.v.length) {
    s.v = [...s.v.slice(0, -1), value]
  }
  return SERIES.get(key)!.v
}

/** A small trend line. Nothing is drawn until there are two points. */
export function Sparkline({ data, className, tone = "primary" }: {
  data: number[]; className?: string; tone?: "primary" | "warn" | "bad"
}) {
  if (data.length < 2) return <div className={cn("h-8", className)} />
  const w = 96, h = 32, pad = 3
  const lo = Math.min(...data), hi = Math.max(...data)
  const span = hi - lo || 1
  const pts = data.map((v, i) => [
    pad + (i / (data.length - 1)) * (w - 2 * pad),
    h - pad - ((v - lo) / span) * (h - 2 * pad),
  ])
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ")
  const colour = tone === "bad" ? "var(--sev-5)" : tone === "warn" ? "var(--sev-3)" : "var(--primary)"
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className={cn("h-8 w-24", className)} aria-hidden>
      <path d={d} fill="none" stroke={colour} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** One headline number: a quiet label, the value large, an optional line under
 *  it, and an optional trend. The same card on every screen. */
export function StatCard({
  label, value, sub, icon: Icon, series, tone, onClick, className, children,
}: {
  label: ReactNode
  value: ReactNode
  sub?: ReactNode
  icon?: ComponentType<{ className?: string }>
  series?: number[]
  tone?: "warn" | "bad"
  onClick?: () => void
  className?: string
  children?: ReactNode
}) {
  const Comp = onClick ? "button" : "div"
  return (
    <Comp
      onClick={onClick}
      className={cn(
        "flex min-w-0 flex-col gap-1 rounded-xl border bg-card p-4 text-left shadow-card",
        onClick && "transition-colors hover:border-primary/50",
        tone === "warn" && "border-amber-400/70",
        tone === "bad" && "border-red-400/70",
        className,
      )}
    >
      <div className="flex items-center gap-2">
        <span className="truncate text-xs font-medium text-muted-foreground">{label}</span>
        {Icon && (
          <span className="ml-auto flex size-7 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-foreground">
            <Icon className="size-3.5" />
          </span>
        )}
      </div>
      <div className="flex items-end justify-between gap-2">
        <div className={cn(
          "truncate font-semibold tracking-tight tabular-nums",
          typeof value === "string" && value.length > 12 ? "text-lg" : "text-2xl",
          tone === "bad" && "text-red-600 dark:text-red-400",
        )}>
          {value}
        </div>
        {series && <Sparkline data={series} tone={tone} className="shrink-0" />}
      </div>
      {sub && <div className="truncate text-xs text-muted-foreground">{sub}</div>}
      {children}
    </Comp>
  )
}
