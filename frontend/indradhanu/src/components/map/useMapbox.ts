import { useEffect, useRef, useState } from "react"
import mapboxgl from "mapbox-gl"
import "mapbox-gl/dist/mapbox-gl.css"

const TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined

/** One plain Mapbox map for a page that draws its own layers (the arrival-time
 *  view, the evidence trail). Light or dark basemap from the app theme; resizes
 *  with its box. The map exists once `ready` is true. */
export function useMapbox({ center, zoom, pitch = 0 }: { center: [number, number]; zoom: number; pitch?: number }) {
  const ref = useRef<HTMLDivElement | null>(null)
  const map = useRef<mapboxgl.Map | null>(null)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState<string | null>(TOKEN ? null : "VITE_MAPBOX_TOKEN is not set in frontend/indradhanu/.env.local")

  useEffect(() => {
    if (!TOKEN || !ref.current || map.current) return
    mapboxgl.accessToken = TOKEN
    const dark = document.documentElement.classList.contains("dark")
    let m: mapboxgl.Map
    try {
      m = new mapboxgl.Map({
        container: ref.current,
        style: dark ? "mapbox://styles/mapbox/dark-v11" : "mapbox://styles/mapbox/light-v11",
        center, zoom, pitch, antialias: true, attributionControl: true,
        fadeDuration: 0, projection: "mercator",
      } as mapboxgl.MapOptions)
    } catch (e) {
      setFailed(`The map could not start: ${(e as Error).message}`)
      return
    }
    map.current = m
    m.addControl(new mapboxgl.NavigationControl({ visualizePitch: true }), "top-right")
    m.on("load", () => setReady(true))
    m.on("error", (e) => {
      // Only a refused token is fatal; a tile that fails to load is not.
      const status = (e as { error?: { status?: number } }).error?.status
      if (status === 401 || status === 403) setFailed("The Mapbox token was refused (HTTP " + status + ").")
    })
    const ro = new ResizeObserver(() => m.resize())
    ro.observe(ref.current)
    return () => {
      ro.disconnect()
      m.remove()
      map.current = null
      setReady(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { ref, map, ready, failed }
}

/** Set (or create) a GeoJSON source's data. */
export function setSource(m: mapboxgl.Map, id: string, data: GeoJSON.FeatureCollection) {
  const s = m.getSource(id) as mapboxgl.GeoJSONSource | undefined
  if (s) s.setData(data)
  else m.addSource(id, { type: "geojson", data })
}

/** A line drawn as a thin polygon `halfWidthM` either side, so it can be extruded. */
export function ribbon(coords: [number, number][], halfWidthM: number): GeoJSON.MultiPolygon {
  const polys: [number, number][][][] = []
  for (let k = 0; k < coords.length - 1; k++) {
    const [x0, y0] = coords[k], [x1, y1] = coords[k + 1]
    const kx = 111320 * Math.cos((y0 * Math.PI) / 180), ky = 110540
    const dx = (x1 - x0) * kx, dy = (y1 - y0) * ky
    const len = Math.hypot(dx, dy) || 1
    const nx = (-dy / len) * halfWidthM / kx, ny = (dx / len) * halfWidthM / ky
    polys.push([[[x0 + nx, y0 + ny], [x1 + nx, y1 + ny], [x1 - nx, y1 - ny], [x0 - nx, y0 - ny], [x0 + nx, y0 + ny]]])
  }
  return { type: "MultiPolygon", coordinates: polys }
}

/** A small square around a point, `halfM` metres from centre to edge. */
export function square([x, y]: [number, number], halfM: number): GeoJSON.Polygon {
  const dx = halfM / (111320 * Math.cos((y * Math.PI) / 180)), dy = halfM / 110540
  return { type: "Polygon", coordinates: [[[x - dx, y - dy], [x + dx, y - dy], [x + dx, y + dy], [x - dx, y + dy], [x - dx, y - dy]]] }
}
