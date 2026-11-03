# Authentication

Supabase issues the identity. This service verifies it and decides what that
identity may do. Three layers check the same thing independently, which is the
point: a bug in any one of them does not open the others.

```
  Supabase Auth            FastAPI                     Postgres
  ─────────────            ───────                     ────────
  issues the JWT     →     verifies it, resolves   →   row level security
  (email + password)       the role from profiles      checks the role again
                           and authorises the call     via app.is_staff()
```

## Verifying the token

Supabase signs access tokens one of two ways, and a project can be switched
between them from the dashboard at any time:

- **Legacy, symmetric.** HS256 against the project's shared JWT secret
  (`SUPABASE_JWT_KEY`).
- **Current, asymmetric.** ES256 or RS256 against a private key Supabase holds,
  with the public half at `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`.

`app/core/jwks.py` supports both and picks per token, reading the algorithm from
the token header but **verifying with an algorithm from our own allow-list**.
That distinction matters: accepting the token's own `alg` is how
algorithm-confusion forgery works, where an attacker signs HS256 using the
public key as the shared secret.

Key sets are cached for an hour. A `kid` we have not seen drops the cache and
refetches once, so a key rotation heals itself rather than locking everyone out
until a restart.

## Roles

`profiles.role` is the authority, not the token. Supabase knows a user id and an
email; it does not know that this person is the ward officer for w-12.

| Role | Interfaces | Notable |
|---|---|---|
| `citizen` | citizen | Default for every signup. Can read risk and file reports without an account at all |
| `field_operator` | field, citizen | Sees only their own agency's tasks, enforced in the query and again in RLS |
| `ward_officer` | admin, field, citizen | Staff. May act on decisions their clause delegates, within their ward |
| `commissioner` | admin, field, citizen | Staff, and may act outside delegation |
| `admin` | all | Staff, escalation |

**Roles are promoted, never self-declared.** `app.handle_new_user` gives every
new signup `citizen` and deliberately ignores any role in the signup metadata. A
role that can be requested at signup is a privilege escalation waiting to
happen. To promote someone:

```sql
update profiles set role = 'ward_officer', ward_id = 'w-12'
where id = (select id from auth.users where email = 'them@example.com');
```

## Demo accounts

Seeded by migration `pune_demo_accounts`, as real Supabase users with
`auth.identities` rows so email sign-in actually works.

| Email | Role | Who they are |
|---|---|---|
| `commissioner@pune.indradhanu.local` | commissioner | Meera Deshpande. Can authorise evacuations |
| `officer@pune.indradhanu.local` | ward_officer | Rahul Kulkarni. Delegated within one ward |
| `fire@pune.indradhanu.local` | field_operator | Sana Shaikh. Sees only Fire Brigade tasks |
| `pmc@pune.indradhanu.local` | field_operator | Vikram Jadhav. Sees only PMC Drainage tasks |
| `citizen@pune.indradhanu.local` | citizen | Anita Joshi. Risk map and reporting |

Password for all five: `Indradhanu#2026`

These exist so the same moment in an event can be shown from every seat in the
room. They are throwaway credentials for a hackathon project against a
throwaway Supabase project. Delete them before they mean anything:

```sql
delete from auth.users where email like '%@pune.indradhanu.local';
```

The login screen lists them on purpose, for the same reason. Remove the `DEMO`
block in `src/routes/auth/Login.tsx` when that stops being appropriate.

## DEV_AUTH_ROLE

For local work before any user exists:

```
DEV_AUTH_ROLE=ward_officer
```

An unauthenticated request is then treated as that role. Gated twice: the
setting must be non-empty **and** `INDRADHANU_ENV` must be `development`.
`Settings` refuses to load at all otherwise, so it cannot be left switched on by
accident in a deployed environment. Every request that uses it logs a warning,
and `/auth/me` reports `developmentIdentity: true` so the interface can show a
badge rather than pretending it is a real session.

Now that the demo accounts exist, prefer signing in. Leave `DEV_AUTH_ROLE`
empty.

## Frontend

- `src/lib/supabase.ts` holds the browser client and `accessToken()`. Tokens are
  read fresh from the session on every request rather than captured once: an
  access token lasts about an hour and a flood shift does not.
- `src/auth/AuthProvider.tsx` owns the session, and calls `/auth/me` to learn
  the role. It asks the server rather than reading claims, because the role is
  not in the token.
- `src/auth/RequireRole.tsx` decides what to render. It is a courtesy, not a
  control: the API refuses independently and so does RLS.
- Only the **anon** key belongs in frontend env. The service role key bypasses
  row level security completely and must never ship to a browser.

## What the layers actually catch

An officer opening a decision reserved to the Commissioner is refused by
`operations.act_on_decision`, which names the clause that blocked them. If that
check were removed, the `decisions_officer_act` RLS policy would still refuse
the UPDATE. If both were removed, the frontend would still not show the button.
Three chances to get it right, and the one that matters most is the one closest
to the data.
