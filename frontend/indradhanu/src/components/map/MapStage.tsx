import { useEffect, useState, type ReactNode } from "react"
import { Maximize2, Minimize2, PanelRightClose, PanelRightOpen } from "lucide-react"
import { Button } from "@/components/ui/button"
import { MapLegend } from "./MapLegend"

/** The map, and a way to make it the whole screen.
 *
 *  A 520-pixel map of a city is a thumbnail. The moment anything interesting is
 *  happening an operator wants the wall, and the thing they must not lose on the
 *  way there is the narration: a map with no feed is a picture of dots moving,
 *  and the argument this system makes is *why* they moved.
 *
 *  So fullscreen keeps the agent feed, as a panel over the map rather than
 *  beside it, collapsible for when the geography is the point. Escape exits,
 *  because anything that takes over the screen must give it back the way
 *  everything else does.
 *
 *  Implemented as a fixed overlay rather than the Fullscreen API on purpose:
 *  the browser's own fullscreen hides the operating system's clock, and a
 *  control room runs on a shared display where that matters.
 */
export function MapStage({
  map, panel, panelTitle = "What is happening", footer, className,
}: {
  /** Render the map. `expanded` lets the caller pick a taller class. */
  map: (expanded: boolean) => ReactNode
  /** The live feed, shown beside the map when expanded and below when not. */
  panel?: ReactNode
  panelTitle?: string
  footer?: ReactNode
  className?: string
}) {
  const [expanded, setExpanded] = useState(false)
  const [showPanel, setShowPanel] = useState(true)

  useEffect(() => {
    if (!expanded) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setExpanded(false)
    }
    window.addEventListener("keydown", onKey)
    // A fixed overlay over a scrollable page leaves the page scrolling behind
    // it, which on a trackpad is disorienting.
    const previous = document.body.style.overflow
    document.body.style.overflow = "hidden"
    return () => {
      window.removeEventListener("keydown", onKey)
      document.body.style.overflow = previous
    }
  }, [expanded])

  const controls = (
    <div className="pointer-events-auto flex items-center gap-1.5">
      {expanded && panel && (
        <Button
          size="sm"
          variant="secondary"
          className="h-8 gap-1.5 text-xs shadow-lg"
          onClick={() => setShowPanel((v) => !v)}
        >
          {showPanel ? (
            <><PanelRightClose className="size-3.5" /> Hide feed</>
          ) : (
            <><PanelRightOpen className="size-3.5" /> Show feed</>
          )}
        </Button>
      )}
      <Button
        size="sm"
        variant="secondary"
        className="h-8 gap-1.5 text-xs shadow-lg"
        onClick={() => setExpanded((v) => !v)}
        title={expanded ? "Escape also exits" : "Expand the map"}
      >
        {expanded ? (
          <><Minimize2 className="size-3.5" /> Exit full screen</>
        ) : (
          <><Maximize2 className="size-3.5" /> Full screen</>
        )}
      </Button>
    </div>
  )

  if (!expanded) {
    return (
      <div className={`space-y-3 ${className ?? ""}`}>
        <div className="relative">
          {map(false)}
          <div className="absolute right-2 top-2 z-10">{controls}</div>
          <div className="absolute bottom-2 left-2 z-10 max-w-[260px]">
            <MapLegend />
          </div>
        </div>
        {footer}
      </div>
    )
  }

  return (
    <div className="bg-background fixed inset-0 z-50 flex flex-col">
      <div className="relative min-h-0 flex-1">
        {map(true)}

        <div className="absolute right-3 top-3 z-10">{controls}</div>
        <div className="absolute bottom-3 left-3 z-10 max-w-[300px]">
          <MapLegend />
        </div>

        {panel && showPanel && (
          <aside
            /* Scales with the wall it is shown on. A 360px panel is right on a
               laptop and a postage stamp on a 4K control-room display, where
               the whole point of going full screen was that everything got
               bigger — the narration included, not only the dots. */
            className="absolute right-3 top-14 bottom-3 z-10 flex w-[clamp(320px,24vw,520px)]
                       max-w-[calc(100vw-1.5rem)] flex-col overflow-hidden rounded-lg
                       border border-border bg-card/95 shadow-xl
                       backdrop-blur-sm [font-size:clamp(0.75rem,0.62vw,1rem)]"
          >
            <div className="border-b border-border px-3 py-2 text-xs font-medium text-foreground">
              {panelTitle}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2 ">
              {panel}
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}
