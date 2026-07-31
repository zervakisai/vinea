"""Per-tenant API keys, and the property that a key only opens its own tenant.

An `X-API-Key` header resolved against `api_keys` -- hashed, revocable, and with a
record of when each key was issued and last used. `X-Ops-Key` resolves against the
same table, to a row with no tenant and `scope='ops'`.

The security property that matters is not the key format, it is the **scoping**: a
key authenticates a tenant, and every tenant-scoped route asserts that the
`{tenant}` in the path matches the authenticated one. So acme's key cannot read
olivares's advisories even by guessing the URL -- and that check lives in one
dependency rather than sprinkled per route, where it would eventually be forgotten.

This remains the seam OIDC would slot into. The shape below -- a dependency that
returns the authenticated tenant -- is exactly what a JWT implementation replaces,
without touching a single route.

## What moved, and why

The previous version read `VINEA_API_KEYS="key-acme:acme,key-olivares:olivares"` --
plaintext, in an environment variable, revocable only by editing a Secret and
restarting every pod that read it. ADR-012 has the full argument; the short version
is that a credential you cannot revoke in under a deploy is a credential you cannot
revoke during the incident that requires it.

## Every attempt is recorded, and the recording cannot fail the request

`access_log` gets a row per authenticated call and per rejection, with *why* the
credential failed rather than just that it did. The write is wrapped and swallowed:
an audit trail that can 500 a grower's morning read has inverted its own priority.
That is a real limit and it is stated in ADR-012 rather than hidden -- a log that
may silently drop rows is not evidence of absence.
"""

from __future__ import annotations

import logging

from fastapi import Depends, Header, HTTPException, Path, Request, status
from sqlmodel import Session

from vinea import keys
from vinea.db.session import scope_to_ops

logger = logging.getLogger(__name__)

# Same 401 for missing, unknown, revoked and expired. Which one it was goes to the
# log, never to the caller: telling somebody that a key is "expired" rather than
# "unknown" confirms the key existed, which is the one bit a prober wants.
_UNAUTHORIZED = "Missing or invalid API key."


def _engine():
    # Lazily, to avoid an import cycle (main imports this module), and through
    # `current_engine` rather than `get_engine` so a test's dependency override
    # reaches the auth path too -- see that function for why the difference matters.
    from vinea.api.main import current_engine

    return current_engine()


