import { useMemo, useState } from "react"
import { Boxes, Loader2, Truck, Warehouse, Wrench } from "lucide-react"
import { useDemo } from "@/routes/demo/DemoProvider"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow,
} from "@/components/ui/table"
import { Empty } from "./DecisionGate"

/** The inventory, live.
 *
 *  No city publishes unit availability as a feed, so this platform is the system
 *  of record for it: the corporation enters its fleet once on the configuration
 *  screen, and dispatch and closure keep it current from then on. Which means
 *  this page has to be the same rows the allocator is solving over — a fixture
 *  here would be a second, quieter answer to "what have we got", and the wrong
 *  one.
 *
 *  Relief stock sits on the same page for the same reason. "Do we have enough"
 *  is one question with two halves: vehicles that can move things, and the
 *  things at the places they move them to.
 */

const STATUS_TONE: Record<string, string> = {
  available: "bg-emerald-600 text-white",
  assigned: "bg-amber-500 text-black",
  proposed: "bg-amber-500 text-black",
  en_route: "bg-sky-600 text-white",
  on_site: "bg-violet-600 text-white",
  unavailable: "bg-muted text-muted-foreground",
  offline: "bg-muted text-muted-foreground",
}

const pretty = (s: string) => s.replace(/_/g, " ")

/** Stock names are data — a deployment can invent one — so the unit comes from
 *  the name rather than a table this file would have to be edited to extend. */
const unitOf = (item: string) =>
  item.includes("litre") ? "L" : ""

