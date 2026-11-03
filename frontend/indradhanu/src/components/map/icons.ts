/** Map icons, drawn as SVG and registered with Mapbox as images.
 *
 *  The symbol layers used two-letter codes, which was a correct fix for the
 *  `glyphs > 65535` emoji crash and a poor answer to "what is that dot". An
 *  operator scanning a city at a glance needs shape, not text: a tree is a tree
 *  at any zoom and "FT" is a puzzle at every one.
 *
 *  Each icon is authored once here as an SVG string with a `{c}` placeholder for
 *  its fill, rasterised at 3× into an ImageBitmap, and added with `map.addImage`
 *  under a stable name. Mapbox then handles them as ordinary sprite images:
 *  `icon-image` picks one per feature, `icon-size` scales it, and because they
 *  are raster images rather than SDF glyphs there is no codepoint limit to run
 *  into again.
 *
 *  Two properties worth keeping if these are edited:
 *
 *    * **Authored on a 24×24 grid, centred.** `pin()` scales each glyph into
 *      the pin head; a glyph drawn off-centre sits off-centre in every pin.
 *    * **Solid silhouettes with a white keyline, no thin strokes.** These are
 *      drawn at roughly 18 screen pixels over a dark, busy basemap. A one-pixel
 *      outline disappears; a filled shape does not.
 */

type Svg = string

/** The pin body: a 24-wide head tapering to a point at (12, 31).
 *
 *  A disc centred on the coordinate is ambiguous about *which* pixel it means,
 *  and at the sizes an operator actually wants it covers the junction it is
 *  reporting. A pin has a tip: the tip is the place, the head is the label, and
 *  the head can therefore be large without hiding anything underneath it.
 *
 *  Every symbol layer that uses these must set `icon-anchor: "bottom"`, or the
 *  pin will be drawn centred and the tip will float above its own incident.
 */
const PIN_BODY =
  "M12 .9C6 .9 1.2 5.7 1.2 11.6c0 7.8 9.1 18.4 10.2 19.6a.8.8 0 0 0 1.2 0c" +
  "1.1-1.2 10.2-11.8 10.2-19.6C22.8 5.7 18 .9 12 .9Z"

const wrap = (body: Svg): Svg =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 32" width="24" height="32">${body}</svg>`

/** The coloured pin, with its glyph scaled down into the head.
 *
 *  The glyphs below are all authored on a 24×24 grid centred at (12,12); the
 *  transform drops them into the pin head at (12, 11.4) without any of them
 *  having to know they are inside a pin.
 */
const pin = (c: string, glyph: Svg) =>
  `<path d="${PIN_BODY}" fill="${c}" stroke="#ffffff" stroke-width="1.7"/>` +
  `<circle cx="12" cy="11.4" r="8.4" fill="#0b1220" opacity=".18"/>` +
  `<g transform="translate(12 11.4) scale(.68) translate(-12 -12)">${glyph}</g>`

const g = (body: Svg) => `<g fill="#ffffff" fill-rule="evenodd">${body}</g>`

// ---------------------------------------------------------------- hazards ---
// Drawn to be recognisable at a glance and distinct in silhouette from each
// other, which matters more than realism: fire, tree and wave must not be
// confusable when there are forty of them on screen.

const FIRE = g(
  `<path d="M12 4.2c.9 2.1.3 3.4-.7 4.6-1.2 1.4-2.6 2.7-2.6 4.9a3.3 3.3 0 0 0 1.6 2.9c-.3-.9-.2-1.9.6-2.7.5.9 1.3 1.4 2.2 2 1 .7 1.6 1.5 1.5 2.7a3.9 3.9 0 0 0 2.3-3.5c0-2.3-1.3-3.6-2.3-5.1-.7-1-1-1.9-.9-3-.6.5-1 1.2-1.2 2-.6-1.7-.6-3.3-.5-4.8Z"/>` +
  `<path d="M11.2 15.6c-.7.7-.9 1.6-.5 2.4.3.6 1 1 1.8 1 1 0 1.8-.7 1.8-1.6 0-.8-.5-1.3-1.2-1.8-.6-.4-1.2-.8-1.5-1.4-.3.5-.4 1-.4 1.4Z"/>`
)

const TREE = g(
  `<path d="M12 3.4 7.6 9.2h2L6.4 14h3.3l-2 2.9h9L14.6 14h3.3l-3.2-4.8h2L12 3.4Z"/>` +
  `<rect x="11" y="16.6" width="2" height="4.2" rx=".6"/>`
)