def _record(
    request: Request,
    verification: keys.Verification,
    *,
    status_code: int,
    outcome: str | None = None,
) -> None:
    """Write one `access_log` row, at most once per request. Never raises.

    Its own session and its own transaction, deliberately. The request's session may
    be rolled back by the failure this row is describing, and an audit entry that
    disappears with the thing it audits is worse than none -- it looks like the
    attempt never happened.

    **At most once**, and that is load-bearing. `scoped_tenant` runs *after*
    `authenticated_tenant` has already stashed a successful verification, so a
    request rejected for reaching the wrong tenant was logging twice: once as
    `wrong_tenant` by the dependency that refused it, and once as `ok` by the
    middleware on the way out. Two rows for one request, the second contradicting
    the first -- and the `ok` row is the one an eye scanning for trouble skips. The
    flag on `request.state` is what makes the first writer the only writer.
    """
    if getattr(request.state, "access_logged", False):
        return
    request.state.access_logged = True

    route = request.url.path
    try:
        from vinea.db.models import AccessLog

        matched = request.scope.get("route")
        route = getattr(matched, "path", "") or request.url.path

        with Session(_engine()) as session:
            # `access_log` is under a row policy and a rejected request has no
            # tenant to scope to, so this writes under the ops escape -- the same
            # one the worker and /ops/* use.
            scope_to_ops(session)
            key = verification.key
            session.add(
                AccessLog(
                    tenant=key.tenant if key else None,
                    api_key_id=key.id if key else None,
                    key_prefix=key.prefix if key else None,
                    route=route,
                    method=request.method,
                    status_code=status_code,
                    outcome=outcome or verification.outcome,
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 -- the audit trail must not break the request
        logger.warning("could not write access_log for %s %s", request.method, route, exc_info=True)


def _verify(request: Request, presented: str | None, scope: str) -> keys.Verification:
    """Look the key up, and mark it used. Its own session, committed immediately.

    Separate from the request's session because `last_used_at` should be recorded
    whether or not the request that used the key goes on to succeed -- the question
    it answers is "is this credential still in circulation", and a 404 afterwards
    does not make the key less in circulation.
    """
    with Session(_engine()) as session:
        # Before the tenant is known there is nothing to scope to; see ADR-012 on
        # why that is bootstrap ordering rather than a hole.
        scope_to_ops(session)
        verification = keys.verify(session, presented, scope=scope)
        session.commit()
        if verification.key is not None:
            # Detach a plain snapshot: the session closes here and the caller must
            # not touch a lazy attribute afterwards.
            session.refresh(verification.key)
            session.expunge(verification.key)
        return verification


def authenticated_tenant(
    request: Request, x_api_key: str | None = Header(default=None)
) -> str:
    """FastAPI dependency: resolve `X-API-Key` to a tenant, or 401.

    The single choke point. Routes that are tenant-scoped then compare the result
    against the path (see `scoped_tenant`).
    """
    verification = _verify(request, x_api_key, keys.TENANT_SCOPE)
    if not verification.ok:
        _record(request, verification, status_code=status.HTTP_401_UNAUTHORIZED)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_UNAUTHORIZED,
            headers={"WWW-Authenticate": "ApiKey"},
        )
    # Stashed so a route that succeeds can be logged once, at the end, with its real
    # status -- see `record_authenticated_call`.
    request.state.verification = verification
    return verification.tenant  # type: ignore[return-value]


def scoped_tenant(
    request: Request,
    tenant: str = Path(...),
    authed_tenant: str = Depends(authenticated_tenant),
) -> str:
    """Authenticate, then authorize. The whole access-control story in one place.

    A route that `Depends(scoped_tenant)` cannot serve one tenant's data to
    another's key, and there is no per-route check to forget.
    """
    if tenant != authed_tenant:
        _record(
            request,
            getattr(request.state, "verification", keys.Verification(keys.WRONG_TENANT)),
            status_code=status.HTTP_403_FORBIDDEN,
            outcome=keys.WRONG_TENANT,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is not authorized for tenant '{tenant}'.",
        )
    return tenant


def check_scope(path_tenant: str, authed_tenant: str) -> None:
    """403 if a key is used against a tenant it does not own.

    The whole point of per-tenant keys: authentication (who are you) and
    authorization (may you touch THIS tenant) are different questions, and
    conflating them is how one tenant reads another's data.

    Kept for the routes that resolve the tenant themselves rather than through
    `scoped_tenant`. It raises without logging, because those callers already hold a
    request and log their own outcome.
    """
    if path_tenant != authed_tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is not authorized for tenant '{path_tenant}'.",
        )


def require_ops_key(request: Request, x_ops_key: str | None = Header(default=None)) -> None:
    """Gate the cross-tenant `/ops/*` surface behind an ops-scope key.

    Queue depth spans every tenant, so a per-tenant key is the wrong credential for
    it. Ops keys live in the same table with `scope='ops'` and no tenant: one
    revocation path for both kinds, because two would mean one of them eventually
    goes unused and stale.
    """
    verification = _verify(request, x_ops_key, keys.OPS_SCOPE)
    if not verification.ok:
        _record(request, verification, status_code=status.HTTP_401_UNAUTHORIZED)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid X-Ops-Key."
        )
    request.state.verification = verification


def record_authenticated_call(request: Request, status_code: int) -> None:
    """Log a call that got past authentication, with the status it actually returned.

    Called from the middleware rather than the dependency, because a dependency runs
    before the route and cannot know whether the answer was a 200 or a 404 -- and
    "this key asked for something that does not exist, forty times" is exactly the
    pattern an access log exists to make visible.
    """
    verification = getattr(request.state, "verification", None)
    if verification is None:
        return
    _record(request, verification, status_code=status_code)
