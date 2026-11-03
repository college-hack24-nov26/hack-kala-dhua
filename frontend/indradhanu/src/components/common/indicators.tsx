import type { HazardType, Severity } from "@/api/types"
import { cn } from "@/lib/utils"
import { hazardBg, hazardLabel, severityBg, severityLabel } from "@/lib/tokens"

export function SeverityBadge({
  severity,
  className,
  showLabel = true,
}: {
  severity: Severity
  className?: string
  showLabel?: boolean
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-semibold whitespace-nowrap",
        severityBg[severity],
        className
      )}
    >
      <span className="tabular">{severity}</span>
      {showLabel && <span>{severityLabel[severity]}</span>}
    </span>
  )
}

export function HazardChip({
  hazard,
  className,
}: {
  hazard: HazardType
  className?: string
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-xs font-medium",
        className
      )}
    >
      <span className={cn("size-2.5 rounded-full", hazardBg[hazard])} />
      {hazardLabel[hazard]}
    </span>
  )
}

/** A compact score meter. Solid fill, no gradient. */
export function ScoreBar({
  score,
  severity,
}: {
  score: number
  severity: Severity
}) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-muted">
        <div
          className={cn(
            "h-full rounded-full",
            severityBg[severity].split(" ")[0]
          )}
          style={{ width: `${Math.round(score * 100)}%` }}
        />
      </div>
      <span className="tabular text-xs font-medium">
        {Math.round(score * 100)}
      </span>
    </div>
  )
}

/** Twelve-hour risk projection, drawn as a bar sparkline. */
export function Sparkline({
  values,
  severity,
  className,
}: {
  values: number[]
  severity: Severity
  className?: string
}) {
  const max = Math.max(...values, 0.01)
  return (
    <div className={cn("flex h-6 items-end gap-[2px]", className)}>
      {values.map((v, i) => (
        <div
          key={i}
          className={cn(
            "w-[3px] rounded-sm",
            severityBg[severity].split(" ")[0]
          )}
          style={{
            height: `${Math.max(8, (v / max) * 100)}%`,
            opacity: i === 5 ? 1 : 0.55,
          }}
        />
      ))}
    </div>
  )
}

export function StatTile({
  label,
  value,
  sub,
  className,
}: {
  label: string
  value: string
  sub?: string
  className?: string
}) {
  return (
    <div className={cn("rounded-lg border bg-card p-4", className)}>
      <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </div>
      <div className="tabular mt-1.5 text-2xl font-semibold">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
    </div>
  )
}
