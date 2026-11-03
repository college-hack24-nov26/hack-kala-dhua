import type { BlockPayloadType } from "@/lib/ledger/types"

export const TYPE_STYLE: Record<BlockPayloadType, { color: string; label: string }> = {
  GENESIS: { color: "#94a3b8", label: "Genesis" },
  SOS_REQUEST: { color: "#2a78d6", label: "SOS / report" },
  RESOURCE_ALLOCATION: { color: "#10b981", label: "Allocation" },
  APPROVAL: { color: "#f59e0b", label: "Approval" },
  INCIDENT_VERIFICATION: { color: "#8b5cf6", label: "Verification" },
  FIELD_UPDATE: { color: "#ec835a", label: "Field update" },
  SENSOR_EVENT: { color: "#14b8a6", label: "Sensor" },
  SYSTEM_EVENT: { color: "#64748b", label: "System" },
  MERGE_BLOCK: { color: "#e11d48", label: "Merge" },
}
