"""`python -m vinea.keys` -- issue, list and revoke API keys.

    python -m vinea.keys issue --tenant acme --label "nightly UI"
    python -m vinea.keys issue --ops --label "on-call laptop" --expires-days 90
    python -m vinea.keys list [--tenant acme] [--all]
    python -m vinea.keys revoke vinea_t_AbCdEfGhIjKl
    python -m vinea.keys import-env          # migrate off VINEA_API_KEYS

The bootstrap answer to "how does the first key exist". Not a migration: a migration
that mints a credential writes it into a file every deploy replays, and everyone who
can read the migration history can then authenticate. Not an endpoint either --
that endpoint would itself need a credential.

So it is a command run by a person with database access, which is the smallest set
that necessarily already has everything anyway.

Keys are printed **once**. Only the hash is stored and there is no recovery path;
that is the property that makes a leaked database not a leaked credential, and it
means losing the key costs an issue plus a revoke.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

from sqlmodel import Session

from vinea import keys
from vinea.db.session import make_engine, scope_to_ops


def _issue(session: Session, args) -> int:
    scope = keys.OPS_SCOPE if args.ops else keys.TENANT_SCOPE
    expires_at = (
        datetime.now(UTC) + timedelta(days=args.expires_days) if args.expires_days else None
    )
    issued = keys.issue(
        session,
        tenant=None if args.ops else args.tenant,
        label=args.label,
        scope=scope,
        expires_at=expires_at,
    )
    session.commit()

    where = "every tenant (/ops/*)" if args.ops else f"tenant '{args.tenant}'"
    print(f"Issued a key for {where}.")
    print(f"  label   : {issued.row.label}")
    print(f"  prefix  : {issued.prefix}")
    print(f"  expires : {expires_at.date().isoformat() if expires_at else 'when revoked'}")
    print()
    print(issued.secret)
    print()
    print("Shown once. Only a SHA-256 of it is stored, and there is no recovery path.")
    header = "X-Ops-Key" if args.ops else "X-API-Key"
    print(f"Use it as the {header} header.")
    return 0


def _list(session: Session, args) -> int:
    rows = keys.list_keys(session, tenant=args.tenant, include_revoked=args.all)
    if not rows:
        print("No keys." + ("" if args.all else " (--all also shows revoked ones.)"))
        return 0

    print(f"{'prefix':<22} {'scope':<7} {'tenant':<12} {'last used':<12} {'state':<10} label")
    print("-" * 88)
    now = datetime.now(UTC)
    for row in rows:
        if row.revoked_at is not None:
            state = "revoked"
        elif row.expires_at is not None and keys._aware(row.expires_at) <= now:
            state = "expired"
        else:
            state = "active"
        last_used = (
            keys._aware(row.last_used_at).date().isoformat() if row.last_used_at else "never"
        )
        print(
            f"{row.prefix:<22} {row.scope:<7} {(row.tenant or '-'):<12} "
            f"{last_used:<12} {state:<10} {row.label}"
        )
    return 0


def _revoke(session: Session, args) -> int:
    # Accept the whole key or just its prefix. Someone revoking in a hurry pastes
    # whatever they are holding, and refusing the full key would send them to a text
    # editor while a compromised credential is still live.
    prefix = keys.prefix_of(args.key)
    row = keys.revoke(session, prefix=prefix)
    if row is None:
        print(f"No key with prefix {prefix}.", file=sys.stderr)
        return 1
    session.commit()
    print(f"Revoked {row.prefix} ({row.label}) — it stops working on the next request.")
    return 0


def _import_env(session: Session, args) -> int:
    """Migrate `VINEA_API_KEYS` / `VINEA_OPS_KEY` into the table.

    The upgrade path for a deployment that already has keys in circulation. It
    stores the hash of each existing key, so **the keys people already hold keep
    working** and the environment variables can then be deleted -- rather than
    every client needing a new credential on the same afternoon.

    Idempotent: a key already in the table is skipped, so running this twice is
    safe. That matters because the first thing anyone does after an ambiguous
    output is run it again.
    """
    from sqlmodel import select

    from vinea.db.models import ApiKey

    raw = os.environ.get("VINEA_API_KEYS", "").strip()
    ops = os.environ.get("VINEA_OPS_KEY", "").strip()
    if not raw and not ops:
        print("Neither VINEA_API_KEYS nor VINEA_OPS_KEY is set; nothing to import.")
        return 0

    imported, skipped = 0, 0

    def already_there(secret: str) -> bool:
        return (
            session.exec(
                select(ApiKey).where(ApiKey.key_hash == keys.hash_key(secret))
            ).first()
            is not None
        )

    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        secret, tenant = (part.strip() for part in pair.split(":", 1))
        if already_there(secret):
            skipped += 1
            continue
        session.add(
            ApiKey(
                tenant=tenant,
                scope=keys.TENANT_SCOPE,
                label=f"imported from VINEA_API_KEYS on {datetime.now(UTC).date().isoformat()}",
                prefix=keys.prefix_of(secret),
                key_hash=keys.hash_key(secret),
            )
        )
        imported += 1

    if ops:
        if already_there(ops):
            skipped += 1
        else:
            session.add(
                ApiKey(
                    tenant=None,
                    scope=keys.OPS_SCOPE,
                    label=f"imported from VINEA_OPS_KEY on {datetime.now(UTC).date().isoformat()}",
                    prefix=keys.prefix_of(ops),
                    key_hash=keys.hash_key(ops),
                )
            )
            imported += 1

    session.commit()
    print(f"Imported {imported} key(s); {skipped} were already stored.")
    if imported:
        print(
            "The keys in circulation keep working. Delete VINEA_API_KEYS and "
            "VINEA_OPS_KEY from the Secret -- nothing reads them now, and a "
            "plaintext key in an environment variable is what this replaces."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vinea.keys", description="API keys.")
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue", help="Mint a key. Printed once.")
    issue.add_argument("--tenant", help="The tenant this key opens.")
    issue.add_argument(
        "--ops", action="store_true", help="An operator key for /ops/*, spanning every tenant."
    )
    issue.add_argument("--label", required=True, help="What this key is for. Required.")
    issue.add_argument(
        "--expires-days", type=int, default=None, help="Expire after N days. Default: never."
    )

    listing = sub.add_parser("list", help="Show keys. Never shows anything usable.")
    listing.add_argument("--tenant", default=None)
    listing.add_argument("--all", action="store_true", help="Include revoked and expired keys.")

    revoking = sub.add_parser("revoke", help="Kill a key by its prefix (or paste the whole key).")
    revoking.add_argument("key")

    sub.add_parser("import-env", help="Migrate VINEA_API_KEYS/VINEA_OPS_KEY into the table.")

    args = parser.parse_args(argv)
    if args.command == "issue" and not args.ops and not args.tenant:
        parser.error("issue needs --tenant, or --ops for a cross-tenant key")
    if args.command == "issue" and args.ops and args.tenant:
        parser.error("--ops keys span every tenant; do not give one a --tenant")

    handlers = {"issue": _issue, "list": _list, "revoke": _revoke, "import-env": _import_env}
    with Session(make_engine()) as session:
        # `api_keys` has no tenant to scope to -- the whole point is to look at it
        # across tenants -- so this runs under the ops escape, like every other
        # fleet-wide command.
        scope_to_ops(session)
        return handlers[args.command](session, args)


if __name__ == "__main__":
    sys.exit(main())
