"""Issue, verify and revoke API keys.

The whole lifecycle in one module, because a credential system where minting and
checking live apart is one where they eventually disagree about the format.

## The key format

    vinea_t_<43 url-safe base64 characters>   a tenant key
    vinea_o_<43 url-safe base64 characters>   an ops key

The prefix is not decoration. It makes a leaked key **greppable**: a secret scanner
looking for `vinea_[to]_` finds one in a commit, a log, or a paste, which a bare
base64 blob would sail past. The scope letter means an operator reading
`vinea_o_...` in the wrong place knows immediately how bad it is.

32 bytes from `secrets.token_urlsafe`. Not `random`, not a UUID -- a UUID4 carries
122 bits with a known structure, and using one as a bearer token is the kind of
choice that is defensible right up until someone points out the version nibble.

## Shown once

`issue` returns the key. Nothing stores it and nothing can recover it. Losing it
means issuing another and revoking the first, which is a two-command inconvenience
and the reason a leaked database is not a leaked credential.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from vinea.db.models import ApiKey

TENANT_SCOPE = "tenant"
OPS_SCOPE = "ops"

_SCOPE_LETTER = {TENANT_SCOPE: "t", OPS_SCOPE: "o"}

# Enough to identify a row without being enough to use. 20 characters is the
# `vinea_x_` marker plus 12 of the secret -- 72 bits, which identifies a key among
# any number anyone will ever hold and leaves 184 bits unguessable.
PREFIX_LENGTH = 20

# How stale `last_used_at` is allowed to get. The column answers "was this used this
# month", so writing it on every request would put an UPDATE on the grower-facing
# read path to sharpen a timestamp nobody reads at that resolution.
LAST_USED_RESOLUTION = timedelta(hours=1)


def hash_key(key: str) -> str:
    """SHA-256, hex. See `ApiKey`'s docstring for why this is not a slow KDF."""
    return hashlib.sha256(key.encode()).hexdigest()


