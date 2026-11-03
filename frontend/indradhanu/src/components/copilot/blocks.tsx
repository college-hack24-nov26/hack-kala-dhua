import type { ReactNode } from "react"
import {
  ArrowDown, ArrowRight, ArrowUp, CircleDot, Minus, ShieldAlert, TriangleAlert,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"

/** How the Copilot's answers are drawn.
 *
 *  The Copilot never returns markdown. It returns a list of typed blocks, and
 *  this renders them. That split is the whole reason the feature is worth
 *  having: a ranked table with real column headings, a comparison with arrows
 *  in the right direction, an evidence panel you can scan — none of which
 *  survives being flattened into a paragraph, and all of which a commissioner
 *  reads faster than prose.
 *
 *  It also means the numbers on screen are the numbers the backend computed.
 *  There is no formatting path where a model gets to re-type a figure.
 */

export type Block = Record<string, any> & { type: string }

/** Returns a string or null, never `unknown`.
 *
 *  It used to return `v` unchanged in the else branch, so its type was
 *  `string | unknown` — which React will not accept as a child, and which made
 *  every `{num(x) ?? "—"}` in this file a type error. Null is the right empty
 *  value because that is what the `?? "—"` at each call site is testing for. */
const num = (v: unknown): string | null => {
  if (typeof v === "number") {
    return Number.isInteger(v) ? v.toLocaleString() : v.toFixed(1)
  }
  if (v === null || v === undefined || v === "") return null
  return String(v)
}

/** Cell rendering, by what the value *is* rather than by column configuration.
 *
 *  Backend tables come from a dozen different queries and hand-listing formats
 *  for every column of every one of them is how this file would rot. A ratio
 *  between 0 and 1 in a column called `confidence` is a percentage; an ISO
 *  timestamp is a time; everything else is what it is.
 */
function cell(key: string, value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return <span className="text-muted-foreground">—</span>
  if (typeof value === "boolean") return value ? "yes" : "no"
  if (Array.isArray(value)) return value.join(", ")

  const k = key.toLowerCase()
  if (typeof value === "number") {
    if (/score$|confidence|trust|evidence|coverage|p_?atleastone/i.test(k) && value <= 1 && value >= 0) {
      return <span className="tabular-nums">{(value * 100).toFixed(0)}%</span>
    }
    if (/severity|worst/.test(k)) return <SeverityPip value={value} />
    return <span className="tabular-nums">{num(value)}</span>
  }
  if (typeof value === "string") {
    if (/^\d{4}-\d{2}-\d{2}T/.test(value)) {
      return (
        <span className="tabular-nums">
          {new Date(value).toLocaleTimeString(undefined, {
            hour: "2-digit", minute: "2-digit", hour12: false,
          })}
        </span>
      )
    }
    if (/status|pressure|verification/.test(k)) return <Tone value={value} />
    return value
  }
  if (typeof value === "object") return JSON.stringify(value)
  return String(value)
}

function SeverityPip({ value }: { value: number }) {
  const tone =
    value >= 5 ? "bg-red-600" : value >= 4 ? "bg-orange-500"
    : value >= 3 ? "bg-amber-500" : "bg-slate-400"
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`size-2 rounded-full ${tone}`} />
      <span className="tabular-nums">{value}</span>
    </span>
  )
}

const TONE: Record<string, string> = {
  available: "text-emerald-600 dark:text-emerald-400",
  open: "text-emerald-600 dark:text-emerald-400",
  steady: "text-emerald-600 dark:text-emerald-400",
  confirmed: "text-emerald-600 dark:text-emerald-400",
  auto_issued: "text-emerald-600 dark:text-emerald-400",
  approved: "text-emerald-600 dark:text-emerald-400",
  tightening: "text-amber-600 dark:text-amber-400",
  awaiting_approval: "text-amber-600 dark:text-amber-400",
  pending: "text-amber-600 dark:text-amber-400",
  saturating: "text-red-600 dark:text-red-400",
  full: "text-red-600 dark:text-red-400",
  closed: "text-red-600 dark:text-red-400",
  rejected: "text-red-600 dark:text-red-400",
}

function Tone({ value }: { value: string }) {
  return (
    <span className={TONE[value] ?? ""}>{value.replace(/_/g, " ")}</span>
  )
}

function Delta({ change, better }: { change: number | null; better: boolean | null }) {
  if (change === null || change === undefined) {
    return <span className="text-muted-foreground">—</span>
  }
  if (change === 0) {
    return (
      <span className="text-muted-foreground inline-flex items-center gap-1">
        <Minus className="size-3" /> no change
      </span>
    )
  }
  const tone =
    better === true ? "text-emerald-600 dark:text-emerald-400"
    : better === false ? "text-red-600 dark:text-red-400"
    : "text-muted-foreground"
  const Icon = change < 0 ? ArrowDown : ArrowUp
  return (
    <span className={`inline-flex items-center gap-1 tabular-nums ${tone}`}>
      <Icon className="size-3" />
      {Math.abs(change).toLocaleString()}
    </span>
  )
}