const WAVE = g(
  `<path d="M3.2 9.8c1.6 0 1.6 1.5 3.2 1.5s1.6-1.5 3.2-1.5 1.6 1.5 3.2 1.5 1.6-1.5 3.2-1.5 1.6 1.5 3.2 1.5v2.1c-1.6 0-1.6-1.5-3.2-1.5s-1.6 1.5-3.2 1.5-1.6-1.5-3.2-1.5-1.6 1.5-3.2 1.5-1.6-1.5-3.2-1.5Z"/>` +
  `<path d="M3.2 14.6c1.6 0 1.6 1.5 3.2 1.5s1.6-1.5 3.2-1.5 1.6 1.5 3.2 1.5 1.6-1.5 3.2-1.5 1.6 1.5 3.2 1.5v2.1c-1.6 0-1.6-1.5-3.2-1.5s-1.6 1.5-3.2 1.5-1.6-1.5-3.2-1.5-1.6 1.5-3.2 1.5-1.6-1.5-3.2-1.5Z" opacity=".75"/>` +
  `<path d="M8.6 6.4h6.8v1.9H8.6z" opacity=".9"/>`
)

const PUDDLE = g(
  `<ellipse cx="12" cy="15.4" rx="7.6" ry="3.6"/>` +
  `<path d="M9 9.2a3 3 0 1 0 6 0c0-1.7-3-4.9-3-4.9S9 7.5 9 9.2Z" opacity=".85"/>`
)

const BOLT = g(
  `<path d="M13.6 3 6.8 13.1h3.9L9.9 21l7.3-10.6h-4.1L13.6 3Z"/>`
)

const CRACK = g(
  `<path d="M4.4 19.6 9.9 4l1.9 5.6 2.4-2 .6 4.4 3.1-1.2-1.6 3.5 3.3.4-4.6 5.5H4.4Z"/>`
)

const DRAIN = g(
  `<rect x="4.4" y="6.6" width="15.2" height="10.8" rx="1.6"/>` +
  `<g fill="#0b1220"><rect x="6.6" y="8.8" width="1.6" height="6.4" rx=".6"/>` +
  `<rect x="9.6" y="8.8" width="1.6" height="6.4" rx=".6"/>` +
  `<rect x="12.6" y="8.8" width="1.6" height="6.4" rx=".6"/>` +
  `<rect x="15.6" y="8.8" width="1.6" height="6.4" rx=".6"/></g>`
)

const PERSON = g(
  `<circle cx="12" cy="5.6" r="2.6"/>` +
  `<path d="M8.2 20.4v-5.2l-1.9 2a1.2 1.2 0 1 1-1.8-1.6l3.3-3.6a3 3 0 0 1 2.2-1h4a3 3 0 0 1 2.2 1l3.3 3.6a1.2 1.2 0 1 1-1.8 1.6l-1.9-2v5.2a1.3 1.3 0 0 1-2.6 0v-3.2h-2.4v3.2a1.3 1.3 0 0 1-2.6 0Z"/>`
)

const HEAT = g(
  `<circle cx="12" cy="12" r="4"/>` +
  `<g stroke="#ffffff" stroke-width="1.8" stroke-linecap="round">` +
  `<path d="M12 2.6v2.6M12 18.8v2.6M2.6 12h2.6M18.8 12h2.6"/>` +
  `<path d="M5.4 5.4 7.2 7.2M16.8 16.8l1.8 1.8M18.6 5.4 16.8 7.2M7.2 16.8 5.4 18.6"/></g>`
)

const BOX = g(
  `<path d="M12 3.2 3.6 7v10L12 20.8 20.4 17V7L12 3.2Zm0 2.3 5.6 2.5L12 10.5 6.4 8 12 5.5Z"/>` +
  `<path d="M4.8 8.8 11 11.6v6.9l-6.2-2.8V8.8Zm14.4 0v7.9L13 19.5v-6.9l6.2-2.8Z" opacity=".8"/>`
)

/** A report nothing recognised.
 *
 *  Needed because `hz-default` was the flame, which was harmless while no fire
 *  category existed and is actively misleading now that one does: an
 *  unclassified report and a burning building would have drawn the same marker.
 *  A question mark says the one true thing about this pin — somebody reported
 *  something here and the system has not worked out what. */
