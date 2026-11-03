import { useCallback, useEffect, useMemo, useState } from "react"
import {
  Building2, CheckCircle2, Loader2, MapPin, Plus, Settings2,
  Trash2, Truck, UserPlus, Users,
} from "lucide-react"
import { request } from "@/api/httpClient"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { LiveMap } from "@/components/map/LiveMap"

/** Standing up a deployment, without psql.
 *
 *  The portability claim has been true in the schema since the first migration:
 *  hazards, categories, capabilities and resource kinds are rows, not enums. But
 *  a claim nobody can exercise is a claim nobody believes, and "we would just
 *  run some SQL" is not an answer a municipal IT department accepts.
 *
 *  So this is the same portability with a front door. Add a city, add its
 *  subdivisions, register the fleet and the relief facilities, create the staff
 *  logins. Every write lands in the tables the Pune deployment already uses, and
 *  the moment a ward exists the risk agent scores it and the allocator can send
 *  units into it.
 */

type Deployment = {
  cities: { id: string; name: string; country: string; timezone: string
            wards: number; units: number; lifelines: number }[]
  wards: { id: string; name: string; number: string; population: number
           cityId: string; centroid: [number, number]; boundaryDrawn: boolean
           openIncidents: number; lifelines: number }[]
  agencies: { id: string; name: string; short_name: string; kind: string
              jurisdiction: string; active: boolean }[]
  resources: { id: string; kind: string; label: string; operator: string
               agencyId: string | null; capacity: number; status: string
               committed: number; location: [number, number] }[]
  lifelines: { id: string; kind: string; name: string; wardId: string | null
               capacity: number | null; occupancy: number | null; status: string
               supplies: Record<string, number>; servedPerHour: number | null
               location: [number, number] }[]
  people: { id: string; fullName: string; role: string
            wardId: string | null; operator: string | null }[]
  taxonomy: {
    resourceKinds: { id: string; label: string; defaultCapacity: number
                     capabilities: string[] }[]
    lifelineKinds: { id: string; label: string }[]
    capabilities: { id: string; label: string }[]
    categories: { id: string; label: string; lifeSafety: boolean }[]
    roles: string[]
  }
}

type Tab = "city" | "wards" | "fleet" | "facilities" | "people"

const TABS: [Tab, string, typeof MapPin][] = [
  ["city", "City", Building2],
  ["wards", "Wards", MapPin],
  ["fleet", "Fleet", Truck],
  ["facilities", "Facilities", Settings2],
  ["people", "People", Users],
]

const ROLE_NOTE: Record<string, string> = {
  citizen: "Sees only their own surroundings. No console access.",
  field_operator: "One agency's units and tasks. Needs an operator.",
  ward_officer: "The whole console. Can approve what the gate holds.",
  commissioner: "Everything, including actions the matrix reserves upward.",
  admin: "Everything, plus this screen.",
}

function Field({
  label, hint, children,
}: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <Label className="text-xs">{label}</Label>
      {children}
      {hint && <p className="text-muted-foreground text-[11px]">{hint}</p>}
    </div>
  )
}

