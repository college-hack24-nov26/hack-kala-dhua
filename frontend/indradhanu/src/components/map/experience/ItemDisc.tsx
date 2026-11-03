import {
  Ambulance, Building2, Droplets, Flame, Hospital, House, LifeBuoy, OctagonX, Ship,
  Stethoscope, TriangleAlert, Truck, Utensils, Zap,
} from "lucide-react"
import { Disc } from "./parts"
import { statusRing } from "../mapTheme"
import type { MapItem } from "./items"

function Glyph({ item, size }: { item: MapItem; size: number }) {
  const cls = "text-white"
  const s = { width: size, height: size }
  if (item.kind === "block") return <OctagonX className={cls} style={s} />
  if (item.kind === "incident") {
    return item.sub === "fire" ? <Flame className={cls} style={s} /> : <TriangleAlert className={cls} style={s} />
  }
  if (item.kind === "unit") {
    switch (item.sub) {
      case "ambulance": return <Ambulance className={cls} style={s} />
      case "fire_engine": return <Flame className={cls} style={s} />
      case "boat": return <Ship className={cls} style={s} />
      case "rescue_team": return <LifeBuoy className={cls} style={s} />
      default: return <Truck className={cls} style={s} />
    }
  }
  switch (item.sub) {
    case "hospital": return <Hospital className={cls} style={s} />
    case "medical_camp": return <Stethoscope className={cls} style={s} />
    case "food_kitchen":
    case "relief_centre": return <Utensils className={cls} style={s} />
    case "water_point": return <Droplets className={cls} style={s} />
    case "substation": return <Zap className={cls} style={s} />
    case "shelter":
    case "school": return <House className={cls} style={s} />
    default: return <Building2 className={cls} style={s} />
  }
}

export function ItemDisc({ item, size = 32 }: { item: MapItem; size?: number }) {
  return (
    <Disc
      colour={item.colour}
      size={size}
      ring={item.kind === "unit" && item.status ? statusRing(item.status) : undefined}
    >
      <Glyph item={item} size={Math.round(size * 0.5)} />
    </Disc>
  )
}