const QUERY = g(
  `<path d="M12 4.4c-2.5 0-4.3 1.5-4.6 3.8h2.7c.2-1 .9-1.6 1.9-1.6 1.1 0 1.8.6 1.8 1.5 0 .8-.4 1.2-1.4 1.9-1.2.8-1.7 1.6-1.6 3h2.5c0-.8.3-1.2 1.3-1.9 1.3-.9 2-1.8 2-3.2 0-2.1-1.8-3.5-4.6-3.5Z"/>` +
  `<circle cx="12" cy="16.6" r="1.5"/>`
)

// -------------------------------------------------------------- vehicles ---

const AMBULANCE = g(
  `<path d="M2.6 8.2h11v8.2h-11z"/><path d="M13.6 10.6h3.6l3.2 3.2v2.6h-6.8z"/>` +
  `<g fill="#0b1220"><rect x="6.4" y="10" width="3.6" height="1.5"/>` +
  `<rect x="7.4" y="9" width="1.6" height="3.5"/></g>` +
  `<circle cx="6.4" cy="17.6" r="2"/><circle cx="16.8" cy="17.6" r="2"/>`
)

const BOAT = g(
  `<path d="M3 15.4h18l-2.2 4H5.2L3 15.4Z"/>` +
  `<path d="M11.2 3.4h1.6v10.6h-1.6z"/>` +
  `<path d="M13.6 4.6 19 9.4l-5.4 2.2V4.6Z" opacity=".85"/>`
)

const PUMP = g(
  `<path d="M6 9.6h9.2v8.8H6z"/><rect x="4.2" y="7.4" width="12.8" height="2.4" rx=".8"/>` +
  `<path d="M17 11.4h2.4v2.2H17z"/>` +
  `<path d="M19.4 12.5c1.4 1.6 2 2.7 2 3.6a2 2 0 1 1-4 0c0-.9.6-2 2-3.6Z" opacity=".9"/>`
)

const ENGINE = g(
  `<path d="M2.4 9h10.2v7.4H2.4z"/><path d="M12.6 11h4.4l3.6 3.4v2H12.6z"/>` +
  `<rect x="4" y="5.4" width="8" height="2" rx=".8" opacity=".85"/>` +
  `<circle cx="6" cy="17.8" r="1.9"/><circle cx="16.6" cy="17.8" r="1.9"/>`
)

const TEAM = g(
  `<circle cx="8" cy="7" r="2.4"/><circle cx="16" cy="7" r="2.4"/>` +
  `<path d="M2.8 18.6c0-2.9 2.3-5 5.2-5s5.2 2.1 5.2 5v1.4H2.8v-1.4Z"/>` +
  `<path d="M13.6 14.2c.7-.4 1.5-.6 2.4-.6 2.9 0 5.2 2.1 5.2 5v1.4h-5.2v-1.4c0-1.7-.9-3.3-2.4-4.4Z" opacity=".85"/>`
)

const BUS = g(
  `<rect x="3.6" y="3.8" width="16.8" height="13.2" rx="2.2"/>` +
  `<g fill="#0b1220"><rect x="5.6" y="6" width="5.4" height="4.2" rx=".6"/>` +
  `<rect x="13" y="6" width="5.4" height="4.2" rx=".6"/></g>` +
  `<circle cx="7.2" cy="18.4" r="1.9"/><circle cx="16.8" cy="18.4" r="1.9"/>`
)

const DIGGER = g(
  `<path d="M3 13.4h9.6v4.2H3z"/><circle cx="6" cy="18.6" r="2.2"/><circle cx="12.6" cy="18.6" r="1.8"/>` +
  `<path d="M12.8 12.6 17 5.6l1.6 1-3.6 6.6 4.6 1.4-.6 2-6.2-1.8v-2.2Z"/>`
)

const TRUCK = g(
  `<path d="M2.4 6.6h11.2v9.8H2.4z"/><path d="M13.6 9.6h3.8l3.2 3.4v3.4h-7z"/>` +
  `<path d="M4.6 8.6h6.8v2.2H4.6z" fill="#0b1220" opacity=".55"/>` +
  `<circle cx="6.4" cy="17.6" r="2"/><circle cx="16.8" cy="17.6" r="2"/>`
)

const TANKER = g(
  `<rect x="2.4" y="8" width="12.4" height="7.2" rx="3.6"/>` +
  `<path d="M15.2 10h2.6l2.8 3v2.2h-5.4z"/>` +
  `<circle cx="6.4" cy="17.4" r="2"/><circle cx="17" cy="17.4" r="2"/>` +
  `<path d="M6 10.4c.9 1.2 1.4 2 1.4 2.6a1.4 1.4 0 1 1-2.8 0c0-.6.5-1.4 1.4-2.6Z" fill="#0b1220" opacity=".5"/>`
)

