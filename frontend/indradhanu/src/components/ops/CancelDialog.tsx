import { useEffect, useState } from "react"
import { ArrowRight, Loader2 } from "lucide-react"
import { request } from "@/api/httpClient"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"

type Kind = "replan" | "redirect" | "stage" | "hold" | "return_to_base"

type Alt = {
  incidentId: string; title: string; severity: number; ward: string
  capability: string; short: number; km: number
}

type Preview = {
  unit: string
  current: { doing: string; status: string; severity: number | null }
  insteadText: string
  comparison: {
    rows: { metric: string; current: number | null; proposed: number | null
            change: number | null; better: boolean | null }[]
    wards: { ward?: string; name?: string; current: number | null
             proposed: number | null; better: boolean | null }[]
    note?: string
  }
  alternatives: Alt[]
}

const OPTIONS: { kind: Kind; label: string; hint: string }[] = [
  { kind: "replan", label: "Let the planner cover it",
    hint: "This unit is freed; the next solve sends whoever is best. It will not be sent back to this job." },
  { kind: "redirect", label: "Send it somewhere else",
    hint: "Pinned there, so the planner does not undo it." },
  { kind: "stage", label: "Stage it in a ward",
    hint: "Drives to the ward and becomes available from there." },
  { kind: "hold", label: "Hold it where it is",
    hint: "Out of the plan for the minutes you choose (rest, reserve, repair)." },
  { kind: "return_to_base", label: "Send it back to base", hint: "Drives home, then held for 20 minutes." },
]

/** Stop a unit's job, and say what it should do instead.
 *
 *  Everything this dialog does is a proposal. "Put to the policy gate" sends
 *  `cancel_assignment` through the delegation matrix exactly like a Copilot
 *  action; if the clause delegates it, it happens at once, otherwise it waits
 *  on the approvals screen with these parameters attached.
 *
 *  The preview re-solves the real plan on a copy, so the officer sees who
 *  covers the job and which wards get slower before they commit.
 */
