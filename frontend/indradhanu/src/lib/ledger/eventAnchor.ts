/** Anchor the command centre's real event log into the local ledger.
 *
 *  Every backend event records the event that caused it (`causationId`), so the
 *  log is already a graph: a report causes an incident, which causes an
 *  assignment, which causes a re-route. Anchoring makes that graph
 *  tamper-evident on this device: each event becomes a signed block whose
 *  parent is the block of its cause. */
import { fetchEvents, type EventRow } from "@/api/httpClient"
import type { DAGEngine } from "./dagEngine"
import type { BlockPayloadType } from "./types"

export function payloadTypeFor(kind: string): BlockPayloadType {
  if (/^(report|incident)\./.test(kind)) return "SOS_REQUEST"
  if (/^(assignment|dispatch|resource)\./.test(kind)) return "RESOURCE_ALLOCATION"
  if (/^(decision|gate|approval|policy)\./.test(kind)) return "APPROVAL"
  if (/^(drone|sensor|vision|camera)\./.test(kind)) return "INCIDENT_VERIFICATION"
  if (/^(road|field|crew)\./.test(kind)) return "FIELD_UPDATE"
  if (/^(iot|mesh)\./.test(kind)) return "SENSOR_EVENT"
  return "SYSTEM_EVENT"
}

export async function anchorEvents(engine: DAGEngine, limit = 120): Promise<{ added: number; seen: number }> {
  const rows: EventRow[] = await fetchEvents({ limit })
  const byEvent = new Map<number, string>()
  for (const b of Object.values(engine.state.blocks)) if (b.eventId != null) byEvent.set(b.eventId, b.hash)
  let added = 0
  let lastEventBlock = [...byEvent.entries()].sort((a, b) => b[0] - a[0])[0]?.[1]
  for (const e of [...rows].sort((a, b) => a.id - b.id)) {
    if (byEvent.has(e.id)) continue
    const cause = e.causationId != null ? byEvent.get(e.causationId) : undefined
    const parents = cause ? [cause] : lastEventBlock ? [lastEventBlock] : engine.state.tips.slice(0, 1)
    const block = await engine.addBlock(payloadTypeFor(e.kind), {
      event_id: e.id, kind: e.kind, actor: e.actor, subject: `${e.subjectType}:${e.subjectId}`,
      ward: e.wardId, occurred_at: e.occurredAt, caused_by: e.causationId, detail: e.payload,
    }, undefined, { parents, timestamp: e.recordedAt, source: "event", eventId: e.id, issuer: "control-room-pune" })
    byEvent.set(e.id, block.hash)
    if (!cause) lastEventBlock = block.hash
    added++
  }
  return { added, seen: rows.length }
}
