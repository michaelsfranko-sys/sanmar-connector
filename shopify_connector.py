import os
import threading
import time

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/shopify", tags=["Shopify"])

_token_lock = threading.Lock()
_token_cache = {
    "access_token": None,
    "expires_at": 0.0,
    "scope": None,
}


def _shopify_settings():
    store = (os.getenv("SHOPIFY_STORE_DOMAIN") or "").strip().strip("/")
    if store.startswith("https://"):
        store = store[len("https://"):]
    elif store.startswith("http://"):
        store = store[len("http://"):]

    return {
        "store": store,
        "client_id": os.getenv("SHOPIFY_CLIENT_ID"),
        "client_secret": os.getenv("SHOPIFY_CLIENT_SECRET"),
        "api_version": os.getenv("SHOPIFY_API_VERSION", "2026-07"),
    }


def _require_shopify_settings():
    settings = _shopify_settings()
    missing = [
        key
        for key in ("store", "client_id", "client_secret")
        if not settings.get(key)
    ]
    if missing:
        raise HTTPException(
            status_code=500,
            detail=(
                "Missing Shopify environment configuration: "
                + ", ".join(missing)
                + ". Expected SHOPIFY_STORE_DOMAIN, SHOPIFY_CLIENT_ID, and SHOPIFY_CLIENT_SECRET."
            ),
        )
    return settings


def _get_access_token(force_refresh: bool = False):
    settings = _require_shopify_settings()
    now = time.time()

    with _token_lock:
        if (
            not force_refresh
            and _token_cache["access_token"]
            and now < (_token_cache["expires_at"] - 60)
        ):
            return _token_cache["access_token"], settings

        url = f"https://{settings['store']}/admin/oauth/access_token"
        try:
            response = httpx.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings["client_id"],
                    "client_secret": settings["client_secret"],
                },
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Could not reach Shopify token endpoint: {type(exc).__name__}: {exc}",
            ) from exc

        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Shopify token request failed with HTTP {response.status_code}: {response.text[:500]}",
            )

        payload = response.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise HTTPException(
                status_code=502,
                detail="Shopify token response did not include an access_token.",
            )

        expires_in = int(payload.get("expires_in") or 3600)
        _token_cache.update(
            {
                "access_token": access_token,
                "expires_at": now + expires_in,
                "scope": payload.get("scope"),
            }
        )
        return access_token, settings


def _graphql(query: str, variables: dict | None = None):
    access_token, settings = _get_access_token()
    url = f"https://{settings['store']}/admin/api/{settings['api_version']}/graphql.json"

    try:
        response = httpx.post(
            url,
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables or {}},
            timeout=45.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach Shopify Admin API: {type(exc).__name__}: {exc}",
        ) from exc

    if response.status_code == 401:
        access_token, settings = _get_access_token(force_refresh=True)
        response = httpx.post(
            url,
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables or {}},
            timeout=45.0,
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Shopify Admin API returned HTTP {response.status_code}: {response.text[:700]}",
        )

    payload = response.json()
    if payload.get("errors"):
        raise HTTPException(
            status_code=502,
            detail={"message": "Shopify GraphQL error", "errors": payload["errors"]},
        )
    return payload.get("data") or {}


@router.get("/health")
def shopify_health():
    data = _graphql(
        """
        query ConnectorHealth {
          shop {
            name
            myshopifyDomain
          }
          currentAppInstallation {
            accessScopes {
              handle
            }
          }
        }
        """
    )

    shop = data.get("shop") or {}
    installation = data.get("currentAppInstallation") or {}
    scopes = sorted(
        scope.get("handle")
        for scope in (installation.get("accessScopes") or [])
        if scope.get("handle")
    )

    return {
        "status": "ok",
        "shop_name": shop.get("name"),
        "myshopify_domain": shop.get("myshopifyDomain"),
        "api_version": _shopify_settings()["api_version"],
        "access_scopes": scopes,
        "required_scopes_present": {
            "write_products": "write_products" in scopes,
            "write_inventory": "write_inventory" in scopes,
            "read_locations": "read_locations" in scopes,
        },
    }
