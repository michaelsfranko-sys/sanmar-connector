import os
import threading
import time

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/shopify", tags=["Shopify"])

_STORE_ENV_PREFIXES = {
    "quality-image": "QUALITY_IMAGE",
    "quality_image": "QUALITY_IMAGE",
    "quality": "QUALITY_IMAGE",
    "prestige": "PRESTIGE",
    "prestige-custom-creations": "PRESTIGE",
}

_token_locks: dict[str, threading.Lock] = {}
_token_caches: dict[str, dict] = {}
_registry_lock = threading.Lock()


def _normalize_store_key(store_key: str | None) -> str:
    raw = (store_key or os.getenv("SHOPIFY_DEFAULT_STORE") or "legacy").strip().lower()
    return raw or "legacy"


def _store_env_prefix(store_key: str) -> str | None:
    return _STORE_ENV_PREFIXES.get(store_key)


def _clean_domain(value: str | None) -> str:
    store = (value or "").strip().strip("/")
    if store.startswith("https://"):
        store = store[len("https://"):]
    elif store.startswith("http://"):
        store = store[len("http://"):]
    return store


def _shopify_settings(store_key: str | None = None):
    key = _normalize_store_key(store_key)
    prefix = _store_env_prefix(key)

    if prefix:
        store = _clean_domain(os.getenv(f"{prefix}_SHOPIFY_STORE_DOMAIN"))
        client_id = os.getenv(f"{prefix}_SHOPIFY_CLIENT_ID")
        client_secret = os.getenv(f"{prefix}_SHOPIFY_CLIENT_SECRET")
        api_version = os.getenv(
            f"{prefix}_SHOPIFY_API_VERSION",
            os.getenv("SHOPIFY_API_VERSION", "2026-07"),
        )
    else:
        store = _clean_domain(os.getenv("SHOPIFY_STORE_DOMAIN"))
        client_id = os.getenv("SHOPIFY_CLIENT_ID")
        client_secret = os.getenv("SHOPIFY_CLIENT_SECRET")
        api_version = os.getenv("SHOPIFY_API_VERSION", "2026-07")

    return {
        "store_key": key,
        "store": store,
        "client_id": client_id,
        "client_secret": client_secret,
        "api_version": api_version,
    }


def _require_shopify_settings(store_key: str | None = None):
    settings = _shopify_settings(store_key)
    missing = [
        key
        for key in ("store", "client_id", "client_secret")
        if not settings.get(key)
    ]
    if missing:
        prefix = _store_env_prefix(settings["store_key"])
        expected = (
            f"{prefix}_SHOPIFY_STORE_DOMAIN, {prefix}_SHOPIFY_CLIENT_ID, and {prefix}_SHOPIFY_CLIENT_SECRET"
            if prefix
            else "SHOPIFY_STORE_DOMAIN, SHOPIFY_CLIENT_ID, and SHOPIFY_CLIENT_SECRET"
        )
        raise HTTPException(
            status_code=500,
            detail=(
                f"Missing Shopify environment configuration for store '{settings['store_key']}': "
                + ", ".join(missing)
                + f". Expected {expected}."
            ),
        )
    return settings


def _token_state(store_key: str):
    with _registry_lock:
        if store_key not in _token_locks:
            _token_locks[store_key] = threading.Lock()
        if store_key not in _token_caches:
            _token_caches[store_key] = {
                "access_token": None,
                "expires_at": 0.0,
                "scope": None,
            }
        return _token_locks[store_key], _token_caches[store_key]


def _get_access_token(store_key: str | None = None, force_refresh: bool = False):
    settings = _require_shopify_settings(store_key)
    key = settings["store_key"]
    lock, cache = _token_state(key)
    now = time.time()

    with lock:
        if (
            not force_refresh
            and cache["access_token"]
            and now < (cache["expires_at"] - 60)
        ):
            return cache["access_token"], settings

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
                detail=f"Could not reach Shopify token endpoint for '{key}': {type(exc).__name__}: {exc}",
            ) from exc

        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Shopify token request failed for '{key}' with HTTP {response.status_code}: {response.text[:500]}",
            )

        payload = response.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise HTTPException(
                status_code=502,
                detail=f"Shopify token response for '{key}' did not include an access_token.",
            )

        expires_in = int(payload.get("expires_in") or 3600)
        cache.update(
            {
                "access_token": access_token,
                "expires_at": now + expires_in,
                "scope": payload.get("scope"),
            }
        )
        return access_token, settings


def _graphql(query: str, variables: dict | None = None, store_key: str | None = None):
    access_token, settings = _get_access_token(store_key)
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
            detail=f"Could not reach Shopify Admin API for '{settings['store_key']}': {type(exc).__name__}: {exc}",
        ) from exc

    if response.status_code == 401:
        access_token, settings = _get_access_token(store_key, force_refresh=True)
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
            detail=f"Shopify Admin API for '{settings['store_key']}' returned HTTP {response.status_code}: {response.text[:700]}",
        )

    payload = response.json()
    if payload.get("errors"):
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Shopify GraphQL error for store '{settings['store_key']}'",
                "errors": payload["errors"],
            },
        )
    return payload.get("data") or {}


def _health_for_store(store_key: str | None = None):
    settings = _require_shopify_settings(store_key)
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
        """,
        store_key=settings["store_key"],
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
        "store_key": settings["store_key"],
        "shop_name": shop.get("name"),
        "myshopify_domain": shop.get("myshopifyDomain"),
        "api_version": settings["api_version"],
        "access_scopes": scopes,
        "required_scopes_present": {
            "write_products": "write_products" in scopes,
            "write_inventory": "write_inventory" in scopes,
            "read_locations": "read_locations" in scopes,
            "read_orders": "read_orders" in scopes,
            "read_products": "read_products" in scopes,
        },
    }


@router.get("/health")
def shopify_health():
    return _health_for_store()


@router.get("/health/{store_key}")
def shopify_store_health(store_key: str):
    return _health_for_store(store_key)


@router.get("/stores/health")
def shopify_all_stores_health():
    results = {}
    for store_key in ("quality-image", "prestige"):
        try:
            results[store_key] = _health_for_store(store_key)
        except HTTPException as exc:
            results[store_key] = {
                "status": "error",
                "detail": exc.detail,
            }
    return results
