import { useState } from "react"
import { ChevronDown, ChevronUp, Info } from "lucide-react"

/** What the colours mean.
 *
 *  The map carries four separate colour scales at once — ward severity, hazard
 *  severity, unit status and facility kind — and without this they are four
 *  things somebody has to be told in person. A control room screen that needs a
 *  briefing before it can be read is not finished.
 *
 *  Collapsible, and collapsed is a legitimate default once somebody knows the
 *  map: this should be available, not permanent furniture.
 */

const WARD = [
  ["Not scored", "rgba(100,116,139,0.35)"],
  ["Severity 2", "rgba(56,189,248,0.45)"],
  ["Severity 3", "rgba(250,204,21,0.55)"],
  ["Severity 4", "rgba(249,115,22,0.65)"],
  ["Severity 5", "rgba(239,68,68,0.75)"],
] as const

const UNIT = [
  ["Available", "#64748b"],
  ["Assigned", "#0ea5e9"],
  ["En route", "#f59e0b"],
  ["On scene", "#10b981"],
  ["Offline", "#dc2626"],
] as const

const HAZARD = [
  ["Severity 3", "#eab308"],
  ["Severity 4", "#f97316"],
  ["Severity 5", "#ef4444"],
] as const

const PLACE = [
  ["Hospital", "#0284c7"],
  ["Shelter", "#0d9488"],
  ["Relief centre, kitchen", "#7c3aed"],
  ["Water point", "#0891b2"],
  ["Medical camp", "#db2777"],
  ["Pump station, school, substation", "#475569"],
] as const

function Swatch({ colour, round = true }: { colour: string; round?: boolean }) {
  return (
    <span
      className={`inline-block size-3 shrink-0 border border-black/10 ${
        round ? "rounded-full" : "rounded-[2px]"
      }`}
      style={{ background: colour }}
    />
  )
}

function Group({
  title, items, round = true,
}: {
  title: string
  items: readonly (readonly [string, string])[]
  round?: boolean
}) {
  return (
    <div>
      <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
        {title}
      </div>
      <div className="space-y-0.5">
        {items.map(([label, colour]) => (
          <div key={label} className="flex items-center gap-1.5 text-[11px] text-foreground">
            <Swatch colour={colour} round={round} />
            <span className="truncate">{label}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function MapLegend({ className = "" }: { className?: string }) {
  const [open, setOpen] = useState(false)

  return (
    <div
      className={`pointer-events-auto rounded-lg border border-border bg-card/95 text-foreground shadow-card backdrop-blur-sm ${className}`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-medium text-foreground"
      >
        <Info className="size-3 shrink-0" />
        Legend
        {open ? (
          <ChevronDown className="ml-auto size-3" />
        ) : (
          <ChevronUp className="ml-auto size-3" />
        )}
      </button>

      {open && (
        <div className="grid gap-3 border-t border-border px-2.5 py-2 sm:grid-cols-2">
          <Group title="Ward risk (fill)" items={WARD} round={false} />
          <Group title="Hazard severity" items={HAZARD} />
          <Group title="Unit status" items={UNIT} />
          <Group title="Facility kind" items={PLACE} />

          <div className="sm:col-span-2">
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
              Lines and marks
            </div>
            <div className="space-y-0.5 text-[11px] text-foreground">
              <div className="flex items-center gap-1.5">
                <span className="inline-block h-[3px] w-4 shrink-0 rounded bg-[#f59e0b]" />
                <span>A unit driving to its task, on real streets</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="inline-block h-[3px] w-4 shrink-0 rounded bg-[#22c55e]" />
                <span>Route the citizen agent gave a resident</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="inline-block size-3 shrink-0 rounded-full border-2 border-[#f59e0b]" />
                <span>Where a route ends — the hazard it is going to</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="inline-block size-3 shrink-0 rounded-full border-2 border-[#ef4444]" />
                <span>Facility that has reported itself full or closed</span>
              </div>
              <div className="flex items-center gap-1.5 text-muted-foreground">
                <span className="w-4 shrink-0 text-center tabular-nums">3</span>
                <span>Reports merged into one incident</span>
              </div>
              <div className="flex items-center gap-1.5 text-muted-foreground">
                <span className="w-4 shrink-0 text-center">◎</span>
                <span>A bigger halo is more reports, not more severity</span>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