// ------------------------------------------------------------- lifelines ---

const HOSPITAL = g(
  `<rect x="3.6" y="4.4" width="16.8" height="15.2" rx="2"/>` +
  `<g fill="#0b1220"><rect x="10.6" y="7.4" width="2.8" height="9.2" rx=".7"/>` +
  `<rect x="7.4" y="10.6" width="9.2" height="2.8" rx=".7"/></g>`
)

const SHELTER = g(
  `<path d="M12 3.2 2.4 11h2.8v9.2h13.6V11h2.8L12 3.2Z"/>` +
  `<rect x="9.6" y="13.4" width="4.8" height="6.8" rx=".6" fill="#0b1220" opacity=".55"/>`
)

const FOOD = g(
  `<path d="M5.4 3.6h1.8v7.2a1.6 1.6 0 0 1-1.4 1.6v8a1 1 0 0 1-2 0v-8a1.6 1.6 0 0 1-1.4-1.6V3.6h1.8v5.2h1.2V3.6Z"/>` +
  `<path d="M16.4 3.4c2.6 0 4.4 2.6 4.4 6 0 2.6-1 4.6-2.6 5.3v5.7a1.1 1.1 0 0 1-2.2 0V3.4Z"/>` +
  `<path d="M13.8 12.2c-1.4-.8-2.2-2.6-2.2-4.8 0-2.2.8-4 2.2-4v8.8Z" opacity=".85"/>`
)

const WATER = g(
  `<path d="M12 2.8s6.4 7 6.4 11.2a6.4 6.4 0 1 1-12.8 0C5.6 9.8 12 2.8 12 2.8Z"/>` +
  `<path d="M12 17.8a3.6 3.6 0 0 1-3.6-3.6h1.8a1.8 1.8 0 0 0 1.8 1.8v1.8Z" fill="#0b1220" opacity=".5"/>`
)

const MEDKIT = g(
  `<rect x="2.6" y="7" width="18.8" height="12.4" rx="2"/>` +
  `<path d="M8.6 4.6h6.8a1.4 1.4 0 0 1 1.4 1.4v1h-9.6v-1a1.4 1.4 0 0 1 1.4-1.4Z"/>` +
  `<g fill="#0b1220"><rect x="10.6" y="9.6" width="2.8" height="7.2" rx=".7"/>` +
  `<rect x="8.4" y="11.8" width="7.2" height="2.8" rx=".7"/></g>`
)

const PUMPHOUSE = g(
  `<path d="M4 8.6h11v10.8H4z"/><path d="M3 6h13l-1.6 2.6H4.6L3 6Z"/>` +
  `<path d="M16.4 12h2v2.2h-2z"/>` +
  `<path d="M18.6 13.2c1.3 1.5 1.9 2.5 1.9 3.3a1.9 1.9 0 1 1-3.8 0c0-.8.6-1.8 1.9-3.3Z" opacity=".9"/>`
)

const SCHOOL = g(
  `<path d="M12 3.4 1.8 8.2 12 13l10.2-4.8L12 3.4Z"/>` +
  `<path d="M6 11.4v4.4c0 1.9 2.7 3.4 6 3.4s6-1.5 6-3.4v-4.4l-6 2.8-6-2.8Z" opacity=".85"/>`
)

const SUBSTATION = g(
  `<path d="M6.6 2.8h10.8l-1.4 18.4H8L6.6 2.8Z" opacity=".85"/>` +
  `<path d="M13.4 6 8.8 13h3.2l-.6 5.4 4.6-7.4h-3.2l.6-5Z" fill="#0b1220"/>`
)

// ------------------------------------------------------------------- self ---

/** "You are here". The pin shape is the container now, so the glyph inside it
 *  is a person rather than a second pin. */
const YOU = g(
  `<circle cx="12" cy="6.4" r="3.2"/>` +
  `<path d="M4.8 20.6c0-3.6 3.2-6.4 7.2-6.4s7.2 2.8 7.2 6.4v1.2H4.8v-1.2Z"/>`
)

const FLAG = g(
  `<rect x="5.4" y="2.8" width="1.9" height="18.4" rx=".8"/>` +
  `<path d="M7.8 3.6h11.4l-2.6 4.2 2.6 4.2H7.8z"/>`
)

/** name -> [svg body, default fill]. The fill is overridden per use where the
 *  colour carries meaning (severity, unit status). */
