/** First-launch history for the local ledger: an illustrative Indradhanu
 *  workflow, labelled "seed" on every block so nobody mistakes it for a live
 *  record. Live history is anchored from the event log (see eventAnchor). */
import { storeMediaOffline } from "@/lib/ipfs/client"
import type { DAGEngine } from "./dagEngine"

function cameraFrameSvg(): Blob {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">
<rect width="320" height="180" fill="#1e293b"/><rect x="0" y="120" width="320" height="60" fill="#334155"/>
<rect x="30" y="50" width="70" height="70" fill="#475569"/><rect x="190" y="35" width="90" height="85" fill="#475569"/>
<path d="M0 135 Q80 125 160 138 T320 132 V180 H0Z" fill="#2563eb" opacity="0.55"/>
<rect x="212" y="58" width="40" height="40" fill="none" stroke="#ef4444" stroke-width="2"/>
<text x="214" y="54" fill="#ef4444" font-family="monospace" font-size="9">person 0.91</text>
<text x="8" y="14" fill="#e2e8f0" font-family="monospace" font-size="9">CAM-HDP-04  demo frame  (seed data)</text></svg>`
  return new Blob([svg], { type: "image/svg+xml" })
}

export async function seedLedger(engine: DAGEngine): Promise<void> {
  const t0 = Date.parse("2026-09-28T05:30:00Z")
  const at = (min: number) => new Date(t0 + min * 60_000).toISOString()
  const seed = { source: "seed" as const }

  const g = await engine.addBlock("GENESIS", { note: "Ledger node initialised", sector: "Pune", node: "control-room-pune" },
    undefined, { ...seed, parents: [], timestamp: at(0) })
  const sos = await engine.addBlock("SOS_REQUEST", {
    channel: "BitChat mesh", text: "Stranded at Hadapsar, water at the door, 3 people", ward: "Hadapsar", trust: 0.66,
  }, undefined, { ...seed, parents: [g.hash], timestamp: at(4), issuer: "mesh-gateway-hadapsar" })
  const alloc = await engine.addBlock("RESOURCE_ALLOCATION", {
    request: "NDRF water-rescue team", requested_by: "Duty officer", for: "Hadapsar SOS", eta_min: 18,
  }, undefined, { ...seed, parents: [sos.hash], timestamp: at(6) })
  const cid = await storeMediaOffline(cameraFrameSvg())
  const verify = await engine.addBlock("INCIDENT_VERIFICATION", {
    method: "VLM surveillance", camera: "CAM-HDP-04", finding: "person on a roof, water 1 m", confidence: 0.91,
  }, cid, { ...seed, parents: [alloc.hash], timestamp: at(8), issuer: "vlm-node-hadapsar" })
  // Two teams lose signal and keep writing: two branches off the same block.
  const a = await engine.addBlock("FIELD_UPDATE", {
    team: "NDRF-2", ward: "Hadapsar", status: "on site, 2 of 3 people out", offline: true,
  }, undefined, { ...seed, parents: [verify.hash], timestamp: at(31), issuer: "ward-team-hadapsar" })
  const b = await engine.addBlock("FIELD_UPDATE", {
    team: "Fire-7", ward: "Kothrud", status: "road blocked at Paud Phata, rerouting", offline: true,
  }, undefined, { ...seed, parents: [verify.hash], timestamp: at(33), issuer: "ward-team-kothrud" })
  await engine.addBlock("MERGE_BLOCK", {
    note: "Mesh reconnected; both ward branches merged without overwriting either", branches: 2,
  }, undefined, { ...seed, parents: [a.hash, b.hash], timestamp: at(47), issuer: "control-room-pune" })
}
