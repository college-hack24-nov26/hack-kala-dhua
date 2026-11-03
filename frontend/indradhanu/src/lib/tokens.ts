import type { HazardType, Severity } from "@/api/types"

/** Tailwind classes for the categorical hazard hues (solid fills only). */
export const hazardBg: Record<HazardType, string> = {
  flood: "bg-hazard-flood",
  heat: "bg-hazard-heat",
  fire: "bg-hazard-fire",
  air: "bg-hazard-air",
  seismic: "bg-hazard-seismic",
}

export const hazardText: Record<HazardType, string> = {
  flood: "text-hazard-flood",
  heat: "text-hazard-heat",
  fire: "text-hazard-fire",
  air: "text-hazard-air",
  seismic: "text-hazard-seismic",
}

export const hazardLabel: Record<HazardType, string> = {
  flood: "Flood",
  heat: "Heatwave",
  fire: "Wildfire",
  air: "Air Quality",
  seismic: "Seismic",
}

/** Raw CSS custom-property references, for Mapbox paint expressions. */
export const hazardVar: Record<HazardType, string> = {
  flood: "--hazard-flood",
  heat: "--hazard-heat",
  fire: "--hazard-fire",
  air: "--hazard-air",
  seismic: "--hazard-seismic",
}

export const severityBg: Record<Severity, string> = {
  1: "bg-sev-1 text-sev-1-foreground",
  2: "bg-sev-2 text-sev-2-foreground",
  3: "bg-sev-3 text-sev-3-foreground",
  4: "bg-sev-4 text-sev-4-foreground",
  5: "bg-sev-5 text-sev-5-foreground",
}

export const severityLabel: Record<Severity, string> = {
  1: "Minimal",
  2: "Low",
  3: "Moderate",
  4: "High",
  5: "Critical",
}

export const severityVar: Record<Severity, string> = {
  1: "--sev-1",
  2: "--sev-2",
  3: "--sev-3",
  4: "--sev-4",
  5: "--sev-5",
}

/** Score (0-1) to severity bucket. Single source of truth. */
export function scoreToSeverity(score: number): Severity {
  if (score >= 0.8) return 5
  if (score >= 0.6) return 4
  if (score >= 0.4) return 3
  if (score >= 0.2) return 2
  return 1
}

/** Convert an oklch() string to a hex colour.
 *  Mapbox GL parses CSS Color Level 3 only, so every token has to be
 *  converted before it reaches a paint expression. */
export function oklchToHex(input: string): string | null {
  const m = input.match(
    /oklch\(\s*([\d.]+%?)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*[\d.]+%?)?\s*\)/i
  )
  if (!m) return null
  let L = parseFloat(m[1])
  if (m[1].endsWith("%")) L /= 100
  const C = parseFloat(m[2])
  const H = (parseFloat(m[3]) * Math.PI) / 180

  const a = C * Math.cos(H)
  const b = C * Math.sin(H)

  // Oklab -> LMS
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b
  const s_ = L - 0.0894841775 * a - 1.291485548 * b
  const l3 = l_ * l_ * l_
  const m3 = m_ * m_ * m_
  const s3 = s_ * s_ * s_

  // LMS -> linear sRGB
  const lr = 4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3
  const lg = -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3
  const lb = -0.0041960863 * l3 - 0.7034186147 * m3 + 1.707614701 * s3

  const enc = (x: number) => {
    const v = x <= 0.0031308 ? 12.92 * x : 1.055 * Math.pow(x, 1 / 2.4) - 0.055
    return Math.round(Math.max(0, Math.min(1, v)) * 255)
  }
  return (
    "#" +
    [enc(lr), enc(lg), enc(lb)]
      .map((v) => v.toString(16).padStart(2, "0"))
      .join("")
  )
}

/** Resolve a CSS custom property to a hex colour Mapbox can parse. */
export function cssColor(varName: string, fallback = "#8a8a8a"): string {
  if (typeof window === "undefined") return fallback
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue(varName)
    .trim()
  if (!raw) return fallback
  if (raw.startsWith("#") || raw.startsWith("rgb") || raw.startsWith("hsl")) return raw
  return oklchToHex(raw) ?? fallback
}
