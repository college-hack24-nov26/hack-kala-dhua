import { Navigate, Route, Routes } from "react-router-dom"
import { AdminShell } from "@/components/layout/AdminShell"
import { RequireRole } from "@/auth/RequireRole"

import Login from "@/routes/auth/Login"
import DemoConsole from "@/routes/demo/DemoConsole"
import Copilot from "@/routes/admin/Copilot"
import Dispatch from "@/routes/admin/Dispatch"
import CitizenApp from "@/routes/citizen/CitizenApp"
import FieldApp from "@/routes/field/FieldApp"
import IntakeInbox from "@/routes/admin/IntakeInbox"
import RiskBoard from "@/routes/admin/RiskBoard"
import Forecast from "@/routes/admin/Forecast"
import AgencyHandoff from "@/routes/admin/AgencyHandoff"
import Configuration from "@/routes/admin/Configuration"
import IncidentQueue from "@/routes/admin/IncidentQueue"
import AllocationPlanner from "@/routes/admin/AllocationPlanner"
import DecisionGate from "@/routes/admin/DecisionGate"
import AgentTrace from "@/routes/admin/AgentTrace"
import ResourcesPage from "@/routes/admin/ResourcesPage"
import UnitsPage from "@/routes/admin/UnitsPage"
import SurgePage from "@/routes/admin/SurgePage"
import ModelsPage from "@/routes/admin/ModelsPage"
import AlertsPage from "@/routes/admin/AlertsPage"
import AfterAction from "@/routes/admin/AfterAction"
import Replay from "@/routes/admin/Replay"
import Architecture from "@/routes/admin/Architecture"
import LiveFeed from "@/routes/admin/LiveFeed"
import CommandWall from "@/routes/admin/CommandWall"
import ZonePage from "@/routes/admin/ZonePage"
import ZoneAgents from "@/routes/admin/ZoneAgents"
import ZoneLive from "@/routes/admin/ZoneLive"
import ArrivalAccess from "@/routes/admin/ArrivalAccess"
import EvidenceTrail from "@/routes/admin/EvidenceTrail"
import AuditTrail from "@/routes/admin/AuditTrail"
import AgentGraph from "@/routes/admin/AgentGraph"
import IncidentResponse from "@/routes/admin/IncidentResponse"
import MeshDevices from "@/routes/admin/MeshDevices"
import SensorAnalytics from "@/routes/admin/SensorAnalytics"
import DroneLink from "@/routes/admin/DroneLink"
import Ledger from "@/routes/admin/Ledger"

/** Three interfaces, three URLs.
 *
 *  They are separate routes rather than tabs because they are separate jobs. A
 *  resident opens /citizen on a phone with no account; a crew opens /field
 *  scoped to their agency; an officer opens /admin. Putting a walkable marker
 *  on the operations console was simply the wrong screen for it.
 */
export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />

      {/* The public face. No account required: the largest audience for this
          has none, and should not meet a login wall to find out whether their
          street is about to flood. */}
      <Route path="/" element={<Navigate to="/citizen" replace />} />
      <Route path="/citizen/*" element={<CitizenApp />} />

      <Route
        path="/field/*"
        element={
          <RequireRole need="field">
            <FieldApp />
          </RequireRole>
        }
      />

      <Route
        path="/admin/*"
        element={
          <RequireRole need="staff">
            <AdminShell>
              <Routes>
                <Route path="wall" element={<CommandWall />} />
                <Route path="wall/zone/:zoneId" element={<ZonePage />} />
                <Route path="wall/zone/:zoneId/agents" element={<ZoneAgents />} />
                <Route path="wall/zone/:zoneId/live" element={<ZoneLive />} />
                <Route path="arrival" element={<ArrivalAccess />} />
                <Route path="evidence" element={<EvidenceTrail />} />
                <Route path="audit" element={<AuditTrail />} />
                <Route path="console" element={<DemoConsole />} />
                <Route path="copilot" element={<Copilot />} />
                {/* Who is going where. It was only on the map, which answers
                    "where" and not "why that unit". */}
                <Route path="dispatch" element={<Dispatch />} />
                <Route path="intake" element={<IntakeInbox />} />
                <Route path="feed" element={<LiveFeed />} />
                <Route path="response" element={<IncidentResponse />} />
                <Route path="mesh" element={<MeshDevices />} />
                {/* LoRa field nodes: heatmap over the map, then each node's history. */}
                <Route path="analytics" element={<SensorAnalytics />} />
                <Route path="swarm" element={<Navigate to="/admin/drone-link" replace />} />
                <Route path="drone-link" element={<DroneLink />} />
                <Route path="ledger" element={<Ledger />} />
                <Route path="risk" element={<RiskBoard />} />
                <Route path="forecast" element={<Forecast />} />
                <Route path="incidents" element={<IncidentQueue />} />
                <Route path="allocation" element={<AllocationPlanner />} />
                <Route path="handoff" element={<AgencyHandoff />} />
                <Route path="decisions" element={<DecisionGate />} />
                <Route path="agent" element={<AgentTrace />} />
                <Route path="graph" element={<AgentGraph />} />
                <Route path="resources" element={<ResourcesPage />} />
                <Route path="units" element={<UnitsPage />} />
                <Route path="surge" element={<SurgePage />} />
                <Route path="models" element={<ModelsPage />} />
                <Route path="alerts" element={<AlertsPage />} />
                <Route path="after-action" element={<AfterAction />} />
                <Route path="replay" element={<Replay />} />
                <Route path="architecture" element={<Architecture />} />
                <Route path="configuration" element={<Configuration />} />
                {/* `live` and `demo` were two names for overlapping things.
                    One console now; both old paths land on it. */}
                <Route path="live" element={<Navigate to="/admin/console" replace />} />
                <Route path="demo" element={<Navigate to="/admin/console" replace />} />
                <Route path="*" element={<Navigate to="/admin/wall" replace />} />
              </Routes>
            </AdminShell>
          </RequireRole>
        }
      />

      <Route path="*" element={<Navigate to="/citizen" replace />} />
    </Routes>
  )
}

export default App