const RISK: Record<string, string> = {
  low: "border-emerald-500/50 text-emerald-700 dark:text-emerald-400",
  medium: "border-amber-500/50 text-amber-700 dark:text-amber-400",
  high: "border-red-500/50 text-red-700 dark:text-red-400",
}

// --------------------------------------------------------------- the blocks ---
export function BlockView({
  block, onPropose,
}: {
  block: Block
  onPropose?: (actions: any[], strategyId?: string) => void
}) {
  switch (block.type) {
    case "text":
      return <p className="text-sm leading-relaxed">{block.body}</p>

    case "stats":
      return (
        <div>
          {block.title && <Label>{block.title}</Label>}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {block.stats.map((s: any) => (
              <div key={s.label} className="rounded-md border p-2">
                <div className="text-muted-foreground text-[11px]">{s.label}</div>
                <div
                  className={`text-lg font-semibold tabular-nums ${
                    s.tone === "bad" ? "text-red-600 dark:text-red-400"
                    : s.tone === "warn" ? "text-amber-600 dark:text-amber-400" : ""
                  }`}
                >
                  {num(s.value)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )

    case "table":
      return (
        <div>
          {block.title && <Label>{block.title}</Label>}
          <div className="overflow-x-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  {block.columns.map((c: any) => (
                    <TableHead key={c.key} className="whitespace-nowrap">{c.label}</TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {block.rows.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={block.columns.length}
                               className="text-muted-foreground text-center text-xs">
                      Nothing here right now.
                    </TableCell>
                  </TableRow>
                ) : block.rows.map((row: any, i: number) => (
                  <TableRow key={row.id ?? i}>
                    {block.columns.map((c: any) => (
                      <TableCell key={c.key} className="text-sm">
                        {cell(c.key, row[c.key])}
                      </TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {block.note && <Note>{block.note}</Note>}
        </div>
      )

    case "comparison":
      return (
        <div>
          {block.title && <Label>{block.title}</Label>}
          <div className="overflow-x-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Impact</TableHead>
                  <TableHead className="text-right">Now</TableHead>
                  <TableHead className="text-right">Proposed</TableHead>
                  <TableHead className="text-right">Change</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {block.rows.map((r: any) => (
                  <TableRow key={r.metric}>
                    <TableCell className="text-sm font-medium">{r.metric}</TableCell>
                    <TableCell className="text-right text-sm tabular-nums">{num(r.current) ?? "—"}</TableCell>
                    <TableCell className="text-right text-sm tabular-nums">{num(r.proposed) ?? "—"}</TableCell>
                    <TableCell className="text-right text-sm">
                      <Delta change={r.change} better={r.better} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {block.wards?.length > 0 && (
            <>
              <Label>Who pays for it</Label>
              <div className="overflow-x-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Ward</TableHead>
                      <TableHead className="text-right">Now</TableHead>
                      <TableHead className="text-right">Proposed</TableHead>
                      <TableHead className="text-right">Change</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {block.wards.map((w: any) => (
                      <TableRow key={w.ward}>
                        <TableCell className="text-sm">{w.ward}</TableCell>
                        <TableCell className="text-right text-sm tabular-nums">{w.current ?? "no cover"}</TableCell>
                        <TableCell className="text-right text-sm tabular-nums">{w.proposed ?? "no cover"}</TableCell>
                        <TableCell className="text-right text-sm">
                          <Delta change={w.change} better={w.better} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </>
          )}
          <Note>
            {block.engine ? `Re-solved with ${block.engine}. ` : ""}
            {block.note || "Nothing has been changed; this is a solve on a copy."}
          </Note>
        </div>
      )

    case "terms":
      return (
        <div>
          {block.title && <Label>{block.title}</Label>}
          <div className="space-y-2 rounded-md border p-3">
            {block.terms.map((t: any) => (
              <div key={t.term} className="space-y-1">
                <div className="flex items-baseline justify-between gap-2 text-xs">
                  <span className="font-medium">{t.term}</span>
                  <span className="text-muted-foreground tabular-nums">
                    {num(t.value)} · {t.contribution} of {Math.round(t.weight * 100)} points
                  </span>
                </div>
                <Progress value={(t.contribution / (t.weight * 100)) * 100} className="h-1.5" />
              </div>
            ))}
          </div>
          {block.comparison?.length > 0 && (
            <div className="mt-2 overflow-x-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Term</TableHead>
                    <TableHead className="text-right">{block.subject}</TableHead>
                    <TableHead className="text-right">{block.against}</TableHead>
                    <TableHead className="text-right">Difference</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {block.comparison.map((c: any) => (
                    <TableRow key={c.term}>
                      <TableCell className="text-sm">{c.term}</TableCell>
                      <TableCell className="text-right text-sm tabular-nums">{c.contribution_a}</TableCell>
                      <TableCell className="text-right text-sm tabular-nums">{c.contribution_b}</TableCell>
                      <TableCell className="text-right text-sm tabular-nums">
                        {(c.contribution_a - c.contribution_b).toFixed(1)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          {block.note && <Note>{block.note}</Note>}
        </div>
      )

    case "cards":
      return (
        <div>
          {block.title && <Label>{block.title}</Label>}
          <div className="grid gap-2 sm:grid-cols-2">
            {block.cards.map((c: any, i: number) => (
              <Card key={i} className={c.tone === "warn" ? "border-amber-500/50" : ""}>
                <CardHeader className="pb-2">
                  <CardTitle className="text-sm">{c.title}</CardTitle>
                  <CardDescription className="text-xs">{c.subtitle}</CardDescription>
                </CardHeader>
                <CardContent className="space-y-0.5 pt-0">
                  {c.lines.map((l: any) => (
                    <div key={l.label} className="flex justify-between gap-2 text-xs">
                      <span className="text-muted-foreground">{l.label}</span>
                      <span className="text-right">{l.value}</span>
                    </div>
                  ))}
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )

    case "evidence":
      return (
        <div>
          {block.title && <Label>{block.title}</Label>}
          <div className="grid gap-x-6 gap-y-1 rounded-md border p-3 sm:grid-cols-2">
            {block.items.map((it: any) => (
              <div key={it.label} className="flex justify-between gap-3 text-xs">
                <span className="text-muted-foreground">{it.label}</span>
                <span className="text-right font-medium">{String(it.value)}</span>
              </div>
            ))}
          </div>
          {block.note && <Note>{block.note}</Note>}
        </div>
      )

    case "timeline":
      return (
        <div>
          {block.title && <Label>{block.title}</Label>}
          <div className="max-h-80 space-y-1.5 overflow-auto rounded-md border p-3">
            {block.events.map((e: any) => (
              <div key={e.id} className="flex gap-2 text-xs">
                <span className="text-muted-foreground shrink-0 tabular-nums">
                  {new Date(e.at).toLocaleTimeString(undefined, {
                    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
                  })}
                </span>
                <CircleDot className="text-muted-foreground mt-0.5 size-3 shrink-0" />
                <span className="min-w-0">
                  <span className="font-medium">{e.kind.replace(/[._]/g, " ")}</span>
                  <span className="text-muted-foreground"> · {e.actor.replace(/[:_]/g, " ")}</span>
                </span>
              </div>
            ))}
          </div>
          {block.note && <Note>{block.note}</Note>}
        </div>
      )

    case "strategies":
      return <Strategies block={block} onPropose={onPropose} />

    case "actions":
      return (
        <div>
          {block.title && <Label>{block.title}</Label>}
          <div className="space-y-1.5 rounded-md border p-3">
            {block.actions.length === 0 ? (
              <p className="text-muted-foreground text-xs">
                This option does nothing, which is the point of it.
              </p>
            ) : block.actions.map((a: any, i: number) => (
              <div key={i} className="flex items-start gap-2 text-xs">
                <ArrowRight className="mt-0.5 size-3 shrink-0" />
                <span>
                  <span className="font-medium">{a.action}</span>
                  <span className="text-muted-foreground"> · {a.actionKey.replace(/_/g, " ")}</span>
                </span>
              </div>
            ))}
            {onPropose && block.actions.length > 0 && (
              <button
                type="button"
                onClick={() => onPropose(block.actions, block.strategyId)}
                className="bg-primary text-primary-foreground hover:bg-primary/90 mt-2 w-full rounded-md px-3 py-2 text-xs font-medium"
              >
                Put {block.actions.length} action{block.actions.length === 1 ? "" : "s"} to the policy gate
              </button>
            )}
          </div>
          {block.note && <Note>{block.note}</Note>}
        </div>
      )

    case "proposals":
      return (
        <div>
          <Label>What the gate did</Label>
          <div className="space-y-2">
            {block.proposals.map((p: any) => (
              <div key={p.id}
                   className={`rounded-md border p-2.5 text-xs ${
                     p.status === "auto_issued" ? "border-emerald-500/50" : "border-amber-500/50"
                   }`}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">{p.action}</span>
                  <Badge variant="outline">
                    {p.status === "auto_issued"
                      ? p.executed
                        ? "done"
                        // Authorised and not carried out is a third state, and
                        // showing it as "authorised" let it read as finished.
                        // `note` says why; the badge should not disagree with it.
                        : "authorised, not carried out"
                      : "needs an officer"}
                  </Badge>
                </div>
                <p className="text-muted-foreground mt-1">{p.reason}</p>
                {/* Which clause decided, and — when it was held — who it is
                    reserved to. "Waiting for an officer" without naming the
                    clause or the officer is the system asserting authority it
                    will not show its working for, which is the opposite of the
                    argument this product makes. */}
                {p.clause && (
                  <p className="text-muted-foreground mt-1">
                    {p.status === "auto_issued"
                      ? `Authorised under ${p.clause}.`
                      : `Held under ${p.clause}` +
                        (p.delegatedTo
                          ? ` — reserved to the ${String(p.delegatedTo).replace(/_/g, " ")}.`
                          : ".")}
                  </p>
                )}
                {p.note && <p className="mt-1 text-amber-700 dark:text-amber-400">{p.note}</p>}
                {p.result?.etaMinutes != null && (
                  <p className="mt-1">
                    {p.result.resource} en route, {p.result.etaMinutes} min
                    {p.result.engine ? ` (${p.result.engine})` : ""}.
                  </p>
                )}
              </div>
            ))}
          </div>
          {block.summary && <Note>{block.summary}</Note>}
        </div>
      )

    default:
      return null
  }
}

function Strategies({
  block, onPropose,
}: {
  block: Block
  onPropose?: (actions: any[], strategyId?: string) => void
}) {
  return (
    <div className="space-y-3">
      {block.title && <Label>{block.title}</Label>}

      {block.matrix?.rows?.length > 0 && (
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Metric</TableHead>
                {block.matrix.columns.map((c: any) => (
                  <TableHead key={c.id} className="text-right whitespace-nowrap">
                    {c.title.length > 26 ? `${c.title.slice(0, 26)}…` : c.title}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {block.matrix.rows.map((r: any) => (
                <TableRow key={r.metric}>
                  <TableCell className="text-sm font-medium">{r.metric}</TableCell>
                  {block.matrix.columns.map((c: any) => (
                    <TableCell key={c.id} className="text-right text-sm tabular-nums">
                      {r[c.id] === null || r[c.id] === undefined ? "—" : String(r[c.id])}
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {block.strategies.map((s: any) => (
        <Card key={s.id} className={RISK[s.risk] ?? ""}>
          <CardHeader className="pb-2">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <CardTitle className="text-sm">{s.title}</CardTitle>
              <Badge variant="outline" className={RISK[s.risk] ?? ""}>{s.risk} risk</Badge>
            </div>
            <CardDescription className="text-xs">{s.summary}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2.5">
            <p className="text-xs leading-relaxed">{s.rationale}</p>

            {s.expected?.rows?.length > 0 && (
              <div className="overflow-x-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="text-xs">Impact</TableHead>
                      <TableHead className="text-right text-xs">Now</TableHead>
                      <TableHead className="text-right text-xs">After</TableHead>
                      <TableHead className="text-right text-xs">Change</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {s.expected.rows.map((r: any) => (
                      <TableRow key={r.metric}>
                        <TableCell className="text-xs">{r.metric}</TableCell>
                        <TableCell className="text-right text-xs tabular-nums">{num(r.current) ?? "—"}</TableCell>
                        <TableCell className="text-right text-xs tabular-nums">{num(r.proposed) ?? "—"}</TableCell>
                        <TableCell className="text-right text-xs">
                          <Delta change={r.change} better={r.better} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}

            <div>
              <div className="text-muted-foreground mb-1 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide">
                <TriangleAlert className="size-3" /> What it costs
              </div>
              <ul className="space-y-0.5">
                {s.drawbacks.map((d: string, i: number) => (
                  <li key={i} className="text-xs leading-relaxed">— {d}</li>
                ))}
              </ul>
            </div>

            {s.evidence?.length > 0 && (
              <p className="text-muted-foreground text-[11px]">
                Rests on: {s.evidence.join("; ")}.
              </p>
            )}

            {s.actions.length > 0 && onPropose && (
              <button
                type="button"
                onClick={() => onPropose(s.actions, s.id)}
                className="border-primary/50 hover:bg-primary/10 w-full rounded-md border px-3 py-2 text-xs font-medium"
              >
                <ShieldAlert className="mr-1 inline size-3" />
                Put {s.actions.length} action{s.actions.length === 1 ? "" : "s"} to the policy gate
              </button>
            )}
          </CardContent>
        </Card>
      ))}

      {block.note && <Note>{block.note}</Note>}
    </div>
  )
}

function Label({ children }: { children: ReactNode }) {
  return (
    <div className="text-muted-foreground mb-1 mt-1 text-[11px] font-medium uppercase tracking-wide">
      {children}
    </div>
  )
}

function Note({ children }: { children: ReactNode }) {
  return <p className="text-muted-foreground mt-1.5 text-[11px] leading-relaxed">{children}</p>
}
