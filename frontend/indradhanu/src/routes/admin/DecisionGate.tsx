import { useMemo, useState } from "react"
import { ClipboardCheck, Gavel, Loader2, ShieldAlert } from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { DECISION_STATUS } from "@/lib/plain"

/** What the system proposed, and what it was not allowed to do on its own.
 *
 *  The gate is the part of this that is not a technology argument. An action the
 *  delegation matrix reserves to a named officer does not issue because the
 *  model was confident about it; it waits, and the interface names the clause
 *  that made it wait. Everything else issues automatically and is listed here
 *  afterwards, which is the other half of the same promise: nothing happens that
 *  an officer cannot see.
 */

const TABS = {
  awaiting_approval: "Waiting on an officer",
  auto_issued: "Issued automatically",
  approved: "Approved",
  rejected: "Rejected",
} as const
type Tab = keyof typeof TABS

const TONE: Record<string, string> = {
  awaiting_approval: "border-destructive/50",
  auto_issued: "border-emerald-500/40",
  approved: "border-sky-500/40",
  rejected: "border-slate-500/40",
  overridden: "border-amber-500/40",
}

const time = (iso: string) =>
  new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit", minute: "2-digit", hour12: false,
  })

/** The "nothing here yet" panel, shared with the alerts and after-action
 *  screens, which import it from this file. A screen with no rows should say
 *  what would put rows in it, because an empty table and a broken table look
 *  identical otherwise. */
export function Empty({
  icon, title, body,
}: {
  icon?: React.ReactNode
  title: string
  body: string
}) {
  return (
    <div className="flex min-h-[320px] flex-col items-center justify-center gap-2 p-8 text-center">
      {icon}
      <p className="text-sm font-medium">{title}</p>
      <p className="text-muted-foreground max-w-md text-xs">{body}</p>
    </div>
  )
}

export default function DecisionGate() {
  const { state, activity, busy, run } = useDemo()
  const [tab, setTab] = useState<Tab>("awaiting_approval")

  const counts = useMemo(() => {
    const c = new Map<string, number>()
    for (const d of state.decisions) c.set(d.status, (c.get(d.status) ?? 0) + 1)
    return c
  }, [state.decisions])

  const rows = state.decisions.filter((d) => d.status === tab)
  const wardName = useMemo(
    () => new Map(state.wards.map((w) => [w.id, w.name] as const)),
    [state.wards]
  )
  const waiting = counts.get("awaiting_approval") ?? 0

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      {waiting > 0 && (
        <Card className="border-destructive/50">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <ShieldAlert className="size-4" />
              {waiting} action{waiting === 1 ? "" : "s"} will not issue without you
            </CardTitle>
            <CardDescription>
              These are held by the delegation matrix, not by a confidence
              threshold. Until somebody with the delegation acts, nothing moves.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      <div className="flex flex-wrap gap-1">
        {(Object.keys(TABS) as Tab[]).map((k) => (
          <Button
            key={k}
            size="sm"
            variant={tab === k ? "secondary" : "ghost"}
            className="h-8 text-xs"
            onClick={() => setTab(k)}
          >
            {TABS[k]}
            <span className="text-muted-foreground ml-1.5 tabular-nums">
              {counts.get(k) ?? 0}
            </span>
          </Button>
        ))}
      </div>

      {rows.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <ClipboardCheck className="size-4" /> Nothing in this state
            </CardTitle>
            <CardDescription>
              {state.running
                ? "Decisions appear as the policy agent proposes them."
                : "Start live ingest on the command console; the policy agent proposes as incidents open."}
            </CardDescription>
          </CardHeader>
        </Card>
      ) : (
        <div className="grid gap-2 xl:grid-cols-2">
          {rows.map((d) => (
            <Card key={d.id} className={TONE[d.status] ?? ""}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <CardTitle className="text-sm">{d.action}</CardTitle>
                  <span className="text-muted-foreground shrink-0 text-xs tabular-nums">
                    {time(d.createdAt)}
                  </span>
                </div>
                <CardDescription className="space-y-1">
                  <span className="block">
                    {/* A raw ward uuid in the one place a commissioner reads
                        hardest. The name was always available on `state.wards`
                        and this line never looked at it. */}
                    {d.target}
                    {d.wardId ? ` · ${wardName.get(d.wardId) ?? d.wardId}` : ""}
                  </span>
                  <span className="block">
                    {/* Status in words. "auto_issued" reads to a stranger as
                        "a person waved it through"; it means the opposite —
                        the clause allowed it without one. */}
                    <span className="font-medium">
                      {DECISION_STATUS[d.status]?.label ?? d.status.replace(/_/g, " ")}
                    </span>
                    {DECISION_STATUS[d.status]?.hint
                      ? ` — ${DECISION_STATUS[d.status].hint}`
                      : ""}
                  </span>
                  <span className="block">
                    The model was {(d.confidence * 100).toFixed(0)}% confident.
                    Confidence alone never issues anything: the clause below
                    decides.
                  </span>
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-2">
                <p className="text-xs italic">{d.rationale}</p>

                {d.clause ? (
                  <div className="bg-muted/40 rounded border p-2 text-xs">
                    <div className="flex items-center gap-1.5">
                      <Gavel className="size-3 shrink-0" />
                      <Badge variant="outline">{d.clause}</Badge>
                    </div>
                    <div className="text-muted-foreground mt-1">
                      {d.withinDelegation === false
                        ? `Outside the standing delegation. Reserved to ${d.delegatedTo ?? "a named officer"}.`
                        : `Within the standing delegation of ${d.delegatedTo ?? "the duty officer"}.`}
                    </div>
                  </div>
                ) : (
                  <p className="text-muted-foreground text-xs">
                    No clause attached; this fell inside the standing delegation.
                  </p>
                )}

                {(activity.get(d.id)?.length ?? 0) > 0 && (
                  <div className="space-y-0.5 border-t pt-2">
                    {activity.get(d.id)!.map((a, i) => (
                      <div key={i} className="text-muted-foreground text-xs">
                        <span className="mr-2 tabular-nums">{time(a.at)}</span>
                        {a.text}
                      </div>
                    ))}
                  </div>
                )}

                {(d.status === "awaiting_approval" || d.status === "auto_issued") && (
                  <div className="flex gap-2 pt-1">
                    <Button
                      size="sm"
                      className="h-7 text-xs"
                      disabled={busy !== null}
                      onClick={() => run(`ok-${d.id}`, `/demo/decisions/${d.id}/approve`)}
                    >
                      {busy === `ok-${d.id}` && (
                        <Loader2 className="size-3 animate-spin" />
                      )}
                      Approve
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      className="h-7 text-xs"
                      disabled={busy !== null}
                      onClick={() => run(`no-${d.id}`, `/demo/decisions/${d.id}/reject`)}
                    >
                      Reject
                    </Button>
                    {d.status === "auto_issued" && (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 text-xs"
                        disabled={busy !== null}
                        onClick={() => run(`ov-${d.id}`, `/demo/decisions/${d.id}/override`)}
                      >
                        Override
                      </Button>
                    )}
                  </div>
                )}
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}
