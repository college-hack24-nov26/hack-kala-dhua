/** Shared domain types for the API surface.
 *
 *  This module was removed in an earlier frontend cleanup and two files kept
 *  importing it — `lib/tokens.ts` and `components/common/indicators.tsx` —
 *  which is where most of the build errors came from. It is reinstated here
 *  rather than having each file declare its own copy, because a second
 *  definition of `Severity` that drifts from the first is a worse bug than a
 *  missing import: nothing would fail, the colours would simply stop agreeing.
 *
 *  Both of these are data in the database, not code. `HazardType` must match
 *  the ids in `hazard_types`, and severity is the 1-5 band the risk scorer and
 *  the policy gate both work in. Adding a hazard means adding a row and then a
 *  member here; TypeScript will then point at every `Record<HazardType, …>`
 *  that needs a colour for it, which is the reason to keep these as unions
 *  rather than widening them to `string`.
 */

export type HazardType = "flood" | "heat" | "fire" | "air" | "seismic"

/** 1 minimal, 2 low, 3 moderate, 4 high, 5 critical. */
export type Severity = 1 | 2 | 3 | 4 | 5

export const HAZARD_TYPES: readonly HazardType[] = [
  "flood",
  "heat",
  "fire",
  "air",
  "seismic",
]

export const SEVERITIES: readonly Severity[] = [1, 2, 3, 4, 5]

/** Narrows an arbitrary number to the band, for values arriving from the API
 *  as plain integers. Out-of-range clamps rather than throwing: a severity of 7
 *  in a payload should colour something red, not blank the screen. */
export function toSeverity(value: number): Severity {
  const n = Math.round(value)
  if (n <= 1) return 1
  if (n >= 5) return 5
  return n as Severity
}
