"""Find out why the database will not connect.

`TimeoutError` out of asyncpg tells you nothing useful: DNS failure, a blocked
port, the wrong pooler hostname and an IPv6-only host all look identical. This
tries each candidate in turn and says which one works, so the fix is a fact
rather than a guess.

    python scripts/check_db.py

Run it from `backend/` with the virtualenv active. It prints no passwords.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import urllib.parse as up

sys.path.insert(0, ".")

from app.core.config import settings  # noqa: E402

CONNECT_TIMEOUT = 8.0


def redact(dsn: str) -> str:
    u = up.urlsplit(dsn)
    return f"{u.scheme}://{u.username}:***@{u.hostname}:{u.port}{u.path}"


def swap_host(dsn: str, host: str, port: int | None = None) -> str:
    u = up.urlsplit(dsn)
    netloc = f"{u.username}:{up.quote(u.password or '', safe='')}@{host}:{port or u.port}"
    return up.urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def resolve(host: str) -> tuple[list[str], list[str]]:
    """(ipv4, ipv6) addresses for a host."""
    v4: list[str] = []
    v6: list[str] = []
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(host, None):
            if fam == socket.AF_INET and sa[0] not in v4:
                v4.append(sa[0])
            elif fam == socket.AF_INET6 and sa[0] not in v6:
                v6.append(sa[0])
    except socket.gaierror:
        pass
    return v4, v6


def tcp_open(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(host, port, fam, socket.SOCK_STREAM)
        except socket.gaierror as exc:
            continue
        for *_x, sa in infos:
            try:
                s = socket.socket(fam, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect(sa)
                s.close()
                return True, f"{'IPv4' if fam == socket.AF_INET else 'IPv6'} {sa[0]}"
            except OSError as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
    return False, locals().get("last", "no address resolved")


async def try_connect(dsn: str) -> tuple[bool, str]:
    try:
        import asyncpg
    except ImportError:
        return False, "asyncpg is not installed in this environment"
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn, statement_cache_size=0), timeout=CONNECT_TIMEOUT
        )
        version = await conn.fetchval("select version()")
        wards = await conn.fetchval("select count(*) from wards")
        await conn.close()
        return True, f"{version.split(',')[0]} | wards={wards}"
    except asyncio.TimeoutError:
        return False, f"timed out after {CONNECT_TIMEOUT:g}s"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


async def main() -> int:
    pooled = settings.supabase_transaction_pooler
    direct = settings.supabase_direct_connection_string

    candidates: list[tuple[str, str]] = []
    if pooled:
        u = up.urlsplit(pooled)
        host = u.hostname or ""
        candidates.append(("pooler, as configured", pooled))
        # Projects created from roughly 2025 onward sit behind aws-1-*; a .env
        # copied from an older project keeps aws-0-* and times out silently.
        for a, b in (("aws-0-", "aws-1-"), ("aws-1-", "aws-0-")):
            if host.startswith(a):
                candidates.append(
                    (f"pooler, {b.rstrip('-')} variant", swap_host(pooled, host.replace(a, b, 1)))
                )
        # Session mode on 5432 survives some networks that block 6543.
        candidates.append(("pooler, session mode :5432", swap_host(pooled, host, 5432)))
    if direct:
        candidates.append(("direct connection", direct))

    if not candidates:
        print("No DSN configured. Set SUPABASE_TRANSACTION_POOLER in .env.")
        return 2

    print("=" * 74)
    print("Indradhanu database connectivity check")
    print("=" * 74)

    working: list[tuple[str, str]] = []
    for label, dsn in candidates:
        u = up.urlsplit(dsn)
        host, port = u.hostname or "", u.port or 5432
        print(f"\n{label}")
        print(f"  {redact(dsn)}")

        v4, v6 = resolve(host)
        print(f"  DNS        A={v4 or 'none'}  AAAA={v6 or 'none'}")
        if not v4 and not v6:
            print("  VERDICT    hostname does not resolve. Wrong host.")
            continue
        if not v4 and v6:
            print(
                "  NOTE       IPv6 only. Most home and campus networks in India"
                "\n             cannot reach this. Use the pooler, not the direct host."
            )

        ok, how = tcp_open(host, port)
        print(f"  TCP :{port}  {'open via ' + how if ok else 'BLOCKED - ' + how}")
        if not ok:
            continue

        ok, detail = await try_connect(dsn)
        print(f"  POSTGRES   {'OK - ' + detail if ok else 'FAILED - ' + detail}")
        if ok:
            working.append((label, dsn))

    print("\n" + "=" * 74)
    if working:
        label, dsn = working[0]
        print(f"WORKS: {label}")
        print(f"\nPut this in backend/.env as SUPABASE_TRANSACTION_POOLER")
        print("(the real password is in your existing value, keep it):\n")
        print(f"  {redact(dsn)}")
        print(
            "\nIf more than one worked, prefer a pooler entry over the direct"
            "\nconnection: pgbouncer is what the API is configured for."
        )
        return 0

    print("NOTHING CONNECTED. In order of likelihood:")
    print("  1. The pooler hostname is wrong. Copy the exact string from the")
    print("     Supabase dashboard: Project -> Connect -> Transaction pooler.")
    print("  2. Your network blocks outbound 6543. Try session mode on 5432,")
    print("     or a phone hotspot to confirm it is the network.")
    print("  3. The password in the DSN needs URL-encoding if it contains")
    print("     any of : / ? # [ ] @ ! $ & ' ( ) * + , ; =")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
