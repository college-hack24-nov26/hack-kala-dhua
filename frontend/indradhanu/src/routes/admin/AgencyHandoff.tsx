import { useEffect, useMemo, useRef, useState } from "react"
import {
  ArrowRight, Bot, Building2, Check, CornerDownRight, Handshake, Loader2, Send,
  TriangleAlert, Truck, X,
} from "lucide-react"
import { useSearchParams } from "react-router-dom"
import { useDemo } from "@/routes/demo/DemoProvider"
import type { AgencyRequest } from "@/routes/demo/useDemo"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

/** Asking somebody else for what you do not have.
 *
 *  This is the PS20 requirement the system is named after, and it was the one
 *  with no screen: the endpoints and the audit trail existed, so the workflow
 *  worked and nobody could see it.
 *
 *  The design point is that a request starts from a **shortfall the system
 *  already knows about**, not from a blank form. If an incident needs two water
 *  rescue units and the municipal fleet has one, the gap is on the map, on the
 *  incident queue and here — and here it comes with a button that names the
 *  agencies which actually hold that capability. Coordination failing because
 *  nobody knew who to ring is the failure mode this replaces.
 *
 *  Every transition is an event. Afterwards the log answers who was asked, when,
 *  and what they said, which is the part that matters in the review.
 */

const STATUS: Record<string, { label: string; tone: string }> = {
  requested: {
    label: "Waiting for a reply",
    tone: "bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30",
  },
  acknowledged: {
    label: "Acknowledged",
    tone: "bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30",
  },
  fulfilled: {
    label: "Fulfilled",
    tone: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30",
  },
  declined: {
    label: "Declined",
    tone: "bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/30",
  },
  cancelled: {
    label: "Cancelled",
    tone: "bg-slate-500/15 text-slate-600 dark:text-slate-400 border-slate-500/30",
  },
}

/** What each kind of answer means, in the officer's words. */
const REPLY: Record<string, { label: string; tone: string }> = {
  accept_full: { label: "Accepted", tone: "text-emerald-700 bg-emerald-50 border-emerald-200 dark:text-emerald-300 dark:bg-emerald-500/10 dark:border-emerald-500/30" },
  accept_partial: { label: "Partly accepted", tone: "text-teal-700 bg-teal-50 border-teal-200 dark:text-teal-300 dark:bg-teal-500/10 dark:border-teal-500/30" },
  delayed: { label: "Later", tone: "text-amber-700 bg-amber-50 border-amber-200 dark:text-amber-300 dark:bg-amber-500/10 dark:border-amber-500/30" },
  need_info: { label: "Asked for details", tone: "text-sky-700 bg-sky-50 border-sky-200 dark:text-sky-300 dark:bg-sky-500/10 dark:border-sky-500/30" },
  decline_capacity: { label: "Declined: no capacity", tone: "text-red-700 bg-red-50 border-red-200 dark:text-red-300 dark:bg-red-500/10 dark:border-red-500/30" },
  decline_jurisdiction: { label: "Declined: jurisdiction", tone: "text-red-700 bg-red-50 border-red-200 dark:text-red-300 dark:bg-red-500/10 dark:border-red-500/30" },
  stand_down: { label: "Stood down", tone: "text-slate-600 bg-slate-50 border-slate-200 dark:text-slate-300 dark:bg-slate-500/10 dark:border-slate-500/30" },
}

const time = (iso: string | null) =>
  iso
    ? new Date(iso).toLocaleTimeString(undefined, {
        hour: "2-digit", minute: "2-digit", hour12: false,
      })
    : "—"