export default function ResourcesPage() {
  const { state, busy, run } = useDemo()
  const [kind, setKind] = useState<string | null>(null)

  const wardName = useMemo(() => {
    const m = new Map<string, string>()
    for (const w of state.wards) m.set(w.id, w.name)
    return m
  }, [state.wards])

  const fleet = useMemo(() => {
    const rows = kind ? state.resources.filter((r) => r.kind === kind) : state.resources
    return [...rows].sort(
      (a, b) => a.kind.localeCompare(b.kind) || a.label.localeCompare(b.label)
    )
  }, [state.resources, kind])

  const kinds = useMemo(() => {
    const counts = new Map<string, number>()
    for (const r of state.resources) counts.set(r.kind, (counts.get(r.kind) ?? 0) + 1)
    return [...counts.entries()].sort((a, b) => b[1] - a[1])
  }, [state.resources])

  const available = state.resources.filter((r) => r.status === "available").length
  const operators = new Set(state.resources.map((r) => r.operator)).size

  /** Units that are neither working nor available.
   *
   *  This is the number the sidebar counts, and until now it was a number and
   *  nothing else: the fleet table showed `offline` in a status column, buried
   *  among everything else, with the reason in a truncated cell — and there was
   *  no action anywhere, for any role, that put a vehicle back. A crew could
   *  take a truck out with a puncture and it stayed out for the rest of the
   *  event, because the verb did not exist (migration 017 adds it).
   *
   *  Deliberately not "not available": a crew working an incident is not a
   *  problem, and counting them would make this list longest exactly when the
   *  city is busiest and the list least useful. */
  const out = useMemo(
    () => state.resources.filter((r) => r.status !== "available" && !r.assignedTo),
    [state.resources]
  )

  /** Shelter pressure, over everything that actually shelters people rather
   *  than over the two kinds this page used to know the names of. */
  const shelter = useMemo(() => {
    const rows = state.facilities.filter((f) => (f.capacity ?? 0) > 0)
    return {
      rows: [...rows].sort(
        (a, b) =>
          (b.occupancy ?? 0) / Math.max(1, b.capacity ?? 1) -
          (a.occupancy ?? 0) / Math.max(1, a.capacity ?? 1)
      ),
      occupancy: rows.reduce((n, f) => n + (f.occupancy ?? 0), 0),
      capacity: rows.reduce((n, f) => n + (f.capacity ?? 0), 0),
    }
  }, [state.facilities])

  /** Every stock line in the city, by item. The items are whatever the
   *  lifelines carry; nothing here is hardcoded to food and water. */
  const stock = useMemo(() => {
    const totals = new Map<string, number>()
    for (const f of state.facilities) {
      for (const [item, qty] of Object.entries(f.supplies ?? {})) {
        totals.set(item, (totals.get(item) ?? 0) + (Number(qty) || 0))
      }
    }
    return [...totals.entries()].sort((a, b) => b[1] - a[1])
  }, [state.facilities])

  const supplyPoints = state.facilities.filter(
    (f) => Object.keys(f.supplies ?? {}).length > 0
  )

  if (!state.resources.length) {
    return (
      <Empty
        icon={<Truck className="text-muted-foreground size-8" />}
        title="No units registered"
        body="Register the fleet on the configuration screen. Until something is in the inventory the allocator has nothing to allocate, and this page is the honest version of that."
      />
    )
  }

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="grid gap-5 md:grid-cols-4">
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Units in fleet</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{state.resources.length}</CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground pt-0 text-xs">
            across {operators} operator{operators === 1 ? "" : "s"}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Available</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{available}</CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground pt-0 text-xs">
            {state.resources.length - available} committed right now
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Shelter occupancy</CardDescription>
            <CardTitle className="text-2xl tabular-nums">
              {shelter.occupancy}/{shelter.capacity}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground pt-0 text-xs">
            {shelter.rows.length} place{shelter.rows.length === 1 ? "" : "s"} with beds
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Distribution points</CardDescription>
            <CardTitle className="text-2xl tabular-nums">{supplyPoints.length}</CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground pt-0 text-xs">
            holding {stock.length} kind{stock.length === 1 ? "" : "s"} of stock
          </CardContent>
        </Card>
      </div>

      {out.length > 0 && (
        <Card className="border-amber-500/40">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Wrench className="size-4" />
              Out of the fleet
              <span className="text-muted-foreground font-normal tabular-nums">
                {out.length}
              </span>
            </CardTitle>
            <CardDescription>
              Each of these is capacity the allocator cannot use, with the reason
              the crew gave. Returning one puts it back in the pool for the next
              plan; its old task is not restored, because that task was released
              when it went out and belongs to whoever the solver has given it to
              since.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {out.map((r) => (
              <div
                key={r.id}
                className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-2"
              >
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5 text-sm">
                    <span className="font-medium">{r.label}</span>
                    <Badge variant="outline" className="font-normal">
                      {pretty(r.kind)}
                    </Badge>
                    <span className="text-muted-foreground text-xs">{r.operator}</span>
                  </div>
                  <p className="text-muted-foreground mt-0.5 text-xs">
                    {r.unavailableReason ?? pretty(r.status)}
                    {r.statusNote ? ` — ${r.statusNote}` : ""}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-7 shrink-0 text-xs"
                  disabled={busy !== null}
                  onClick={() =>
                    void run(`back-${r.id}`, "/field/status", {
                      subjectType: "resource",
                      subjectId: r.id,
                      statusKind: "back_in_service",
                      note: "Returned to service from the console.",
                    })
                  }
                >
                  {busy === `back-${r.id}` ? (
                    <Loader2 className="size-3 animate-spin" />
                  ) : (
                    <Wrench className="size-3" />
                  )}
                  Back in service
                </Button>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {stock.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Boxes className="size-4" /> Relief stock across the city
            </CardTitle>
            <CardDescription>
              What is on the shelves right now. It drains as people are served
              and goes back up when a supply run arrives — both are ordinary
              assignments, not a separate logistics system.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
            {stock.map(([item, qty]) => (
              <div key={item} className="rounded-md border p-2">
                <div className="text-muted-foreground text-xs">{pretty(item)}</div>
                <div className="text-lg font-semibold tabular-nums">
                  {qty.toLocaleString()}{unitOf(item)}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-sm">
            <Truck className="size-4" /> Fleet
          </CardTitle>
          <div className="flex flex-wrap gap-1 pt-1">
            <Button
              size="sm" variant={kind === null ? "secondary" : "ghost"}
              className="h-7 text-xs" onClick={() => setKind(null)}
            >
              All {state.resources.length}
            </Button>
            {kinds.map(([k, n]) => (
              <Button
                key={k} size="sm" variant={kind === k ? "secondary" : "ghost"}
                className="h-7 text-xs" onClick={() => setKind(k)}
              >
                {pretty(k)} {n}
              </Button>
            ))}
          </div>
        </CardHeader>
        <CardContent className="px-0">
          <div className="max-h-[28rem] overflow-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Unit</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Operator</TableHead>
                  <TableHead>Can do</TableHead>
                  <TableHead className="text-right">Capacity</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>On</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {fleet.map((r) => {
                  const incident = state.incidents.find((i) => i.id === r.incidentId)
                  return (
                    <TableRow key={r.id}>
                      <TableCell className="font-medium">{r.label}</TableCell>
                      <TableCell className="text-muted-foreground text-sm">
                        {pretty(r.kind)}
                      </TableCell>
                      <TableCell className="text-sm">{r.operator}</TableCell>
                      <TableCell className="max-w-[14rem]">
                        <div className="flex flex-wrap gap-1">
                          {r.capabilities.slice(0, 3).map((c) => (
                            <Badge key={c} variant="outline" className="font-normal">
                              {pretty(c)}
                            </Badge>
                          ))}
                          {r.capabilities.length > 3 && (
                            <span className="text-muted-foreground text-xs">
                              +{r.capabilities.length - 3}
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">{r.capacity}</TableCell>
                      <TableCell>
                        <Badge className={STATUS_TONE[r.status] ?? ""}>
                          {pretty(r.status)}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-muted-foreground max-w-[16rem] truncate text-xs">
                        {incident
                          ? `${incident.title}${r.etaMinutes != null ? ` · ETA ${r.etaMinutes}m` : ""}`
                          /* The reason first when there is one. A unit out of
                             the fleet has a cause, and the note beside it was
                             winning a column that should have been showing it. */
                          : r.unavailableReason
                            ? `${r.unavailableReason}${r.statusNote ? ` — ${r.statusNote}` : ""}`
                            : r.statusNote || "—"}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      {shelter.rows.length > 0 && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Warehouse className="size-4" /> Places with beds
            </CardTitle>
            <CardDescription>
              Ordered by how full they are, because the one at the top is the one
              the citizen agent will stop sending people to next.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2.5">
            {shelter.rows.map((f) => {
              const pct = Math.min(
                100, ((f.occupancy ?? 0) / Math.max(1, f.capacity ?? 1)) * 100
              )
              return (
                <div key={f.id} className="space-y-1">
                  <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
                    <span className="font-medium">{f.name}</span>
                    <span className="text-muted-foreground text-xs tabular-nums">
                      {f.kindLabel} · {wardName.get(f.wardId ?? "") ?? "—"} ·{" "}
                      {f.occupancy ?? 0}/{f.capacity ?? 0}
                      {f.status !== "open" && ` · ${pretty(f.status)}`}
                    </span>
                  </div>
                  <Progress value={pct} className="h-1.5" />
                </div>
              )
            })}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