export const ICONS: Record<string, [Svg, string]> = {
  // hazards, by incident category
  "hz-flooded_road": [WAVE, "#f97316"],
  "hz-waterlogging": [PUDDLE, "#eab308"],
  "hz-fallen_tree": [TREE, "#65a30d"],
  "hz-power_line": [BOLT, "#eab308"],
  "hz-structural_damage": [CRACK, "#ef4444"],
  "hz-blocked_drain": [DRAIN, "#a16207"],
  "hz-person_stranded": [PERSON, "#ef4444"],
  "hz-heat_casualty": [HEAT, "#f97316"],
  "hz-supply_shortage": [BOX, "#8b5cf6"],
  "hz-fire": [FIRE, "#ef4444"],
  "hz-unknown_report": [QUERY, "#64748b"],
  // Slate, not orange. The default is for a category this build has never
  // heard of, and painting it a warning colour claims a severity nothing has
  // established.
  "hz-default": [QUERY, "#64748b"],

  // vehicles, by resource kind
  "rk-ambulance": [AMBULANCE, "#64748b"],
  "rk-boat": [BOAT, "#64748b"],
  "rk-pump": [PUMP, "#64748b"],
  "rk-fire_engine": [ENGINE, "#64748b"],
  "rk-rescue_team": [TEAM, "#64748b"],
  "rk-bus": [BUS, "#64748b"],
  "rk-jcb": [DIGGER, "#64748b"],
  "rk-supply_truck": [TRUCK, "#64748b"],
  "rk-water_tanker": [TANKER, "#64748b"],
  "rk-default": [TRUCK, "#64748b"],

  // lifelines, by kind
  "lf-hospital": [HOSPITAL, "#0284c7"],
  "lf-shelter": [SHELTER, "#0d9488"],
  "lf-relief_centre": [FOOD, "#7c3aed"],
  "lf-food_kitchen": [FOOD, "#7c3aed"],
  "lf-water_point": [WATER, "#0891b2"],
  "lf-medical_camp": [MEDKIT, "#db2777"],
  "lf-pump_station": [PUMPHOUSE, "#475569"],
  "lf-school": [SCHOOL, "#475569"],
  "lf-substation": [SUBSTATION, "#475569"],
  "lf-default": [SHELTER, "#0d9488"],

  // everything else
  "ui-me": [YOU, "#8b5cf6"],
  "ui-destination": [FLAG, "#22c55e"],
  "ui-block": [CRACK, "#dc2626"],
}

/** Every colour a hazard or unit icon can be tinted with, so each combination
 *  can be pre-rasterised once instead of on every poll. */
export const TINTS = [
  "#ef4444", "#f97316", "#eab308", "#65a30d", "#22c55e", "#0d9488",
  "#0891b2", "#0284c7", "#0ea5e9", "#7c3aed", "#8b5cf6", "#db2777",
  "#a16207", "#64748b", "#475569", "#dc2626", "#10b981", "#f59e0b",
]

export const imageName = (icon: string, colour: string) =>
  `${icon}|${colour}`

/** The round marker: the same glyph, in a disc centred on the coordinate.
 *
 *  This is the marker system the citizen and crew maps share with the BiChat
 *  Android map: a filled disc in the colour that carries the meaning, a dark
 *  keyline that separates it from any basemap, and the glyph in white. It is
 *  smaller than the pin and has no tip, so it must be drawn with
 *  `icon-anchor: "center"`. Built on demand through `styleimagemissing`, like
 *  every non-default tint. */
export const badgeName = (icon: string, colour: string) =>
  imageName(`bd/${icon}`, colour)

/** How large each pin is rasterised, and the ratio Mapbox is told to divide by.
 *
 *  96×128 at `pixelRatio: 4` is a 24×32 pt pin that stays crisp on a retina
 *  laptop and on a 3× phone. Raising the bitmap rather than the `icon-size`
 *  multiplier is what keeps a bigger icon from also being a blurry one.
 */
const PIN_W = 96
const PIN_H = 128
const PIN_RATIO = 4

/** Badges are 28 pt across, rasterised at 3x like the pins are at 4x. */
const BADGE_PX = 84
const BADGE_RATIO = 3

const wrapBadge = (body: Svg): Svg =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 28 28" width="28" height="28">${body}</svg>`

const badge = (c: string, glyph: Svg) =>
  `<circle cx="14" cy="14" r="13.4" fill="#0b0f17" opacity=".92"/>` +
  `<circle cx="14" cy="14" r="11.4" fill="${c}"/>` +
  `<g transform="translate(14 14) scale(.62) translate(-12 -12)">${glyph}</g>`