export default function AgencyHandoff() {
  const { state, busy, run, refresh } = useDemo()
  const [note, setNote] = useState("")
  const [draft, setDraft] = useState<{
    incidentId: string | null
    wardId: string
    capability: string
    quantity: number
  } | null>(null)

  /** Needs nobody in the municipal fleet is covering, joined to the agencies
   *  that actually hold that capability. This is the working list. */
  const shortfalls = useMemo(() => {
    const byIncident = new Map(state.incidents.map((i) => [i.id, i] as const))
    const asked = new Set(
      state.agencyRequests
        .filter((r) => r.status === "requested" || r.status === "acknowledged")
        .map((r) => `${r.incidentId}:${r.capability}`)
    )
    return state.needs
      .filter((n) => n.met < n.required)
      .map((n) => {
        const incident = byIncident.get(n.incidentId)
        if (!incident) return null
        const able = state.agencies.filter((a) =>
          a.capabilities.includes(n.capability)
        )
        return {
          ...n,
          incident,
          gap: n.required - n.met,
          able,
          alreadyAsked: asked.has(`${n.incidentId}:${n.capability}`),
        }
      })
      .filter((x): x is NonNullable<typeof x> => x !== null)
      .sort((a, b) => b.incident.severity - a.incident.severity || b.gap - a.gap)
  }, [state.needs, state.incidents, state.agencies, state.agencyRequests])

  /** Arriving here from the shortfall itself.
   *
   *  The allocation planner is where an officer finds out that a demand could
   *  not be covered, and this is the screen that does something about it — and
   *  there was no path between them. They had to read a capability off one
   *  screen, navigate here, and find the matching row by eye, which is exactly
   *  the kind of re-entry that gets skipped when a room is busy.
   *
   *  `?capability=` and `?incident=` open the draft already filled in. The
   *  parameters are a hint, never a command: the draft is only opened for a
   *  shortfall that is genuinely in the live list, so a stale or hand-edited
   *  link cannot invent a request for a need that does not exist.
   */
  const [params, setParams] = useSearchParams()
  const consumed = useRef(false)
  useEffect(() => {
    if (consumed.current) return
    const capability = params.get("capability")
    if (!capability) return
    const incident = params.get("incident")
    const match = shortfalls.find(
      (s) => s.capability === capability && (!incident || s.incidentId === incident)
    )
    // Wait for the first poll rather than giving up: this screen mounts before
    // the world has arrived, and a link that silently did nothing would be
    // worse than one that takes a second.
    if (!shortfalls.length) return
    consumed.current = true
    if (match) {
      setDraft({
        incidentId: match.incidentId,
        wardId: match.incident.wardId,
        capability: match.capability,
        quantity: match.gap,
      })
    }
    // Cleared either way, so a refresh does not re-open a draft the officer
    // has already dealt with.
    setParams({}, { replace: true })
  }, [params, setParams, shortfalls])

  const open = state.agencyRequests.filter(
    (r) => r.status === "requested" || r.status === "acknowledged"
  )
  const byId = useMemo(
    () => new Map(state.agencyRequests.map((r) => [r.id, r] as const)),
    [state.agencyRequests]
  )
  const wardName = useMemo(
    () => new Map(state.wards.map((w) => [w.id, w.name] as const)),
    [state.wards]
  )

  async function send(toAgency: string) {
    if (!draft) return
    await run(`ask-${toAgency}`, "/agency-requests", {
      incidentId: draft.incidentId,
      wardId: draft.wardId,
      toAgency,
      capabilityId: draft.capability,
      quantity: draft.quantity,
      note: note || undefined,
    })
    setDraft(null)
    setNote("")
    await refresh()
  }

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="grid gap-5 md:grid-cols-3">
        <Card className={shortfalls.length ? "border-destructive/50" : undefined}>
          <CardHeader className="pb-3">
            <CardDescription>Needs nobody is covering</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {shortfalls.reduce((n, s) => n + s.gap, 0)}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            Across {shortfalls.length} incident need(s). The planner records a
            shortfall rather than silently under-serving it.
          </CardContent>
        </Card>
        <Card className={open.length ? "border-amber-500/40" : undefined}>
          <CardHeader className="pb-3">
            <CardDescription>Requests awaiting a reply</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{open.length}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Agencies reachable</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {state.agencies.length}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-xs">
            Each one listed with the capabilities it actually holds, so a request
            goes to somebody who can answer it.
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <TriangleAlert className="size-4" /> Gaps you could ask about
            </CardTitle>
            <CardDescription>
              Straight from the allocator's unmet demand. Nothing is typed twice.
            </CardDescription>
          </CardHeader>
          <CardContent className="max-h-[560px] space-y-2 overflow-y-auto">
            {shortfalls.length === 0 && (
              <p className="text-muted-foreground text-xs">
                Every recorded need is covered by the fleet. Nothing to hand off.
              </p>
            )}
            {shortfalls.map((s) => {
              const picked =
                draft?.incidentId === s.incidentId &&
                draft?.capability === s.capability
              return (
                <div
                  key={`${s.incidentId}-${s.capability}`}
                  className={`rounded-md border p-2 ${picked ? "border-primary" : ""}`}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="destructive" className="tabular-nums">
                      short {s.gap}
                    </Badge>
                    <span className="text-sm font-medium">
                      {s.capability.replace(/_/g, " ")}
                    </span>
                    <span className="text-muted-foreground text-xs">
                      {s.incident.title}
                    </span>
                    <span className="text-muted-foreground ml-auto text-xs">
                      sev {s.incident.severity}
                    </span>
                  </div>

                  {s.alreadyAsked ? (
                    <p className="text-muted-foreground mt-1 text-xs">
                      Already asked for. Waiting on a reply below.
                    </p>
                  ) : s.able.length === 0 ? (
                    <p className="text-destructive mt-1 text-xs">
                      No configured agency holds this capability. That is a
                      resourcing gap for the city, not a coordination one, and it
                      is recorded as such.
                    </p>
                  ) : !picked ? (
                    <Button
                      size="sm"
                      variant="outline"
                      className="mt-2 h-7 text-xs"
                      onClick={() =>
                        setDraft({
                          incidentId: s.incidentId,
                          wardId: s.incident.wardId,
                          capability: s.capability,
                          quantity: s.gap,
                        })
                      }
                    >
                      <Handshake className="size-3" />
                      Ask another agency
                    </Button>
                  ) : (
                    <div className="mt-2 space-y-2">
                      <Input
                        value={note}
                        onChange={(e) => setNote(e.target.value)}
                        placeholder="Anything the other agency needs to know"
                        className="h-8 text-xs"
                      />
                      <div className="flex flex-wrap gap-1.5">
                        {s.able.map((a) => (
                          <Button
                            key={a.id}
                            size="sm"
                            className="h-7 text-xs"
                            disabled={busy !== null}
                            onClick={() => void send(a.id)}
                          >
                            {busy === `ask-${a.id}` ? (
                              <Loader2 className="size-3 animate-spin" />
                            ) : (
                              <Send className="size-3" />
                            )}
                            {a.name}
                          </Button>
                        ))}
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-7 text-xs"
                          onClick={() => {
                            setDraft(null)
                            setNote("")
                          }}
                        >
                          Cancel
                        </Button>
                      </div>
                      <p className="text-muted-foreground text-xs">
                        Requesting {s.gap} × {s.capability.replace(/_/g, " ")} for{" "}
                        {wardName.get(s.incident.wardId) ?? s.incident.wardId}.
                      </p>
                    </div>
                  )}
                </div>
              )
            })}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Building2 className="size-4" /> Requests
            </CardTitle>
            <CardDescription>
              The other agency answers; the agent acts on the answer (stages
              their units, asks the next agency, sends details, or escalates).
              Every step is an event.
            </CardDescription>
          </CardHeader>
          <CardContent className="max-h-[560px] space-y-2 overflow-y-auto">
            {state.agencyRequests.length === 0 && (
              <p className="text-muted-foreground text-xs">
                Nothing has been requested yet.
              </p>
            )}
            {state.agencyRequests.map((r) => {
              const st = STATUS[r.status] ?? STATUS.requested
              return (
                <div key={r.id} className="rounded-md border p-2">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="font-medium">{r.fromName ?? r.fromAgency}</span>
                    <ArrowRight className="text-muted-foreground size-3 shrink-0" />
                    <span className="font-medium">{r.toName ?? r.toAgency}</span>
                    <Badge variant="outline" className={`ml-auto ${st.tone}`}>
                      {st.label}
                    </Badge>
                  </div>
                  <div className="text-muted-foreground mt-1 flex flex-wrap gap-x-3 text-xs">
                    <span className="text-foreground">
                      {r.quantity} × {r.capability.replace(/_/g, " ")}
                    </span>
                    {r.wardName && <span>{r.wardName}</span>}
                    {r.incidentTitle && <span>{r.incidentTitle}</span>}
                    <span>asked {time(r.requestedAt)}</span>
                    {r.respondedAt && (
                      <span>
                        answered {time(r.respondedAt)}
                        {r.respondedBy ? ` by ${r.respondedBy}` : ""}
                      </span>
                    )}
                  </div>
                  {r.followupOf && (
                    <p className="text-muted-foreground mt-1 flex items-center gap-1 text-xs">
                      <CornerDownRight className="size-3" />
                      Follow-up by the agent after{" "}
                      {byId.get(r.followupOf)?.toName ?? "an earlier request"}
                      {byId.get(r.followupOf)?.replyKind
                        ? ` (${REPLY[byId.get(r.followupOf)!.replyKind!]?.label.toLowerCase() ?? byId.get(r.followupOf)!.replyKind})`
                        : ""}
                    </p>
                  )}
                  {r.note && !r.followupOf && (
                    <p className="text-muted-foreground mt-1 text-xs italic">
                      &ldquo;{r.note}&rdquo;
                    </p>
                  )}

                  <Conversation r={r} />

                  {(r.status === "requested" || r.status === "acknowledged") && (
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <span className="text-muted-foreground mr-auto text-xs">
                        {r.replyDueAt
                          ? `Their answer is due about ${time(r.replyDueAt)}`
                          : "Waiting for their answer"}
                      </span>
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        disabled={busy !== null}
                        title="They said yes by phone or radio: stage their units now"
                        onClick={() =>
                          run(`ful-${r.id}`, `/agency-requests/${r.id}/fulfil`)
                        }
                      >
                        <Check className="size-3" /> Record yes
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        className="h-7 text-xs"
                        disabled={busy !== null}
                        title="They said no: the agent asks the next agency"
                        onClick={() =>
                          run(`dec-${r.id}`, `/agency-requests/${r.id}/decline`)
                        }
                      >
                        <X className="size-3" /> Record no
                      </Button>
                    </div>
                  )}
                </div>
              )
            })}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

/** The exchange with the other agency, and what the agent did about it. */
function Conversation({ r }: { r: AgencyRequest }) {
  const thread = r.replies ?? []
  if (!thread.length && !r.nextStep && !(r.units?.length)) return null
  return (
    <div className="mt-2 space-y-1.5">
      {thread.map((m, i) => {
        const tag = m.who === "agency" ? REPLY[m.kind] : undefined
        return (
          <div
            key={i}
            className={
              m.who === "agency"
                ? "bg-muted/60 rounded-md px-2.5 py-1.5 text-xs"
                : "border-primary/20 bg-primary/5 ml-6 rounded-md border px-2.5 py-1.5 text-xs"
            }
          >
            <div className="text-muted-foreground mb-0.5 flex flex-wrap items-center gap-1.5 text-[11px]">
              {m.who === "agent" ? <Bot className="size-3" /> : <Building2 className="size-3" />}
              <span className="text-foreground font-medium">{m.by}</span>
              <span>{time(m.at)}</span>
              {tag && (
                <span className={`ml-auto rounded border px-1.5 py-px ${tag.tone}`}>
                  {tag.label}
                </span>
              )}
            </div>
            &ldquo;{m.text}&rdquo;
          </div>
        )
      })}
      {r.nextStep && (
        <div className="flex items-start gap-1.5 text-xs">
          <Bot className="text-primary mt-0.5 size-3.5 shrink-0" />
          <span>
            <span className="font-medium">Agent next step: </span>
            {r.nextStep}
          </span>
        </div>
      )}
      {!!r.units?.length && (
        <div className="flex flex-wrap items-center gap-1 text-xs">
          <Truck className="text-muted-foreground size-3.5" />
          {r.units.map((u) => (
            <Badge key={u} variant="outline" className="font-mono text-[10px]">
              {u}
            </Badge>
          ))}
        </div>
      )}
    </div>
  )
}
