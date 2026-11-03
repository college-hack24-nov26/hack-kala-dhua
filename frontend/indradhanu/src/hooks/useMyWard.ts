import { useCallback, useEffect, useState } from "react"
import { request } from "@/api/httpClient"

export type Ward = {
  id: string
  cityId: string
  number: string
  name: string
  centroid: [number, number]
  boundary: [number, number][]
  population: number
  elderlyShare: number
  elevationM: number
  areaSqKm: number
}

export type WardLocation = {
  inside: boolean
  ward: Ward | null
  distanceKm: number
  cityId: string
  note: string
}

type State = {
  status: "idle" | "locating" | "resolving" | "ready" | "denied" | "error"
  coords: { lng: number; lat: number; accuracyM: number } | null
  location: WardLocation | null
  message: string
}

/** The resident's real position, resolved to a ward.
 *
 *  Two failures are handled properly because both are ordinary rather than
 *  exceptional:
 *
 *  - **Permission denied.** Most people decline location the first time. That
 *    is not an error state to sit in; the caller falls back to letting them
 *    pick a ward by name.
 *  - **Outside coverage.** The API answers `inside: false` with the nearest
 *    ward and the distance. We surface that rather than silently showing
 *    someone another area's flood risk as if it were theirs.
 *
 *  `watch` keeps the fix current, which matters during an evacuation: the
 *  answer to "where am I and what should I do" changes while you are moving.
 */
export function useMyWard(options: { watch?: boolean; cityId?: string } = {}) {
  const { watch = false, cityId = "pune" } = options
  const [state, setState] = useState<State>({
    status: "idle",
    coords: null,
    location: null,
    message: "",
  })

  const resolve = useCallback(
    async (lng: number, lat: number, accuracyM: number) => {
      setState((s) => ({ ...s, status: "resolving", coords: { lng, lat, accuracyM } }))
      try {
        const location = await request<WardLocation>("/wards/locate", {
          query: { lng, lat, cityId },
        })
        setState({
          status: "ready",
          coords: { lng, lat, accuracyM },
          location,
          message: location.note,
        })
      } catch (err) {
        setState((s) => ({
          ...s,
          status: "error",
          message: err instanceof Error ? err.message : "Could not resolve your ward.",
        }))
      }
    },
    [cityId]
  )

  const locate = useCallback(() => {
    if (!("geolocation" in navigator)) {
      setState((s) => ({
        ...s,
        status: "error",
        message: "This browser cannot report your location.",
      }))
      return
    }
    setState((s) => ({ ...s, status: "locating", message: "" }))

    const onOk = (pos: GeolocationPosition) =>
      void resolve(pos.coords.longitude, pos.coords.latitude, pos.coords.accuracy)

    const onErr = (err: GeolocationPositionError) =>
      setState((s) => ({
        ...s,
        status: err.code === err.PERMISSION_DENIED ? "denied" : "error",
        message:
          err.code === err.PERMISSION_DENIED
            ? "Location is off, so pick your area below instead."
            : "Could not get a location fix. Pick your area below instead.",
      }))

    const opts: PositionOptions = {
      enableHighAccuracy: true,
      timeout: 12_000,
      maximumAge: 30_000,
    }

    if (watch) {
      const id = navigator.geolocation.watchPosition(onOk, onErr, opts)
      return () => navigator.geolocation.clearWatch(id)
    }
    navigator.geolocation.getCurrentPosition(onOk, onErr, opts)
    return undefined
  }, [resolve, watch])

  useEffect(() => {
    const cleanup = locate()
    return cleanup
  }, [locate])

  return { ...state, retry: locate, resolveAt: resolve }
}
