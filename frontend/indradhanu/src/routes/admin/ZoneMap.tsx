import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  Ambulance, Building2, CircleQuestionMark, Construction, Droplets, Flame, Helicopter, LifeBuoy,
  Sailboat, Shield, ThermometerSun, TreePine, Truck, Bus, Users,
} from "lucide-react"
import type { DemoState } from "@/routes/demo/useDemo"
import { LiveMap } from "@/components/map/LiveMap"
import { zoneLayers, zoomFor, type Hazard, type Region, type Zone } from "./zones"

/** Shared pieces of the command wall, its zone page and the zone's agent page. */

export const SEV_BAR: Record<number, string> = {
  5: "bg-[#d03b3b]", 4: "bg-[#ec835a]", 3: "bg-[#fab219]", 2: "bg-zinc-400", 1: "bg-zinc-300",
}
export const SEV_CHIP: Record<number, string> = {
  5: "bg-[#d03b3b] text-white", 4: "bg-[#ec835a] text-white", 3: "bg-[#fab219] text-[#3d2a00]",
  2: "bg-zinc-200 text-zinc-700", 1: "bg-zinc-100 text-zinc-600",
}

export const HAZARD_ICON: Record<Hazard, typeof Flame> = {
  flood: Droplets, fire: Flame, structure: Building2, trees: TreePine,
  heat: ThermometerSun, people: Users, other: CircleQuestionMark,
}

export function unitIcon(kind: string): typeof Truck {
  if (/ambulance|medical/.test(kind)) return Ambulance
  if (/fire/.test(kind)) return Flame
  if (/boat/.test(kind)) return Sailboat
  if (/heli/.test(kind)) return Helicopter
  if (/rescue|ndrf|sdrf/.test(kind)) return LifeBuoy
  if (/jcb|excavat|crane|pump|debris/.test(kind)) return Construction
  if (/bus|transport/.test(kind)) return Bus
  if (/police/.test(kind)) return Shield
  return Truck
}

export const people = (n: number | null | undefined) =>
  n == null ? "—" : n >= 100000 ? `${(n / 100000).toFixed(1)} L` : n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n)

/** Build a screen's map the first time it comes near the viewport, then keep it. */
export function useSeenOnce<T extends Element>(margin = "400px") {
  const ref = useRef<T | null>(null)
  const [seen, setSeen] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el || seen) return
    const io = new IntersectionObserver(([e]) => {
      if (e.isIntersecting) {
        setSeen(true)
        io.disconnect()
      }
    }, { rootMargin: margin })
    io.observe(el)
    return () => io.disconnect()
  }, [margin, seen])
  return [ref, seen] as const
}

/** The element's size, for the static map image under a tile. */
export function useSize<T extends HTMLElement>() {
  const ref = useRef<T | null>(null)
  const [size, setSize] = useState({ w: 360, h: 225 })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    let t: ReturnType<typeof setTimeout> | undefined
    const ro = new ResizeObserver(() => {
      clearTimeout(t)
      t = setTimeout(() => setSize({ w: Math.round(el.offsetWidth), h: Math.round(el.offsetHeight) }), 150)
    })
    ro.observe(el)
    setSize({ w: Math.round(el.offsetWidth) || 360, h: Math.round(el.offsetHeight) || 225 })
    return () => {
      clearTimeout(t)
      ro.disconnect()
    }
  }, [])
  return [ref, size] as const
}

/** The world, refreshed at most every `ms`. Small screens do not need the
 *  once-a-second poll, and re-feeding many maps every second made them stutter. */
export function useThrottledState(state: DemoState, ms: number, resetKey = ""): DemoState {
  const latest = useRef(state)
  const [slow, setSlow] = useState(state)
  const [key, setKey] = useState(resetKey)
  if (key !== resetKey) {
    setKey(resetKey)
    setSlow(state)
  }
  useEffect(() => {
    latest.current = state
  }, [state])
  useEffect(() => {
    const id = setInterval(() => setSlow(latest.current), ms)
    return () => clearInterval(id)
  }, [ms])
  return slow
}

/** One real, interactive map: the whole region, or one zone and what is near it. */
export function ScreenMap({ state, zone, region, big, className, onPickWard }: {
  state: DemoState; zone: Zone | null; region?: Region | null; big?: boolean; className: string
  onPickWard?: (id: string) => void
}) {
  const navigate = useNavigate()
  const pick = useCallback((id: string) => navigate(`/admin/response?incident=${id}`), [navigate])
  const layers = useMemo(() => (zone ? zoneLayers(state, zone) : null), [state, zone])
  if (!zone || !layers) {
    return (
      <LiveMap
        className={className}
        wards={state.wards}
        incidents={state.incidents}
        resources={state.resources}
        facilities={state.facilities}
        blocks={state.roadBlocks}
        needs={state.needs}
        routes={state.routes}
        onPickIncident={pick}
        onPickWard={onPickWard}
        key={region?.id ?? "all"}
        center={region?.center}
        zoom={region ? region.zoom + (big ? 0.6 : 0) : big ? 11.8 : 10.9}
      />
    )
  }
  return (
    <LiveMap
      key={`${zone.id}-${big ? "big" : "tile"}`}
      className={className}
      wards={layers.wards}
      incidents={layers.incidents}
      resources={layers.resources}
      facilities={layers.facilities}
      blocks={layers.blocks}
      needs={layers.needs}
      routes={layers.routes}
      onPickIncident={pick}
      onPickWard={onPickWard}
      center={zone.center}
      zoom={zoomFor(zone, big)}
    />
  )
}