/** Rasterise one SVG at 4× and hand Mapbox an ImageBitmap.
 *
 *  `addImage` accepts an ImageBitmap directly, which avoids the usual
 *  `new Image()` + onload dance and, more importantly, avoids a race where the
 *  first poll's `setData` lands before the sprite exists and Mapbox logs a
 *  missing-image warning for every feature.
 */
async function raster(svg: Svg, w = PIN_W, h = PIN_H): Promise<ImageBitmap> {
  const blob = new Blob([svg], { type: "image/svg+xml" })
  try {
    return await createImageBitmap(blob, { resizeWidth: w, resizeHeight: h })
  } catch {
    // Safari has historically refused SVG blobs in createImageBitmap. Going
    // through an <img> and a canvas costs a frame and works everywhere.
    const url = URL.createObjectURL(blob)
    try {
      const img = new Image()
      img.decoding = "async"
      await new Promise<void>((resolve, reject) => {
        img.onload = () => resolve()
        img.onerror = () => reject(new Error("icon failed to decode"))
        img.src = url
      })
      const canvas = document.createElement("canvas")
      canvas.width = w
      canvas.height = h
      const ctx = canvas.getContext("2d")
      if (!ctx) throw new Error("no 2d context")
      ctx.drawImage(img, 0, 0, w, h)
      return await createImageBitmap(canvas)
    } finally {
      URL.revokeObjectURL(url)
    }
  }
}

type MapLike = {
  hasImage(name: string): boolean
  addImage(name: string, image: ImageBitmap, options?: { pixelRatio?: number }): void
  triggerRepaint?: () => void
}

/** Every pin bitmap, built once per page and shared by every map on it.
 *
 *  This used to rasterise every icon in every one of eighteen tints for EACH
 *  map — a few hundred 96×128 bitmaps, ~15 MB, per map, before Mapbox copied
 *  them again into its own atlas. With a wall of map screens that was most of
 *  the JS heap. Now: one shared cache, and a tint is built only the first time
 *  something is actually drawn in it. */
const READY = new Map<string, ImageBitmap>()
const PENDING = new Map<string, Promise<ImageBitmap | null>>()

function build(key: string): Promise<ImageBitmap | null> {
  const hit = READY.get(key)
  if (hit) return Promise.resolve(hit)
  const inflight = PENDING.get(key)
  if (inflight) return inflight
  const [rawName, colour] = key.split("|")
  const isBadge = rawName.startsWith("bd/")
  const name = isBadge ? rawName.slice(3) : rawName
  const spec = ICONS[name as keyof typeof ICONS]
  if (!spec || !colour) return Promise.resolve(null)
  const job = (isBadge
    ? raster(wrapBadge(badge(colour, spec[0])), BADGE_PX, BADGE_PX)
    : raster(wrap(pin(colour, spec[0]))))
    .then((bitmap) => {
      READY.set(key, bitmap)
      return bitmap
    })
    .catch(() => null)
    .finally(() => PENDING.delete(key))
  PENDING.set(key, job)
  return job
}

/** `styleimagemissing` handler: supply the pin synchronously when it is
 *  already built (no warning, no pop-in), otherwise build it and add it. */
export function provideIcon(map: MapLike, key: string): void {
  if (map.hasImage(key)) return
  const ratio = key.startsWith("bd/") ? BADGE_RATIO : PIN_RATIO
  const hit = READY.get(key)
  if (hit) {
    map.addImage(key, hit, { pixelRatio: ratio })
    return
  }
  void build(key).then((bitmap) => {
    if (bitmap && !map.hasImage(key)) {
      map.addImage(key, bitmap, { pixelRatio: ratio })
      map.triggerRepaint?.()
    }
  })
}

/** Register each icon in its default tint (the common case), from the shared
 *  cache. Other tints arrive through `provideIcon` when first drawn. */
export async function registerIcons(map: MapLike): Promise<void> {
  const keys = Object.entries(ICONS).map(([name, [, fallback]]) => imageName(name, fallback))
  // Anything this page has already built, in any tint, is free to add now.
  for (const key of READY.keys()) if (!keys.includes(key)) keys.push(key)
  await Promise.all(keys.map(async (key) => {
    const bitmap = await build(key)
    if (bitmap && !map.hasImage(key)) {
      map.addImage(key, bitmap, { pixelRatio: key.startsWith("bd/") ? BADGE_RATIO : PIN_RATIO })
    }
  }))
}