def prefix_of(secret: str) -> str:
    """The identifying fragment stored in the clear. Never more than half the key.

    `secret[:20]` would be right for every key this module mints -- 8 characters of
    marker plus 12 of 43 -- and wrong for the ones `import-env` takes over. A legacy
    key like `key-acme` is eight characters long, so a fixed 20-character slice
    would store *the entire credential in plaintext*, in the table whose purpose is
    to not hold credentials. The half rule makes the degenerate case degrade instead
    of inverting the design.

    Imported keys are weak anyway -- they were sitting in an environment variable --
    which is why `import-env` tells the operator to rotate rather than treating the
    import as the end of the job.
    """
    return secret[: min(PREFIX_LENGTH, max(4, len(secret) // 2))]


def generate_key(scope: str) -> str:
    letter = _SCOPE_LETTER.get(scope)
    if letter is None:
        raise ValueError(f"unknown scope {scope!r}; expected 'tenant' or 'ops'")
    return f"vinea_{letter}_{secrets.token_urlsafe(32)}"


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """A freshly minted key and its row. `secret` exists only in this object.

    `prefix` is copied rather than read through `row`, because the caller almost
    always issues inside a `with Session(...)` and uses the result outside it -- and
    a property that reached into a detached ORM instance would raise
    `DetachedInstanceError` at exactly that moment. The plain field survives the
    session; `row` is there for a caller that is still inside one.
    """

    secret: str
    prefix: str
    row: ApiKey


def issue(
    session: Session,
    *,
    tenant: str | None,
    label: str,
    scope: str = TENANT_SCOPE,
    expires_at: datetime | None = None,
) -> IssuedKey:
    """Mint a key, store its hash, return the key once.

    Does not commit -- transactions belong to the caller, the same rule every
    repository in this codebase follows. The CLI commits; a test can roll back.
    """
    if scope == TENANT_SCOPE and not tenant:
        raise ValueError("a tenant key needs a tenant")
    if scope == OPS_SCOPE and tenant:
        raise ValueError("an ops key spans every tenant and must not name one")
    if not label.strip():
        # Enforced here as well as by the NOT NULL, because the failure mode is not
        # a crash -- it is a table of unlabelled keys nobody dares revoke.
        raise ValueError("every key needs a label saying what it is for")

    secret = generate_key(scope)
    row = ApiKey(
        tenant=tenant or None,
        scope=scope,
        label=label.strip(),
        prefix=prefix_of(secret),
        key_hash=hash_key(secret),
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return IssuedKey(secret=secret, prefix=row.prefix, row=row)


@dataclass(frozen=True, slots=True)
class Verification:
    """The result of presenting a key. `outcome` is the reason, always.

    `ok` is a property rather than the whole answer because the caller needs the
    reason even on success -- it goes into `access_log.outcome`, and a log that
    records only failures cannot answer "when was this key last used legitimately".
    """

    outcome: str
    key: ApiKey | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def tenant(self) -> str | None:
        return self.key.tenant if self.key else None


# The outcomes, named once so the API and the log cannot spell them differently.
OK = "ok"
NO_KEY = "no_key"
UNKNOWN_KEY = "unknown_key"
REVOKED = "revoked"
EXPIRED = "expired"
WRONG_SCOPE = "wrong_scope"
WRONG_TENANT = "wrong_tenant"


def verify(session: Session, presented: str | None, *, scope: str = TENANT_SCOPE) -> Verification:
    """Resolve a presented key to its row, or say precisely why not.

    Distinguishes *unknown*, *revoked* and *expired* rather than collapsing them
    into "invalid". The API returns the same 401 for all three -- telling a caller
    which one would help someone probing -- but the log records the difference,
    because they call for three different responses: a misconfigured client, a
    credential that should already be dead being used, and a rotation nobody
    finished.

    Requires an ops-scoped session: the lookup happens *before* the tenant is known,
    so it cannot run under a tenant policy. That is not a hole -- `api_keys` is an
    operator table and no tenant-facing route reads it.
    """
    if not presented:
        return Verification(NO_KEY)

    row = session.exec(select(ApiKey).where(ApiKey.key_hash == hash_key(presented))).first()
    if row is None:
        return Verification(UNKNOWN_KEY)
    if row.revoked_at is not None:
        return Verification(REVOKED, row)

    now = datetime.now(UTC)
    if row.expires_at is not None and _aware(row.expires_at) <= now:
        return Verification(EXPIRED, row)
    if row.scope != scope:
        # A tenant key on /ops/* and an ops key on a tenant route are both this.
        # Recorded distinctly because a tenant key appearing on the operator surface
        # is a different event from a mistyped one.
        return Verification(WRONG_SCOPE, row)

    _touch(session, row, now)
    return Verification(OK, row)


def _aware(value: datetime) -> datetime:
    """Postgres hands back tz-aware values; SQLite and hand-built rows may not."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _touch(session: Session, row: ApiKey, now: datetime) -> None:
    """Advance `last_used_at`, at most once per `LAST_USED_RESOLUTION`.

    Deliberately not flushed. The caller's transaction carries it, so a request that
    fails afterwards does not leave a "used" mark for a call that never completed --
    and more practically, this avoids a round trip on the read path.
    """
    if row.last_used_at is None or _aware(row.last_used_at) < now - LAST_USED_RESOLUTION:
        row.last_used_at = now


def revoke(session: Session, *, prefix: str) -> ApiKey | None:
    """Kill a key by its prefix. Returns the row, or None if there was no such key.

    By prefix rather than by id, because the person revoking is usually holding the
    key or reading it from a log, and looking up an id first is a step during which
    a compromised credential is still live. Already-revoked is a no-op that returns
    the row -- revoking twice is what a worried operator does, and it should not
    look like a failure.
    """
    row = session.exec(select(ApiKey).where(ApiKey.prefix == prefix)).first()
    if row is None:
        return None
    if row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
    session.flush()
    return row


def list_keys(session: Session, *, tenant: str | None = None, include_revoked: bool = False):
    """Every key, or one tenant's. Never returns anything usable as a credential."""
    statement = select(ApiKey)
    if tenant is not None:
        statement = statement.where(ApiKey.tenant == tenant)
    if not include_revoked:
        statement = statement.where(ApiKey.revoked_at.is_(None))  # type: ignore[union-attr]
    return list(session.exec(statement.order_by(ApiKey.created_at)).all())
