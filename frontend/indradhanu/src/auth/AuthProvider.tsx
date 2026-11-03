import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"
import type { Session } from "@supabase/supabase-js"
import { nativeSession, notifySignedOut } from "@/lib/native"
import { supabase, supabaseConfigured } from "@/lib/supabase"
import { fetchMe, type Me } from "@/api/httpClient"

/** Session plus entitlement.
 *
 *  Supabase tells us who is signed in. It does not tell us what they are
 *  allowed to do: role, ward delegation and agency live in `profiles` and are
 *  resolved by the backend at `/auth/me`, through the same code path that
 *  authorises every other call. So the client asks rather than infers, and
 *  nothing here is trusted for access control - it only decides what to render.
 *  The server refuses anything the role does not permit regardless of what this
 *  object says.
 */

const ANONYMOUS: Me = {
  authenticated: false,
  userId: null,
  role: "citizen",
  fullName: "",
  wardId: null,
  operator: null,
  isStaff: false,
  canEscalate: false,
  interfaces: ["citizen"],
  developmentIdentity: false,
}

type AuthState = {
  session: Session | null
  me: Me
  loading: boolean
  configured: boolean
  /** Supabase says this person is signed in, but `/auth/me` could not be
   *  reached, so we do not know what they may do. Distinct from being signed
   *  out, and the guard must not treat the two the same: bouncing somebody
   *  back to a login screen they just used correctly reads as "wrong
   *  password", and they will type it again. */
  entitlementUnavailable: boolean
  /** Resolves once the entitlement is known, and hands it back.
   *
   *  Returning `me` is the point. A caller that signs in and then reads `me`
   *  off the context is reading the binding from the render it was defined in,
   *  which is still the anonymous one — a new entitlement means a *new* `me`
   *  in a *later* render, and the closure that called `signIn` never sees it.
   *  That sent every signed-in commissioner to the citizen portal, because
   *  anonymous is a citizen. No delay fixes it; the value has to come back out
   *  of the call. */
  signIn: (
    email: string,
    password: string
  ) => Promise<{ error: string | null; me: Me | null }>
  signUp: (
    email: string,
    password: string,
    fullName: string
  ) => Promise<{ error: string | null; needsConfirmation: boolean }>
  signOut: () => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [me, setMe] = useState<Me>(ANONYMOUS)
  const [loading, setLoading] = useState(true)
  const [entitlementUnavailable, setEntitlementUnavailable] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setMe(await fetchMe())
      setEntitlementUnavailable(false)
    } catch (err) {
      setEntitlementUnavailable(true)
      // The backend being unreachable is not the same as being signed out. Stay
      // anonymous for rendering, but do not throw the user back to a login
      // screen for what is probably a stopped uvicorn.
      console.warn("[indradhanu] /auth/me unavailable", err)
      setMe(ANONYMOUS)
    }
  }, [])

  useEffect(() => {
    let alive = true
    if (!supabaseConfigured) {
      // No Supabase configured: the backend may still hand us a development
      // identity via DEV_AUTH_ROLE, so ask anyway.
      void refresh().finally(() => alive && setLoading(false))
      return () => {
        alive = false
      }
    }

    supabase.auth.getSession().then(async ({ data }) => {
      if (!alive) return
      // Inside the BiChat phone app, the person already signed in on the phone:
      // adopt that session rather than asking for the password a second time.
      const phone = nativeSession()
      if (phone && phone.email !== data.session?.user?.email) {
        const { data: adopted } = await supabase.auth.setSession({
          access_token: phone.accessToken, refresh_token: phone.refreshToken,
        })
        if (!alive) return
        if (adopted.session) {
          setSession(adopted.session)
          await refresh()
          if (alive) setLoading(false)
          return
        }
      }
      setSession(data.session)
      await refresh()
      if (alive) setLoading(false)
    })

    const { data: sub } = supabase.auth.onAuthStateChange(async (_event, next) => {
      if (!alive) return
      setSession(next)
      await refresh()
    })
    return () => {
      alive = false
      sub.subscription.unsubscribe()
    }
  }, [refresh])

  const signIn = useCallback(async (email: string, password: string) => {
    if (!supabaseConfigured) {
      return { error: "Supabase is not configured in this build.", me: null }
    }
    const { error } = await supabase.auth.signInWithPassword({ email, password })
    if (error) return { error: error.message, me: null }

    // Ask who this is before returning. `accessToken()` reads the session live
    // from the Supabase client, which already holds the new one by the time the
    // call above resolves, so this carries the right bearer. `onAuthStateChange`
    // will also fire and refresh; doing it here too is one extra request and
    // the difference between the caller knowing the role and guessing it.
    try {
      const fresh = await fetchMe()
      setMe(fresh)
      setEntitlementUnavailable(false)
      return { error: null, me: fresh }
    } catch (err) {
      // Signed in, entitlement unknown. Not an error to show as a failed
      // password — the guard has its own screen for exactly this.
      console.warn("[indradhanu] signed in but /auth/me unavailable", err)
      setEntitlementUnavailable(true)
      return { error: null, me: null }
    }
  }, [])

  const signUp = useCallback(
    async (email: string, password: string, fullName: string) => {
      if (!supabaseConfigured) {
        return { error: "Supabase is not configured in this build.", needsConfirmation: false }
      }
      const { data, error } = await supabase.auth.signUp({
        email,
        password,
        // Only the display name. A role passed here would be ignored anyway:
        // `app.handle_new_user` gives every new account the citizen role and
        // deliberately does not read a role out of signup metadata, because a
        // role you can ask for at signup is not a role, it is a door.
        options: { data: { full_name: fullName } },
      })
      if (error) return { error: error.message, needsConfirmation: false }
      // No session back means the project requires email confirmation.
      return { error: null, needsConfirmation: data.session === null }
    },
    []
  )

  const signOut = useCallback(async () => {
    if (supabaseConfigured) await supabase.auth.signOut()
    notifySignedOut()
    setSession(null)
    setMe(ANONYMOUS)
    setEntitlementUnavailable(false)
  }, [])

  const value = useMemo<AuthState>(
    () => ({
      session,
      me,
      loading,
      configured: supabaseConfigured,
      entitlementUnavailable,
      signIn,
      signUp,
      signOut,
      refresh,
    }),
    [session, me, loading, entitlementUnavailable, signIn, signUp, signOut, refresh]
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>")
  return ctx
}
