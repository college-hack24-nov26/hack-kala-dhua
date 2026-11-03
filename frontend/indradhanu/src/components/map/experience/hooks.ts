import { useEffect, useState } from "react"

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia(query).matches
  )
  useEffect(() => {
    const mql = window.matchMedia(query)
    const on = () => setMatches(mql.matches)
    mql.addEventListener("change", on)
    return () => mql.removeEventListener("change", on)
  }, [query])
  return matches
}

/** True on devices whose main pointer can hover — where hover cards make sense. */
export function useCanHover(): boolean {
  return useMediaQuery("(hover: hover) and (pointer: fine)")
}
