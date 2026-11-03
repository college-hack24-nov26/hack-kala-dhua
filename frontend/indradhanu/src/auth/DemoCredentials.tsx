import { useState } from "react"
import { Check, Copy, KeyRound } from "lucide-react"
import {
  DEMO_ENABLED, DEMO_PASSWORD, identitiesFor, type DemoIdentity,
} from "@/auth/demoIdentities"

/** The credentials for one portal, on that portal.
 *
 *  Shown where the person actually is. Somebody handed a tablet on `/citizen`
 *  should not have to be told to navigate to `/login` to find out what to type,
 *  and a judge opening `/field` should see the two crew accounts rather than
 *  the commissioner's.
 *
 *  Renders nothing at all unless `DEMO_ENABLED` — so a production build has no
 *  credentials in it, and this component collapses to nothing.
 */
export function DemoCredentials({
  portal,
  className,
  title,
}: {
  portal: DemoIdentity["portal"]
  className?: string
  title?: string
}) {
  const [copied, setCopied] = useState<string | null>(null)
  if (!DEMO_ENABLED || !DEMO_PASSWORD) return null

  const list = identitiesFor(portal)
  if (!list.length) return null

  async function copy(value: string, key: string) {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(key)
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500)
    } catch {
      // A browser that will not write to the clipboard is not an error worth
      // showing: the text is on screen and can be typed.
    }
  }

  return (
    <div className={`rounded-lg border border-dashed p-3 ${className ?? ""}`}>
      <div className="mb-2 flex items-center gap-1.5">
        <KeyRound className="text-muted-foreground size-3.5" />
        <span className="text-xs font-medium">
          {title ?? "Demo sign-in for this portal"}
        </span>
      </div>

      <div className="space-y-2">
        {list.map((d) => (
          <div key={d.email} className={`rounded border-l-2 pl-2 ${d.tone}`}>
            <div className="text-xs font-medium">
              {d.who} <span className="text-muted-foreground">· {d.label}</span>
            </div>
            <button
              type="button"
              onClick={() => void copy(d.email, d.email)}
              className="hover:text-foreground text-muted-foreground flex w-full items-center gap-1.5 text-left font-mono text-[11px]"
              title="Copy the email"
            >
              <span className="truncate">{d.email}</span>
              {copied === d.email ? (
                <Check className="size-3 shrink-0 text-emerald-500" />
              ) : (
                <Copy className="size-3 shrink-0 opacity-50" />
              )}
            </button>
          </div>
        ))}
      </div>

      <button
        type="button"
        onClick={() => void copy(DEMO_PASSWORD, "pw")}
        className="hover:text-foreground text-muted-foreground mt-2 flex items-center gap-1.5 text-[11px]"
        title="Copy the password"
      >
        Password <code className="text-foreground">{DEMO_PASSWORD}</code>
        {copied === "pw" ? (
          <Check className="size-3 text-emerald-500" />
        ) : (
          <Copy className="size-3 opacity-50" />
        )}
      </button>

      <p className="text-muted-foreground mt-1.5 text-[10px]">
        Seeded demo accounts. This panel and the password are compiled out of a
        production build.
      </p>
    </div>
  )
}
