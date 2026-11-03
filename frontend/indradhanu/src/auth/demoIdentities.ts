/** The seeded demo identities, in one place.
 *
 *  These used to live inside `Login.tsx`, which meant the login screen was the
 *  only place that knew them. That is the wrong shape for a judged demo: a
 *  person opening `/citizen` or `/field` directly has no reason to visit
 *  `/login` first, and telling them "go to another screen to find out how to
 *  sign in" is a step that exists only to be walked through.
 *
 *  The password is not a literal in this file. It comes from the environment,
 *  with a development-only fallback, so a production build contains no working
 *  credential — see `DEMO_ENABLED` below, which is statically false in such a
 *  build and lets the bundler drop everything that reads the password.
 */

export type DemoRole =
  | "commissioner"
  | "ward_officer"
  | "field_operator"
  | "citizen"

export type DemoIdentity = {
  email: string
  /** The role the backend will report for this account, from `profiles`. */
  role: DemoRole
  label: string
  who: string
  can: string
  /** Which portal this identity is for. Used to show the right credentials on
   *  the right screen rather than all five everywhere. */
  portal: "citizen" | "field" | "admin"
  tone: string
}

const FLAG = String(import.meta.env.VITE_DEMO_LOGINS ?? "").toLowerCase()

/** On during development, and off in a build unless somebody says otherwise.
 *
 *  The leak that mattered was the production bundle: a deployed page served
 *  five working staff logins and a shared password to anyone who opened it. A
 *  Vite dev server is a different thing — it is bound to this machine and it is
 *  how the demo is run, so making the operator edit an env file before they can
 *  sign in is friction with no security in it.
 *
 *  `VITE_DEMO_LOGINS=true` enables it in a build for a judged demo;
 *  `VITE_DEMO_LOGINS=false` turns it off even in development.
 */
export const DEMO_ENABLED =
  FLAG === "true" || (import.meta.env.DEV && FLAG !== "false")

/** Never read in a production build: `DEMO_ENABLED` is false there and nothing
 *  calls the code that uses it. Overridable so a demo build can be given its
 *  own password rather than the seeded one. */
export const DEMO_PASSWORD =
  String(import.meta.env.VITE_DEMO_PASSWORD ?? "") ||
  (import.meta.env.DEV ? "Indradhanu#2026" : "")

export const DEMO_IDENTITIES: DemoIdentity[] = [
  {
    email: "commissioner@pune.indradhanu.local",
    role: "commissioner",
    label: "Commissioner",
    who: "Meera Deshpande",
    can: "Authorises evacuations and NDRF requests. Can create logins.",
    portal: "admin",
    tone: "border-violet-500/40",
  },
  {
    email: "officer@pune.indradhanu.local",
    role: "ward_officer",
    label: "Ward officer",
    who: "Rahul Kulkarni",
    can: "The whole console. Approves what the gate holds back.",
    portal: "admin",
    tone: "border-sky-500/40",
  },
  {
    email: "fire@pune.indradhanu.local",
    role: "field_operator",
    label: "Field — Fire Brigade",
    who: "Sana Shaikh",
    can: "Only Fire Brigade units and tasks.",
    portal: "field",
    tone: "border-orange-500/40",
  },
  {
    email: "pmc@pune.indradhanu.local",
    role: "field_operator",
    label: "Field — PMC Drainage",
    who: "Vikram Jadhav",
    can: "Only PMC Drainage units and tasks.",
    portal: "field",
    tone: "border-teal-500/40",
  },
  {
    email: "citizen@pune.indradhanu.local",
    role: "citizen",
    label: "Resident",
    who: "Anita Joshi",
    can: "The public view, with a signed-in reporting history.",
    portal: "citizen",
    tone: "border-slate-500/40",
  },
]

/** Where a role belongs once it is signed in. */
export const LANDING: Record<string, string> = {
  commissioner: "/admin/wall",
  ward_officer: "/admin/wall",
  admin: "/admin/wall",
  field_operator: "/field",
  citizen: "/citizen",
}

/** Which portal a path belongs to, so a person bounced to the login screen from
 *  `/admin/copilot` is shown the two identities that can open it rather than
 *  all five. */
export function portalOf(path: string | undefined): DemoIdentity["portal"] {
  if (!path) return "admin"
  if (path.startsWith("/field")) return "field"
  if (path.startsWith("/citizen")) return "citizen"
  return "admin"
}

export const identitiesFor = (portal: DemoIdentity["portal"]) =>
  DEMO_IDENTITIES.filter((d) => d.portal === portal)
