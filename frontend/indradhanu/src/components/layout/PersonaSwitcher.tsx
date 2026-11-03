import { useLocation, useNavigate } from "react-router-dom"
import { Building2, HardHat, LogOut, User } from "lucide-react"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { Button } from "@/components/ui/button"
import { useAuth } from "@/auth/AuthProvider"

const PERSONAS = [
  { id: "admin", label: "Administration", short: "Admin", to: "/admin/wall", icon: Building2 },
  { id: "citizen", label: "Citizen", short: "Citizen", to: "/citizen", icon: User },
  { id: "field", label: "Field operator", short: "Field", to: "/field", icon: HardHat },
]

/** The three interfaces, switchable in one click so a judge can see the same
 *  moment in the event from all three sides.
 *
 *  Which ones appear now comes from `/auth/me`, so a field operator does not
 *  get shown a console they cannot open. That is presentation only: the route
 *  guard, the API and row level security each refuse independently.
 */
export function PersonaSwitcher() {
  const nav = useNavigate()
  const { pathname } = useLocation()
  const { me, signOut } = useAuth()

  const allowed = PERSONAS.filter((p) => me.interfaces.includes(p.id))
  const active = pathname.startsWith("/citizen")
    ? "citizen"
    : pathname.startsWith("/field")
      ? "field"
      : "admin"

  return (
    <div className="flex items-center gap-2">
      {allowed.length > 1 && (
        <ToggleGroup
          type="single"
          value={active}
          onValueChange={(v) => {
            const p = allowed.find((x) => x.id === v)
            if (p) nav(p.to)
          }}
          variant="outline"
          size="sm"
        >
          {allowed.map((p) => (
            <ToggleGroupItem key={p.id} value={p.id} aria-label={p.label} className="gap-1.5 px-2.5">
              <p.icon className="size-3.5" />
              <span className="hidden text-xs sm:inline">{p.short}</span>
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      )}

      {me.authenticated ? (
        <div className="flex items-center gap-2">
          <div className="hidden text-right leading-tight sm:block">
            <div className="text-xs font-medium">{me.fullName || "Signed in"}</div>
            <div className="text-muted-foreground text-[11px]">
              {me.role.replace(/_/g, " ")}
              {me.wardId ? ` · ${me.wardId}` : ""}
              {me.operator ? ` · ${me.operator}` : ""}
            </div>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void signOut().then(() => nav("/login"))}
            aria-label="Sign out"
          >
            <LogOut className="size-3.5" />
          </Button>
        </div>
      ) : (
        <>
          {me.developmentIdentity && (
            <span
              className="rounded border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-600 dark:text-amber-400"
              title="DEV_AUTH_ROLE is granting this identity without a token. Development only."
            >
              dev identity
            </span>
          )}
          <Button variant="outline" size="sm" onClick={() => nav("/login")}>
            Sign in
          </Button>
        </>
      )}
    </div>
  )
}