export function CancelDialog({
  open, onOpenChange, resourceId, unitLabel, incidents, wards,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  resourceId: string | null
  unitLabel: string
  incidents: { id: string; title: string; severity: number; status: string }[]
  wards: { id: string; name: string }[]
}) {
  const [kind, setKind] = useState<Kind>("replan")
  const [incidentId, setIncidentId] = useState("")
  const [wardId, setWardId] = useState("")
  const [minutes, setMinutes] = useState(30)
  const [reason, setReason] = useState("")
  const [preview, setPreview] = useState<Preview | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) {
      setKind("replan"); setIncidentId(""); setWardId(""); setMinutes(30)
      setReason(""); setPreview(null); setResult(null); setError(null)
    }
  }, [open])

  function instead() {
    const incident = incidents.find((i) => i.id === incidentId)
    const ward = wards.find((w) => w.id === wardId)
    return {
      kind,
      incidentId: kind === "redirect" ? incidentId || undefined : undefined,
      incidentTitle: kind === "redirect" ? incident?.title : undefined,
      wardId: kind === "stage" ? wardId || undefined : undefined,
      wardName: kind === "stage" ? ward?.name : undefined,
      minutes: kind === "hold" ? minutes : undefined,
    }
  }

  // Re-preview whenever the choice changes and is complete.
  useEffect(() => {
    if (!open || !resourceId) return
    if (kind === "redirect" && !incidentId) return
    if (kind === "stage" && !wardId) return
    let live = true
    setLoading(true)
    setError(null)
    request<Preview>("/ops/preview", {
      method: "POST", body: { resourceId, instead: instead(), cityId: "pune" }, toast: false,
    })
      .then((p) => { if (live) setPreview(p) })
      .catch((e) => { if (live) setError(e instanceof Error ? e.message : String(e)) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, resourceId, kind, incidentId, wardId, minutes])

  async function submit() {
    if (!resourceId || reason.trim().length < 3) return
    setBusy(true)
    setError(null)
    try {
      const r = await request<{ summary: string }>("/ops/cancel", {
        method: "POST",
        body: { resourceId, reason: reason.trim(), instead: instead(), cityId: "pune" },
        toast: { loading: "Putting it to the policy gate…", success: (d: unknown) => (d as { summary?: string }).summary ?? "Done." },
      })
      setResult(r.summary)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const open_ = incidents.filter((i) => i.status !== "resolved")

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Cancel {unitLabel}'s job</DialogTitle>
          <DialogDescription>
            {preview
              ? <>Now: <b>{preview.current.doing}</b> ({preview.current.status.replace(/_/g, " ")}).</>
              : "Choose what it should do instead. Nothing happens until the gate rules."}
          </DialogDescription>
        </DialogHeader>

        <fieldset className="space-y-2">
          <legend className="text-muted-foreground mb-1 text-xs font-medium uppercase tracking-wide">
            Instead
          </legend>
          {OPTIONS.map((o) => (
            <label key={o.kind}
                   className={`flex cursor-pointer items-start gap-2 rounded-md border p-2 text-sm ${
                     kind === o.kind ? "border-primary bg-primary/5" : ""}`}>
              <input type="radio" name="instead" className="mt-1" checked={kind === o.kind}
                     onChange={() => setKind(o.kind)} />
              <span>
                <span className="font-medium">{o.label}</span>
                <span className="text-muted-foreground block text-xs">{o.hint}</span>
                {o.kind === "redirect" && kind === "redirect" && (
                  <select className="mt-2 w-full rounded-md border bg-background p-1.5 text-sm"
                          value={incidentId} onChange={(e) => setIncidentId(e.target.value)}>
                    <option value="">Choose an incident…</option>
                    {(preview?.alternatives ?? []).map((a) => (
                      <option key={`alt-${a.incidentId}`} value={a.incidentId}>
                        Suggested: {a.title} · sev {a.severity} · {a.km} km · short {a.short} {a.capability.replace(/_/g, " ")}
                      </option>
                    ))}
                    {open_.map((i) => (
                      <option key={i.id} value={i.id}>sev {i.severity} · {i.title}</option>
                    ))}
                  </select>
                )}
                {o.kind === "stage" && kind === "stage" && (
                  <select className="mt-2 w-full rounded-md border bg-background p-1.5 text-sm"
                          value={wardId} onChange={(e) => setWardId(e.target.value)}>
                    <option value="">Choose a ward…</option>
                    {wards.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                  </select>
                )}
                {o.kind === "hold" && kind === "hold" && (
                  <span className="mt-2 flex items-center gap-2 text-xs">
                    for
                    <input type="number" min={5} max={480} value={minutes}
                           onChange={(e) => setMinutes(Math.max(5, Math.min(480, Number(e.target.value) || 30)))}
                           className="w-20 rounded-md border bg-background p-1" />
                    minutes
                  </span>
                )}
              </span>
            </label>
          ))}
        </fieldset>

        <div>
          <label className="text-muted-foreground text-xs font-medium uppercase tracking-wide" htmlFor="cancel-reason">
            Why (goes on the record and to the crew)
          </label>
          <Textarea id="cancel-reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)}
                    placeholder="e.g. Crew reports the building is already evacuated" />
        </div>

        <div className="rounded-md border p-3 text-xs">
          <div className="mb-2 flex items-center gap-2 font-medium">
            What the plan looks like if you do this
            {loading && <Loader2 className="size-3 animate-spin" />}
          </div>
          {preview ? (
            <>
              <p className="mb-2">{preview.insteadText}</p>
              <table className="w-full tabular-nums">
                <thead className="text-muted-foreground">
                  <tr><th className="text-left font-normal">Measure</th><th className="text-right font-normal">Now</th>
                      <th className="text-right font-normal">After</th></tr>
                </thead>
                <tbody>
                  {preview.comparison.rows.map((r) => (
                    <tr key={r.metric}>
                      <td>{r.metric}</td>
                      <td className="text-right">{r.current ?? "—"}</td>
                      <td className={`text-right ${r.better === false ? "text-destructive" : r.better ? "text-emerald-600" : ""}`}>
                        {r.proposed ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {preview.comparison.note && <p className="text-muted-foreground mt-2">{preview.comparison.note}</p>}
            </>
          ) : (
            <p className="text-muted-foreground">
              {kind === "redirect" ? "Pick an incident to see the effect." :
               kind === "stage" ? "Pick a ward to see the effect." : "Working it out…"}
            </p>
          )}
        </div>

        {error && <p className="text-destructive text-xs">{error}</p>}
        {result && (
          <p className="flex items-start gap-1.5 rounded-md border border-emerald-500/40 bg-emerald-500/5 p-2 text-xs">
            <ArrowRight className="mt-0.5 size-3 shrink-0" /> {result}
          </p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
          <Button onClick={() => void submit()}
                  disabled={busy || !!result || reason.trim().length < 3 ||
                            (kind === "redirect" && !incidentId) || (kind === "stage" && !wardId)}>
            {busy && <Loader2 className="size-3.5 animate-spin" />}
            Put to the policy gate
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
