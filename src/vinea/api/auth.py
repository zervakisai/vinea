"""Per-tenant API keys, and the property that a key only opens its own tenant.

Deliberately simple: an `X-API-Key` header, resolved to a tenant through a mapping
loaded from the environment. This is the "simple header check for now" the plan
calls for, with OIDC/JWT as a clearly marked seam -- the shape below (a dependency
that returns the authenticated tenant) is exactly what an OIDC implementation would
slot into without touching a single route.

The security property that matters is not the key format, it's the *scoping*: a key
authenticates a tenant, and every tenant-scoped route asserts that the `{tenant}`
in the path matches the authenticated tenant. So acme's key cannot read olivares's
advisories even by guessing the URL -- that check lives in one dependency, not
sprinkled per route where it would eventually be forgotten.
"""

from __future__ import annotations

import os

from fastapi import Depends, Header, HTTPException, Path, status

API_KEY_ENV = "VINEA_API_KEYS"
OPS_KEY_ENV = "VINEA_OPS_KEY"


def _key_to_tenant() -> dict[str, str]:
    """Parse VINEA_API_KEYS ("key1:tenantA,key2:tenantB") into a mapping.

    Read from the environment on every call (no caching), the same discipline as
    `config.has_api_key`: a test can set the env and get an honest answer, and a
    rotated key takes effect without a restart. An empty/unset value means no keys
    are configured, and every authenticated route then returns 401 -- fail closed,
    never fail open.
    """
    raw = os.environ.get(API_KEY_ENV, "").strip()
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        key, tenant = pair.split(":", 1)
        mapping[key.strip()] = tenant.strip()
    return mapping


def authenticated_tenant(x_api_key: str | None = Header(default=None)) -> str:
    """FastAPI dependency: resolve the X-API-Key header to a tenant, or 401.

    Returns the tenant the key belongs to. Routes that are tenant-scoped then
    compare it against the path (see `scoped_tenant`). This is the single choke
    point OIDC would replace.
    """
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    tenant = _key_to_tenant().get(x_api_key)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")
    return tenant


def scoped_tenant(
    tenant: str = Path(...),
    authed_tenant: str = Depends(authenticated_tenant),
) -> str:
    """FastAPI dependency for tenant-scoped routes: authenticate, then authorize.

    Resolves the API key to a tenant (authentication) and asserts it matches the
    `{tenant}` in the path (authorization), returning the tenant on success. This
    composition is the whole access-control story in one dependency: a route that
    `Depends(scoped_tenant)` cannot serve one tenant's data to another's key, and
    there's no per-route check to forget.
    """
    check_scope(tenant, authed_tenant)
    return tenant


def check_scope(path_tenant: str, authed_tenant: str) -> None:
    """403 if a key is used against a tenant it doesn't own.

    The whole point of per-tenant keys: authentication (who are you) and
    authorization (may you touch THIS tenant) are different questions, and
    conflating them is how one tenant reads another's data.
    """
    if path_tenant != authed_tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"API key is not authorized for tenant '{path_tenant}'.",
        )


def require_ops_key(x_ops_key: str | None = Header(default=None)) -> None:
    """Gate operator-wide endpoints (/ops/*) behind a separate ops key.

    Queue depth spans all tenants, so a per-tenant key is the wrong credential for
    it. A dedicated ops key keeps operator surface off tenant keys. Roles/OIDC
    would generalise this; the seam is here.
    """
    expected = os.environ.get(OPS_KEY_ENV, "").strip()
    if not expected or x_ops_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid X-Ops-Key."
        )
