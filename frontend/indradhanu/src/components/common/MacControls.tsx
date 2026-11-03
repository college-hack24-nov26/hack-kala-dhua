import type { ComponentType, ReactNode } from "react"
import { cn } from "@/lib/utils"

/** macOS-style controls: a gray track with the chosen item raised on a white
 *  key, and pill toggles for filters that can be combined. */

export type SegOption<T extends string> = {
  value: T
  label: ReactNode
  icon?: ComponentType<{ className?: string }>
  count?: number
  title?: string
}

export function Segmented<T extends string>({
  value, onChange, options, size = "md", className, ariaLabel,
}: {
  value: T
  onChange: (v: T) => void
  options: SegOption<T>[]
  size?: "sm" | "md"
  className?: string
  ariaLabel?: string
}) {
  return (
    <div
      role="radiogroup"
      aria-label={ariaLabel}
      className={cn("inline-flex items-center gap-0.5 rounded-[10px] bg-muted p-[3px] ring-1 ring-black/[0.03]", className)}
    >
      {options.map((o) => {
        const on = o.value === value
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={on}
            title={o.title}
            onClick={() => onChange(o.value)}
            className={cn(
              "inline-flex items-center gap-1.5 whitespace-nowrap rounded-[8px] font-medium transition-all",
              size === "sm" ? "h-7 px-2.5 text-xs" : "h-8 px-3 text-[13px]",
              on
                ? "bg-card text-foreground shadow-[0_1px_2px_rgb(16_24_40/0.08),0_0_0_0.5px_rgb(16_24_40/0.06)]"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {o.icon && <o.icon className="size-3.5" />}
            {o.label}
            {o.count != null && (
              <span className={cn("tabular-nums text-[11px]", on ? "text-muted-foreground" : "opacity-70")}>{o.count}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}

/** A filter chip. `dot` shows a status colour; `icon` a glyph. */
export function Pill({
  on, onClick, children, icon: Icon, dot, count, title,
}: {
  on: boolean
  onClick: () => void
  children: ReactNode
  icon?: ComponentType<{ className?: string }>
  dot?: string
  count?: number
  title?: string
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      title={title}
      onClick={onClick}
      className={cn(
        "inline-flex h-8 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 text-[13px] font-medium transition-colors",
        on
          ? "border-primary/30 bg-accent text-accent-foreground"
          : "border-border bg-card text-muted-foreground hover:bg-muted hover:text-foreground",
        count === 0 && !on && "opacity-55",
      )}
    >
      {dot && <span className={cn("size-2 rounded-full", dot)} />}
      {Icon && <Icon className="size-3.5" />}
      {children}
      {count != null && <span className="tabular-nums text-[11px] opacity-70">{count}</span>}
    </button>
  )
}

/** A circular gauge for a 0-100 score. */
export function RiskRing({ value, size = 44, stroke = 4, className, label = true }: {
  value: number; size?: number; stroke?: number; className?: string; label?: boolean
}) {
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const v = Math.max(0, Math.min(100, value))
  const colour = v >= 75 ? "#d03b3b" : v >= 55 ? "#ec835a" : v >= 35 ? "#eaa21a" : "#3b6fe0"
  return (
    <div className={cn("relative inline-grid place-items-center", className)} style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="currentColor" strokeWidth={stroke} className="text-black/10 dark:text-white/15" />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={colour} strokeWidth={stroke}
                strokeLinecap="round" strokeDasharray={`${(v / 100) * c} ${c}`} />
      </svg>
      {label && (
        <span className="absolute text-center font-semibold tabular-nums leading-none" style={{ fontSize: size * 0.3 }}>
          {Math.round(v)}
        </span>
      )}
    </div>
  )
}