export default function Configuration() {
  const [tab, setTab] = useState<Tab>("city")
  const [data, setData] = useState<Deployment | null>(null)
  const [cityId, setCityId] = useState("pune")
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const load = useCallback(async (city: string) => {
    try {
      setData(await request<Deployment>("/config/deployment", { query: { cityId: city } }))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => { void load(cityId) }, [load, cityId])

  const act = useCallback(
    async (key: string, path: string, body: unknown, method: "POST" | "DELETE" = "POST") => {
      setBusy(key); setError(null); setDone(null)
      try {
        const r = await request<Record<string, unknown>>(path, {
          method, body: method === "POST" ? body : undefined,
        })
        setDone(
          typeof r.note === "string"
            ? r.note
            : `Saved${r.id ? ` as ${String(r.id)}` : ""}.`
        )
        await load(cityId)
        return r
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
        throw e
      } finally { setBusy(null) }
    },
    [cityId, load]
  )

  // ---- forms -------------------------------------------------------------
  const [city, setCity] = useState({
    id: "", name: "", country: "IN", timezone: "Asia/Kolkata",
    adminUnitSingular: "ward", adminUnitPlural: "wards", lng: "", lat: "",
  })
  const [ward, setWard] = useState({
    id: "", name: "", number: "", population: "", lng: "", lat: "", areaSqKm: "4",
  })
  const [unit, setUnit] = useState({
    kind: "", label: "", operator: "", agencyId: "", capacity: "", lng: "", lat: "",
  })
  const [place, setPlace] = useState({
    kind: "", name: "", wardId: "", capacity: "", lng: "", lat: "",
    foodPackets: "", waterLitres: "", medicalKits: "", servedPerHour: "",
  })
  const [person, setPerson] = useState({
    email: "", password: "", fullName: "", role: "ward_officer",
    wardId: "", operator: "",
  })

  const kinds = data?.taxonomy.resourceKinds ?? []
  const placeKinds = data?.taxonomy.lifelineKinds ?? []
  const chosenKind = kinds.find((k) => k.id === unit.kind)

  const fleetByKind = useMemo(() => {
    const m = new Map<string, number>()
    for (const r of data?.resources ?? []) m.set(r.kind, (m.get(r.kind) ?? 0) + 1)
    return m
  }, [data?.resources])

  const placesByKind = useMemo(() => {
    const m = new Map<string, number>()
    for (const l of data?.lifelines ?? []) m.set(l.kind, (m.get(l.kind) ?? 0) + 1)
    return m
  }, [data?.lifelines])

  if (!data) {
    return (
      <div className="p-5 md:p-8 xl:px-10">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-sm">
              <Loader2 className="size-4 animate-spin" /> Reading the deployment
            </CardTitle>
            {error && <CardDescription className="text-destructive text-xs">{error}</CardDescription>}
          </CardHeader>
        </Card>
      </div>
    )
  }

  return (
    <div className="space-y-8 p-5 md:p-8 xl:px-10">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-1">
          {TABS.map(([k, label, Icon]) => (
            <Button
              key={k}
              size="sm"
              variant={tab === k ? "secondary" : "ghost"}
              className="h-8 gap-1.5 text-xs"
              onClick={() => setTab(k)}
            >
              <Icon className="size-3.5" />
              {label}
            </Button>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Label className="text-muted-foreground text-xs">Deployment</Label>
          <select
            className="bg-background h-8 rounded-md border px-2 text-xs"
            value={cityId}
            onChange={(e) => setCityId(e.target.value)}
          >
            {data.cities.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
        </div>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertDescription className="text-xs">{error}</AlertDescription>
        </Alert>
      )}
      {done && (
        <Alert>
          <CheckCircle2 className="size-4" />
          <AlertDescription className="text-xs">{done}</AlertDescription>
        </Alert>
      )}

      {/* ------------------------------------------------------------ city */}
      {tab === "city" && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm">Deployments</CardTitle>
              <CardDescription>
                Every one of these runs the same code. Nothing about Pune is
                compiled in; it is rows, like the rest.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-2">
              {data.cities.map((c) => (
                <div key={c.id} className="flex flex-wrap items-center gap-2 rounded border p-2">
                  <span className="text-sm font-medium">{c.name}</span>
                  <Badge variant="outline">{c.id}</Badge>
                  <span className="text-muted-foreground text-xs">{c.timezone}</span>
                  <span className="text-muted-foreground ml-auto text-xs tabular-nums">
                    {c.wards} wards · {c.units} units · {c.lifelines} facilities
                  </span>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Plus className="size-4" /> Add a city
              </CardTitle>
              <CardDescription>
                The subdivision words are used throughout the interface, so a
                deployment that calls them zones or circles says zones or circles.
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-2 sm:grid-cols-2">
              <Field label="Identifier" hint="Lower case, no spaces. Used in URLs.">
                <Input className="h-8 text-xs" value={city.id} placeholder="nashik"
                       onChange={(e) => setCity({ ...city, id: e.target.value })} />
              </Field>
              <Field label="Name">
                <Input className="h-8 text-xs" value={city.name} placeholder="Nashik"
                       onChange={(e) => setCity({ ...city, name: e.target.value })} />
              </Field>
              <Field label="Country"><Input className="h-8 text-xs" value={city.country}
                       onChange={(e) => setCity({ ...city, country: e.target.value })} /></Field>
              <Field label="Timezone"><Input className="h-8 text-xs" value={city.timezone}
                       onChange={(e) => setCity({ ...city, timezone: e.target.value })} /></Field>
              <Field label="Subdivision, singular"><Input className="h-8 text-xs"
                       value={city.adminUnitSingular}
                       onChange={(e) => setCity({ ...city, adminUnitSingular: e.target.value })} /></Field>
              <Field label="Subdivision, plural"><Input className="h-8 text-xs"
                       value={city.adminUnitPlural}
                       onChange={(e) => setCity({ ...city, adminUnitPlural: e.target.value })} /></Field>
              <Field label="Centre longitude"><Input className="h-8 text-xs" value={city.lng}
                       placeholder="73.79" onChange={(e) => setCity({ ...city, lng: e.target.value })} /></Field>
              <Field label="Centre latitude"><Input className="h-8 text-xs" value={city.lat}
                       placeholder="19.99" onChange={(e) => setCity({ ...city, lat: e.target.value })} /></Field>
              <div className="sm:col-span-2">
                <Button
                  size="sm" className="w-full text-xs" disabled={busy !== null}
                  onClick={() => act("city", "/config/cities", {
                    id: city.id.trim(), name: city.name.trim(),
                    country: city.country, timezone: city.timezone,
                    adminUnitSingular: city.adminUnitSingular,
                    adminUnitPlural: city.adminUnitPlural,
                    lng: Number(city.lng), lat: Number(city.lat),
                  })}
                >
                  {busy === "city" && <Loader2 className="size-3.5 animate-spin" />}
                  Create deployment
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* ----------------------------------------------------------- wards */}
      {tab === "wards" && (
        <div className="grid gap-5 lg:grid-cols-[1fr_380px]">
          <Card className="min-w-0">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm">
                {data.wards.length} subdivisions
              </CardTitle>
              <CardDescription>
                A ward with no real boundary gets a square around its centre,
                sized from its area. Reports still locate correctly; the shape is
                wrong until the municipal geometry replaces it, and the badge
                below says which is which.
              </CardDescription>
            </CardHeader>
            <CardContent className="max-h-[460px] space-y-1 overflow-y-auto">
              {data.wards.map((w) => (
                <div key={w.id} className="flex flex-wrap items-center gap-2 rounded border p-2 text-xs">
                  <span className="font-medium">{w.name}</span>
                  <Badge variant="outline">#{w.number}</Badge>
                  <span className="text-muted-foreground tabular-nums">
                    {w.population.toLocaleString()} people
                  </span>
                  {!w.boundaryDrawn && (
                    <Badge variant="secondary" className="font-normal">approximate shape</Badge>
                  )}
                  {w.openIncidents > 0 && (
                    <Badge variant="destructive">{w.openIncidents} open</Badge>
                  )}
                  <span className="text-muted-foreground">{w.lifelines} facilities</span>
                  <Button
                    size="sm" variant="ghost" className="ml-auto h-6 px-2 text-xs"
                    disabled={busy !== null}
                    onClick={() => act(`w-${w.id}`, `/config/wards/${w.id}`, null, "DELETE")}
                  >
                    <Trash2 className="size-3" />
                  </Button>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-sm">
                <Plus className="size-4" /> Add a subdivision
              </CardTitle>
            </CardHeader>
            <CardContent className="grid gap-2 sm:grid-cols-2">
              <Field label="Identifier"><Input className="h-8 text-xs" value={ward.id}
                       placeholder="w-21" onChange={(e) => setWard({ ...ward, id: e.target.value })} /></Field>
              <Field label="Number"><Input className="h-8 text-xs" value={ward.number}
                       placeholder="21" onChange={(e) => setWard({ ...ward, number: e.target.value })} /></Field>
              <div className="sm:col-span-2">
                <Field label="Name"><Input className="h-8 text-xs" value={ward.name}
                         placeholder="Panchavati" onChange={(e) => setWard({ ...ward, name: e.target.value })} /></Field>
              </div>
              <Field label="Population"><Input className="h-8 text-xs" value={ward.population}
                       placeholder="48000" onChange={(e) => setWard({ ...ward, population: e.target.value })} /></Field>
              <Field label="Area km²" hint="Sizes the placeholder shape.">
                <Input className="h-8 text-xs" value={ward.areaSqKm}
                       onChange={(e) => setWard({ ...ward, areaSqKm: e.target.value })} />
              </Field>
              <Field label="Centre longitude"><Input className="h-8 text-xs" value={ward.lng}
                       onChange={(e) => setWard({ ...ward, lng: e.target.value })} /></Field>
              <Field label="Centre latitude"><Input className="h-8 text-xs" value={ward.lat}
                       onChange={(e) => setWard({ ...ward, lat: e.target.value })} /></Field>
              <div className="sm:col-span-2">
                <Button size="sm" className="w-full text-xs" disabled={busy !== null}
                  onClick={() => act("ward", "/config/wards", {
                    id: ward.id.trim(), cityId, name: ward.name.trim(),
                    number: ward.number.trim(), population: Number(ward.population || 0),
                    lng: Number(ward.lng), lat: Number(ward.lat),
                    areaSqKm: Number(ward.areaSqKm || 4),
                  })}
                >
                  {busy === "ward" && <Loader2 className="size-3.5 animate-spin" />}
                  Add subdivision
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* ----------------------------------------------------------- fleet */}
      {tab === "fleet" && (
        <div className="space-y-3">
          <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-5">
            {kinds.map((k) => (
              <Card key={k.id}>
                <CardHeader className="pb-3">
                  <CardDescription>{k.label}</CardDescription>
                  <CardTitle className="text-xl tabular-nums">
                    {fleetByKind.get(k.id) ?? 0}
                  </CardTitle>
                </CardHeader>
                <CardContent className="text-muted-foreground text-[11px]">
                  {k.capabilities.map((c) => c.replace(/_/g, " ")).join(", ") || "no capabilities"}
                </CardContent>
              </Card>
            ))}
          </div>

          <div className="grid gap-5 lg:grid-cols-[1fr_380px]">
            <Card className="min-w-0">
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">{data.resources.length} units</CardTitle>
                <CardDescription>
                  Capability comes from the kind, not from anything typed here.
                  Registering a boat makes it eligible for water rescue and for
                  nothing else.
                </CardDescription>
              </CardHeader>
              <CardContent className="max-h-[420px] space-y-1 overflow-y-auto">
                {data.resources.map((r) => (
                  <div key={r.id} className="flex flex-wrap items-center gap-2 rounded border p-2 text-xs">
                    <Badge variant="outline">{r.kind.replace(/_/g, " ")}</Badge>
                    <span className="font-medium">{r.label}</span>
                    <span className="text-muted-foreground">{r.operator}</span>
                    <Badge variant={r.status === "available" ? "secondary" : "default"}>
                      {r.status.replace(/_/g, " ")}
                    </Badge>
                    {r.committed > 0 && <Badge variant="destructive">on a task</Badge>}
                    <Button
                      size="sm" variant="ghost" className="ml-auto h-6 px-2"
                      disabled={busy !== null}
                      onClick={() => act(`r-${r.id}`, `/config/resources/${r.id}`, null, "DELETE")}
                    >
                      <Trash2 className="size-3" />
                    </Button>
                  </div>
                ))}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center gap-2 text-sm">
                  <Plus className="size-4" /> Register a unit
                </CardTitle>
              </CardHeader>
              <CardContent className="grid gap-2 sm:grid-cols-2">
                <div className="sm:col-span-2">
                  <Field label="Kind"
                         hint={chosenKind
                           ? `Gains: ${chosenKind.capabilities.map((c) => c.replace(/_/g, " ")).join(", ")}`
                           : "What it can do follows from this."}>
                    <select
                      className="bg-background h-8 w-full rounded-md border px-2 text-xs"
                      value={unit.kind}
                      onChange={(e) => setUnit({ ...unit, kind: e.target.value })}
                    >
                      <option value="">Choose a kind</option>
                      {kinds.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
                    </select>
                  </Field>
                </div>
                <div className="sm:col-span-2">
                  <Field label="Label"><Input className="h-8 text-xs" value={unit.label}
                           placeholder="Rescue boat 14" onChange={(e) => setUnit({ ...unit, label: e.target.value })} /></Field>
                </div>
                <Field label="Operator"><Input className="h-8 text-xs" value={unit.operator}
                         placeholder="Nashik Fire" onChange={(e) => setUnit({ ...unit, operator: e.target.value })} /></Field>
                <Field label="Agency">
                  <select className="bg-background h-8 w-full rounded-md border px-2 text-xs"
                          value={unit.agencyId}
                          onChange={(e) => setUnit({ ...unit, agencyId: e.target.value })}>
                    <option value="">None</option>
                    {data.agencies.map((a) => <option key={a.id} value={a.id}>{a.short_name}</option>)}
                  </select>
                </Field>
                <Field label="Capacity" hint={chosenKind ? `Default ${chosenKind.defaultCapacity}` : ""}>
                  <Input className="h-8 text-xs" value={unit.capacity}
                         onChange={(e) => setUnit({ ...unit, capacity: e.target.value })} />
                </Field>
                <Field label="Longitude"><Input className="h-8 text-xs" value={unit.lng}
                         onChange={(e) => setUnit({ ...unit, lng: e.target.value })} /></Field>
                <Field label="Latitude"><Input className="h-8 text-xs" value={unit.lat}
                         onChange={(e) => setUnit({ ...unit, lat: e.target.value })} /></Field>
                <div className="sm:col-span-2">
                  <Button size="sm" className="w-full text-xs" disabled={busy !== null || !unit.kind}
                    onClick={() => act("unit", "/config/resources", {
                      cityId, kind: unit.kind, label: unit.label.trim(),
                      operator: unit.operator.trim(),
                      agencyId: unit.agencyId || null,
                      capacity: unit.capacity ? Number(unit.capacity) : null,
                      lng: Number(unit.lng), lat: Number(unit.lat),
                    })}
                  >
                    {busy === "unit" && <Loader2 className="size-3.5 animate-spin" />}
                    Register unit
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      )}

      {/* ------------------------------------------------------ facilities */}
      {tab === "facilities" && (
        <div className="space-y-3">
          <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-5">
            {placeKinds.map((k) => (
              <Card key={k.id}>
                <CardHeader className="pb-3">
                  <CardDescription>{k.label}</CardDescription>
                  <CardTitle className="text-xl tabular-nums">
                    {placesByKind.get(k.id) ?? 0}
                  </CardTitle>
                </CardHeader>
              </Card>
            ))}
          </div>

          <div className="grid gap-5 lg:grid-cols-[1fr_380px]">
            <Card className="min-w-0">
              <CardHeader className="pb-3">
                <CardTitle className="text-sm">{data.lifelines.length} facilities</CardTitle>
                <CardDescription>
                  Opening stock is what "low" is measured against later, so a
                  centre that opens with 200 packets raises a shortage at 50 and
                  one that opens with 2,000 does not.
                </CardDescription>
              </CardHeader>
              <CardContent className="max-h-[420px] space-y-1 overflow-y-auto">
                {data.lifelines.map((l) => (
                  <div key={l.id} className="flex flex-wrap items-center gap-2 rounded border p-2 text-xs">
                    <Badge variant="outline">{l.kind.replace(/_/g, " ")}</Badge>
                    <span className="font-medium">{l.name}</span>
                    {l.capacity ? (
                      <span className="text-muted-foreground tabular-nums">
                        {l.occupancy ?? 0}/{l.capacity}
                      </span>
                    ) : null}
                    {Object.entries(l.supplies ?? {}).slice(0, 2).map(([k, v]) => (
                      <span key={k} className="text-muted-foreground tabular-nums">
                        {Number(v).toLocaleString()} {k.replace(/_/g, " ")}
                      </span>
                    ))}
                    <Button
                      size="sm" variant="ghost" className="ml-auto h-6 px-2"
                      disabled={busy !== null}
                      onClick={() => act(`l-${l.id}`, `/config/lifelines/${l.id}`, null, "DELETE")}
                    >
                      <Trash2 className="size-3" />
                    </Button>
                  </div>
                ))}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center gap-2 text-sm">
                  <Plus className="size-4" /> Add a facility
                </CardTitle>
              </CardHeader>
              <CardContent className="grid gap-2 sm:grid-cols-2">
                <div className="sm:col-span-2">
                  <Field label="Kind">
                    <select className="bg-background h-8 w-full rounded-md border px-2 text-xs"
                            value={place.kind}
                            onChange={(e) => setPlace({ ...place, kind: e.target.value })}>
                      <option value="">Choose a kind</option>
                      {placeKinds.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
                    </select>
                  </Field>
                </div>
                <div className="sm:col-span-2">
                  <Field label="Name"><Input className="h-8 text-xs" value={place.name}
                           placeholder="Panchavati Relief Centre"
                           onChange={(e) => setPlace({ ...place, name: e.target.value })} /></Field>
                </div>
                <Field label="Subdivision">
                  <select className="bg-background h-8 w-full rounded-md border px-2 text-xs"
                          value={place.wardId}
                          onChange={(e) => setPlace({ ...place, wardId: e.target.value })}>
                    <option value="">None</option>
                    {data.wards.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                  </select>
                </Field>
                <Field label="Capacity"><Input className="h-8 text-xs" value={place.capacity}
                         onChange={(e) => setPlace({ ...place, capacity: e.target.value })} /></Field>
                <Field label="Food packets"><Input className="h-8 text-xs" value={place.foodPackets}
                         onChange={(e) => setPlace({ ...place, foodPackets: e.target.value })} /></Field>
                <Field label="Water litres"><Input className="h-8 text-xs" value={place.waterLitres}
                         onChange={(e) => setPlace({ ...place, waterLitres: e.target.value })} /></Field>
                <Field label="Medical kits"><Input className="h-8 text-xs" value={place.medicalKits}
                         onChange={(e) => setPlace({ ...place, medicalKits: e.target.value })} /></Field>
                <Field label="Served per hour"><Input className="h-8 text-xs" value={place.servedPerHour}
                         onChange={(e) => setPlace({ ...place, servedPerHour: e.target.value })} /></Field>
                <Field label="Longitude"><Input className="h-8 text-xs" value={place.lng}
                         onChange={(e) => setPlace({ ...place, lng: e.target.value })} /></Field>
                <Field label="Latitude"><Input className="h-8 text-xs" value={place.lat}
                         onChange={(e) => setPlace({ ...place, lat: e.target.value })} /></Field>
                <div className="sm:col-span-2">
                  <Button size="sm" className="w-full text-xs" disabled={busy !== null || !place.kind}
                    onClick={() => {
                      const supplies: Record<string, number> = {}
                      if (place.foodPackets) supplies.food_packets = Number(place.foodPackets)
                      if (place.waterLitres) supplies.water_litres = Number(place.waterLitres)
                      if (place.medicalKits) supplies.medical_kits = Number(place.medicalKits)
                      return act("place", "/config/lifelines", {
                        cityId, kind: place.kind, name: place.name.trim(),
                        wardId: place.wardId || null,
                        capacity: Number(place.capacity || 0),
                        acceptsCasualties: place.kind === "hospital" || place.kind === "medical_camp",
                        supplies,
                        peopleServedPerHour: place.servedPerHour ? Number(place.servedPerHour) : null,
                        lng: Number(place.lng), lat: Number(place.lat),
                      })
                    }}
                  >
                    {busy === "place" && <Loader2 className="size-3.5 animate-spin" />}
                    Add facility
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      )}

      {/* ---------------------------------------------------------- people */}
      {tab === "people" && (
        <div className="grid gap-5 lg:grid-cols-[1fr_380px]">
          <Card className="min-w-0">
            <CardHeader className="pb-3">
              <CardTitle className="text-sm">{data.people.length} accounts</CardTitle>
              <CardDescription>
                The role lives in the profile, never in the signup. That is the
                whole reason this screen exists rather than letting people
                register themselves with a role: the database policies authorise
                against the profile, so whatever can set it is a privilege
                boundary and belongs behind one.
              </CardDescription>
            </CardHeader>
            <CardContent className="max-h-[460px] space-y-1 overflow-y-auto">
              {data.people.map((p) => (
                <div key={p.id} className="flex flex-wrap items-center gap-2 rounded border p-2 text-xs">
                  <span className="font-medium">{p.fullName}</span>
                  <Badge variant={p.role === "citizen" ? "outline" : "secondary"}>
                    {p.role.replace(/_/g, " ")}
                  </Badge>
                  {p.wardId && <span className="text-muted-foreground">{p.wardId}</span>}
                  {p.operator && <span className="text-muted-foreground">{p.operator}</span>}
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-sm">
                <UserPlus className="size-4" /> Create a login
              </CardTitle>
              <CardDescription>
                Commissioner only. The account is created confirmed, so they can
                sign in straight away.
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-2">
              <Field label="Full name"><Input className="h-8 text-xs" value={person.fullName}
                       onChange={(e) => setPerson({ ...person, fullName: e.target.value })} /></Field>
              <Field label="Email"><Input className="h-8 text-xs" type="email" value={person.email}
                       autoComplete="off"
                       onChange={(e) => setPerson({ ...person, email: e.target.value })} /></Field>
              <Field label="Password" hint="At least eight characters. They can change it after.">
                <Input className="h-8 text-xs" type="password" value={person.password}
                       autoComplete="new-password"
                       onChange={(e) => setPerson({ ...person, password: e.target.value })} />
              </Field>
              <Field label="Role" hint={ROLE_NOTE[person.role]}>
                <select className="bg-background h-8 w-full rounded-md border px-2 text-xs"
                        value={person.role}
                        onChange={(e) => setPerson({ ...person, role: e.target.value })}>
                  {data.taxonomy.roles.map((r) => (
                    <option key={r} value={r}>{r.replace(/_/g, " ")}</option>
                  ))}
                </select>
              </Field>
              {person.role === "ward_officer" && (
                <Field label="Subdivision">
                  <select className="bg-background h-8 w-full rounded-md border px-2 text-xs"
                          value={person.wardId}
                          onChange={(e) => setPerson({ ...person, wardId: e.target.value })}>
                    <option value="">Whole city</option>
                    {data.wards.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
                  </select>
                </Field>
              )}
              {person.role === "field_operator" && (
                <Field label="Operator" hint="Must match the units' operator exactly, or they sign in to an empty fleet.">
                  <Input className="h-8 text-xs" value={person.operator}
                         placeholder="Pune Fire Brigade"
                         onChange={(e) => setPerson({ ...person, operator: e.target.value })} />
                </Field>
              )}
              <Button size="sm" className="w-full text-xs" disabled={busy !== null}
                onClick={() => act("person", "/config/people", {
                  email: person.email.trim(), password: person.password,
                  fullName: person.fullName.trim(), role: person.role,
                  wardId: person.wardId || null,
                  operator: person.operator.trim() || null,
                }).then(() => setPerson({ ...person, email: "", password: "", fullName: "" }))}
              >
                {busy === "person" && <Loader2 className="size-3.5 animate-spin" />}
                Create login
              </Button>
            </CardContent>
          </Card>
        </div>
      )}

      {(tab === "wards" || tab === "fleet" || tab === "facilities") && (
        <LiveMap
          className="h-[360px] w-full rounded-lg border"
          wards={data.wards.map((w) => ({
            id: w.id, name: w.name, number: w.number, boundary: null,
            severity: null, score: null, population: w.population,
          }))}
          resources={data.resources.map((r) => ({
            id: r.id, kind: r.kind, label: r.label, status: r.status,
            operator: r.operator, capacity: r.capacity, location: r.location,
          }))}
          facilities={data.lifelines.map((l) => ({
            id: l.id, name: l.name, kind: l.kind, status: l.status,
            capacity: l.capacity, occupancy: l.occupancy,
            supplies: l.supplies, location: l.location,
          }))}
          center={data.wards[0]?.centroid ?? [73.88, 18.58]}
          zoom={10.5}
        />
      )}
    </div>
  )
}
