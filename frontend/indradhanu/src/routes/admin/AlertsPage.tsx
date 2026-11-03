import { useMemo } from "react"
import { MapPin, Radio, Users } from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty } from "./DecisionGate"

/** What the public was actually told.
 *
 *  An alert here is never a side effect of a high score. It exists because a
 *  decision cleared the delegation gate, and it carries that decision's id. The
 *  wards where the action is still waiting for an officer are silent, on
 *  purpose, and the fact that they are silent is visible on the decision gate
 *  rather than hidden here.
 */

const time = (iso: string | null) =>
  iso
    ? new Date(iso).toLocaleTimeString(undefined, {
        hour: "2-digit", minute: "2-digit", hour12: false,
      })
    : "—"

export default function AlertsPage() {
  const { state } = useDemo()
  const alerts = state.alerts

  const { reach, waiting } = useMemo(
    () => ({
      reach: alerts.reduce((n, a) => n + (a.reach || 0), 0),
      waiting: state.decisions.filter(
        (d) =>
          d.status === "awaiting_approval" &&
          /advisory|warning|evacuat/i.test(d.action)
      ).length,
    }),
    [alerts, state.decisions]
  )

  if (!alerts.length) {
    return (
      <Empty
        icon={<Radio className="size-8 text-muted-foreground" />}
        title="No alerts issued"
        body={
          waiting > 0
            ? `${waiting} public-communication action${waiting === 1 ? " is" : "s are"} sitting on the decision gate. Nothing goes out until somebody with the delegation acts, which is the point.`
            : "An advisory appears here once a decision authorising it clears the delegation gate, with the clause that authorised it and the reach it achieved."
        }
      />
    )
  }

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="grid gap-5 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Alerts issued</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{alerts.length}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>People reached</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {reach.toLocaleString()}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            Estimated exposed population of the wards alerted, not a delivery count.
          </CardContent>
        </Card>
        <Card className={waiting ? "border-destructive/50" : undefined}>
          <CardHeader className="pb-3">
            <CardDescription>Held, so not sent</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{waiting}</CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            Waiting on an officer. Those wards are deliberately silent.
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-2 xl:grid-cols-2">
        {alerts.map((a) => (
          <Card key={a.id}>
            <CardHeader className="pb-3">
              <div className="flex items-start justify-between gap-2">
                <CardTitle className="text-sm">{a.headline}</CardTitle>
                <span className="text-muted-foreground shrink-0 text-xs tabular-nums">
                  {time(a.issuedAt)}
                </span>
              </div>
              <CardDescription>
                {a.wardName ?? a.wardId} · severity {a.severity} · {a.language}
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              <p className="bg-muted/40 rounded border p-2 text-xs">{a.action}</p>

              {a.safeLocation && (
                <div className="flex items-center gap-1.5 text-xs">
                  <MapPin className="size-3 shrink-0" />
                  <span className="font-medium">{a.safeLocation.name}</span>
                  <span className="text-muted-foreground">
                    {a.safeLocation.distance_km} km
                  </span>
                </div>
              )}

              <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                <span className="inline-flex items-center gap-1">
                  <Users className="size-3" />
                  {(a.reach || 0).toLocaleString()} reached
                </span>
                {a.channels.map((c) => (
                  <Badge key={c} variant="outline" className="font-normal">
                    {c}
                  </Badge>
                ))}
                {a.decisionId && <span>from an authorised decision</span>}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  )
}
