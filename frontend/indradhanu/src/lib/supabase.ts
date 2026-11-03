import { createClient } from "@supabase/supabase-js"

/** The Supabase browser client.
 *
 *  Only the anon (publishable) key belongs here. It is public by design: every
 *  query it makes is filtered by row level security using the signed-in user's
 *  JWT, which is why `profiles`, `decisions` and `resources` are readable only
 *  to the people the policies allow. The service role key must never appear in
 *  frontend code; it bypasses RLS entirely.
 */
const url = import.meta.env.VITE_SUPABASE_URL as string | undefined
const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined

export const supabaseConfigured = Boolean(url && anonKey)

if (!supabaseConfigured) {
  console.warn(
    "[indradhanu] VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY is missing. " +
      "Sign-in is disabled and the app will run as an anonymous citizen."
  )
}

export const supabase = createClient(url ?? "http://localhost", anonKey ?? "public-anon-key", {
  auth: {
    persistSession: true,
    autoRefreshToken: true,
    detectSessionInUrl: true,
    storageKey: "indradhanu.auth",
  },
})

/** The current access token, refreshed if it is close to expiry.
 *
 *  Every call to the FastAPI service goes through this rather than reading a
 *  token from memory, because a long flood shift outlasts the one hour a
 *  Supabase access token is valid for. Letting the client refresh on demand is
 *  the difference between an officer staying signed in and being logged out
 *  mid-incident.
 */
export async function accessToken(): Promise<string | null> {
  if (!supabaseConfigured) return null
  const { data, error } = await supabase.auth.getSession()
  if (error) {
    console.warn("[indradhanu] could not read session", error.message)
    return null
  }
  return data.session?.access_token ?? null
}
