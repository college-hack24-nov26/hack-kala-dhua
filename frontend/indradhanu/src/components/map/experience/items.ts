import {
  CRITICAL_SEVERITY, MAP, SERVICE_LABEL, incidentColour, placeColour, serviceColour,
  serviceOf,
} from "../mapTheme"

/** One thing on the map, as the list and the detail view see it. The disc
 *  colour and glyph match the marker, so a row and its pin read as one. */
export type MapItem = {
  key: string
  kind: "incident" | "facility" | "unit" | "block"
  id: string
  /** The incident category, facility kind or unit kind. */
  sub: string
  title: string
  location: [number, number]
  colour: string
  critical: boolean
  severity?: number
  status?: string
}

export const itemKey = (kind: MapItem["kind"], id: string) => `${kind}:${id}`

export const words = (s: string) => s.replace(/_/g, " ")

export function incidentItem(i: {
  id: string; title: string; category: string; severity: number; location: [number, number]
}): MapItem {
  return {
    key: itemKey("incident", i.id), kind: "incident", id: i.id, sub: i.category,
    title: i.title, location: i.location, colour: incidentColour(i.severity),
    critical: i.severity >= CRITICAL_SEVERITY, severity: i.severity,
  }
}

export function facilityItem(f: {
  id: string; name: string; kind: string; status: string; location: [number, number]
}): MapItem {
  return {
    key: itemKey("facility", f.id), kind: "facility", id: f.id, sub: f.kind,
    title: f.name, location: f.location, colour: placeColour(f.kind), critical: false,
    status: f.status,
  }
}

export function unitItem(u: {
  id: string; kind: string; label: string; status: string; location: [number, number]
}): MapItem {
  return {
    key: itemKey("unit", u.id), kind: "unit", id: u.id, sub: u.kind,
    title: u.label, location: u.location, colour: serviceColour(u.kind), critical: false,
    status: u.status,
  }
}

export function blockItem(b: { id: string; reason: string; location: [number, number] }): MapItem {
  return {
    key: itemKey("block", b.id), kind: "block", id: b.id, sub: "road_block",
    title: b.reason || "Road blocked", location: b.location, colour: MAP.block, critical: false,
  }
}

export function kindLabel(item: MapItem): string {
  if (item.kind === "unit") return SERVICE_LABEL[serviceOf(item.sub)] === "Logistics" ? words(item.sub) : SERVICE_LABEL[serviceOf(item.sub)]
  if (item.kind === "block") return "Road blocked"
  if (item.kind === "incident") return item.critical ? `Critical · ${words(item.sub)}` : words(item.sub)
  return words(item.sub)
}
