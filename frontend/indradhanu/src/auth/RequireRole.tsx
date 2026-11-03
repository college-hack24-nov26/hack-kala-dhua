import type { ReactNode } from "react"
import { Navigate, useLocation } from "react-router-dom"
import { useAuth } from "@/auth/AuthProvider"

/** Route guard.
 *
 *  This decides what to RENDER, never what is permitted. Every endpoint behind
 *  these screens checks the same role server-side and the database checks it
 *  again in row level security, so a user who edits their way past this guard
 *  reaches an interface that returns 403 to everything. Hiding a screen is a
 *  courtesy; the refusal is enforced twice, elsewhere.
 */
export function RequireRole({
  children,
  need,
}: {
  children: ReactNode
  need: "staff" | "field" | "citizen" | "commissioner"
}) {
  const { me, loading, session, entitlementUnavailable } = useAuth()
  const loc = useLocation()

  if (loading) {
    return (
      <div className="text-muted-foreground flex min-h-svh items-center justify-center text-sm">
        Checking your access…
      </div>
    )
  }

  const ok =
    need === "citizen" ||
    (need === "staff" && me.isStaff) ||
    (need === "commissioner" && me.canEscalate) ||
    (need === "field" && (me.isStaff || me.role === "field_operator"))

  if (!ok) {
    /* Signed in, and we cannot find out what that entitles them to.
     *
     *  This used to fall through to the redirect below, which sent somebody who
     *  had just signed in successfully back to the login screen with no message
     *  — indistinguishable from a rejected password, and the natural response is
     *  to type it again, which also appears to fail. The cause is almost always
     *  a stopped API, and saying so is the whole fix. */
    if (session && entitlementUnavailable) {
      return (
        <div className="flex min-h-svh flex-col items-center justify-center gap-2 p-6 text-center">
          <p className="font-medium">You are signed in, but this screen cannot open yet.</p>
          <p className="text-muted-foreground max-w-md text-sm">
            Your sign-in worked. What it entitles you to is decided by the API,
            and the API is not answering, so this view cannot know whether to
            show you anything. It will open as soon as the service is back —
            nothing is wrong with your account.
          </p>
          <p className="text-muted-foreground max-w-md text-xs">
            If this is your own machine: start the backend, then reload.
          </p>
        </div>
      )
    }
    if (!me.authenticated) {
      return <Navigate to="/login" state={{ from: loc.pathname }} replace />
    }
    return (
      <div className="flex min-h-svh flex-col items-center justify-center gap-2 p-6 text-center">
        <p className="font-medium">This view is not part of your delegation.</p>
        <p className="text-muted-foreground max-w-md text-sm">
          You are signed in as {me.fullName || "an unnamed user"} (
          {me.role.replace(/_/g, " ")}). The operations console is restricted to
          municipal staff.
        </p>
      </div>
    )
  }
  return <>{children}</>
}
