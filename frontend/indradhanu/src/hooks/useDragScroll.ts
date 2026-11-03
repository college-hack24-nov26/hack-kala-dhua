import { useEffect, useRef } from "react"

/** Grab-to-scroll for a wide figure inside an overflow container: press and drag pans it,
 *  a click without movement still reaches what is under the pointer. */
export function useDragScroll<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    let down = false, moved = 0, sx = 0, sy = 0, sl = 0, st = 0, id = -1
    el.style.cursor = "grab"
    const onDown = (e: PointerEvent) => {
      if (e.button !== 0) return
      down = true; moved = 0; sx = e.clientX; sy = e.clientY; sl = el.scrollLeft; st = el.scrollTop; id = e.pointerId
    }
    const onMove = (e: PointerEvent) => {
      if (!down) return
      const dx = e.clientX - sx, dy = e.clientY - sy
      moved = Math.abs(dx) + Math.abs(dy)
      if (moved > 4) {
        if (!el.hasPointerCapture(id)) { el.setPointerCapture(id); el.style.cursor = "grabbing"; el.style.userSelect = "none" }
        el.scrollLeft = sl - dx; el.scrollTop = st - dy
      }
    }
    const onUp = () => { down = false; el.style.cursor = "grab"; el.style.userSelect = "" }
    const onClick = (e: MouseEvent) => { if (moved > 4) { e.stopPropagation(); e.preventDefault() } }
    el.addEventListener("pointerdown", onDown)
    el.addEventListener("pointermove", onMove)
    el.addEventListener("pointerup", onUp)
    el.addEventListener("pointercancel", onUp)
    el.addEventListener("click", onClick, true)
    return () => {
      el.removeEventListener("pointerdown", onDown); el.removeEventListener("pointermove", onMove)
      el.removeEventListener("pointerup", onUp); el.removeEventListener("pointercancel", onUp)
      el.removeEventListener("click", onClick, true)
    }
  }, [])
  return ref
}
