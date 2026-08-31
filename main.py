import csv
import os
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Optional

import paramiko
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

app = FastAPI(
    title="SanMar Connector",
    version="0.2.0",
    description="Private API for searching SanMar EPDD product data and sanmar_dip inventory data.",
)

CACHE_DIR = Path(os.getenv("SANMAR_CACHE_DIR", "/tmp/sanmar"))
EPDD_LOCAL = CACHE_DIR / "SanMar_EPDD.csv"
DIP_LOCAL = CACHE_DIR / "sanmar_dip.txt"


class RefreshResult(BaseModel):
    epdd_downloaded: bool
    dip_downloaded: bool


def _settings():
    return {
        "host": os.getenv("SANMAR_SFTP_HOST", "ftp.sanmar.com").strip(),
        "port": int(os.getenv("SANMAR_SFTP_PORT", "2200")),
        "username": os.getenv("SANMAR_SFTP_USERNAME"),
        "password": os.getenv("SANMAR_SFTP_PASSWORD"),
        "epdd_path": os.getenv("SANMAR_EPDD_REMOTE_PATH", "SanMar_EPDD.csv").strip().strip('"').strip("'"),
        "dip_path": os.getenv("SANMAR_DIP_REMOTE_PATH", "sanmar_dip.txt").strip().strip('"').strip("'"),
    }


def _require_credentials():
    s = _settings()
    if not s["username"] or not s["password"]:
        raise HTTPException(status_code=500, detail="SanMar SFTP credentials are not configured.")
    return s


def _open_sftp():
    s = _require_credentials()
    transport = paramiko.Transport((s["host"], s["port"]))
    transport.connect(username=s["username"], password=s["password"])
    sftp = paramiko.SFTPClient.from_transport(transport)
    return s, transport, sftp


def _safe_listdir(sftp, path: str):
    try:
        return sftp.listdir(path)
    except Exception:
        return []


def _resolve_remote_path(sftp, configured_path: str) -> str:
    configured_path = (configured_path or "").strip().strip('"').strip("'")
    candidates = [configured_path]
    if configured_path.startswith("/"):
        candidates.append(configured_path.lstrip("/"))
    else:
        candidates.append("/" + configured_path)

    for candidate in candidates:
        try:
            sftp.stat(candidate)
            return candidate
        except OSError:
            pass

    target_name = Path(configured_path).name.lower()
    for directory in [".", "/"]:
        for name in _safe_listdir(sftp, directory):
            if name.lower() == target_name:
                return name if directory == "." else f"/{name}"

    visible = sorted(set(_safe_listdir(sftp, ".") + _safe_listdir(sftp, "/")))
    raise FileNotFoundError(
        f"Could not locate '{configured_path}' on SanMar SFTP. Visible top-level entries: {visible[:100]}"
    )


def _download_sftp_file(sftp, remote_path: str, local_path: Path):
    resolved_path = _resolve_remote_path(sftp, remote_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = local_path.with_suffix(local_path.suffix + ".part")
    sftp.get(resolved_path, str(temp_path))
    shutil.move(str(temp_path), str(local_path))
    return resolved_path


def refresh_files() -> RefreshResult:
    s, transport, sftp = _open_sftp()
    try:
        _download_sftp_file(sftp, s["epdd_path"], EPDD_LOCAL)
        _download_sftp_file(sftp, s["dip_path"], DIP_LOCAL)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SanMar SFTP refresh failed: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            sftp.close()
        finally:
            transport.close()
    return RefreshResult(epdd_downloaded=EPDD_LOCAL.exists(), dip_downloaded=DIP_LOCAL.exists())


def ensure_cache():
    if not EPDD_LOCAL.exists() or not DIP_LOCAL.exists():
        refresh_files()


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _clean_description(value: Optional[str]) -> str:
    if not value:
        return ""
    return " ".join(value.replace("|", " ").split())


def search_epdd(style: str, color: Optional[str] = None, size: Optional[str] = None):
    ensure_cache()
    rows = []
    with EPDD_LOCAL.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if _norm(row.get("STYLE#")) != _norm(style):
                continue
            if color and _norm(row.get("COLOR_NAME")) != _norm(color):
                continue
            if size and _norm(row.get("SIZE")) != _norm(size):
                continue
            rows.append({
                "unique_key": row.get("UNIQUE_KEY"),
                "style": row.get("STYLE#"),
                "title": row.get("PRODUCT_TITLE"),
                "description": _clean_description(row.get("PRODUCT_DESCRIPTION")),
                "brand": row.get("MILL"),
                "category": row.get("CATEGORY_NAME"),
                "subcategory": row.get("SUBCATEGORY_NAME"),
                "color": row.get("COLOR_NAME"),
                "size": row.get("SIZE"),
                "qty_epdd": row.get("QTY"),
                "piece_price": row.get("PIECE_PRICE"),
                "case_price": row.get("CASE_PRICE"),
                "suggested_price": row.get("SUGGESTED_PRICE"),
                "msrp": row.get("MSRP"),
                "map_pricing": row.get("MAP_PRICING"),
                "product_status": row.get("PRODUCT_STATUS"),
                "inventory_key": row.get("INVENTORY_KEY"),
                "size_index": row.get("SIZE_INDEX"),
                "gtin": row.get("GTIN"),
                "front_model_image_url": row.get("FRONT_MODEL_IMAGE_URL"),
                "color_product_image": row.get("COLOR_PRODUCT_IMAGE"),
                "front_flat_image": row.get("FRONT_FLAT_IMAGE"),
                "back_flat_image": row.get("BACK_FLAT_IMAGE"),
                "color_swatch_image": row.get("COLOR_SWATCH_IMAGE"),
                "spec_sheet": row.get("SPEC_SHEET"),
                "decoration_spec_sheet": row.get("DECORATION_SPEC_SHEET"),
                "product_measurements": row.get("PRODUCT_MEASUREMENTS"),
                "pms_color": row.get("PMS_COLOR"),
            })
    return rows


def search_dip(style: str, color: Optional[str] = None, size: Optional[str] = None):
    ensure_cache()
    rows = []
    with DIP_LOCAL.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            if _norm(row.get("catalog_no")) != _norm(style):
                continue
            if color and _norm(row.get("catalog_color")) != _norm(color):
                continue
            if size and _norm(row.get("size")) != _norm(size):
                continue
            rows.append({
                "inventory_key": row.get("inventory_key"),
                "size_index": row.get("size_index"),
                "style": row.get("catalog_no"),
                "color": row.get("catalog_color"),
                "size": row.get("size"),
                "warehouse": row.get("whse_no"),
                "quantity": int(row.get("quantity") or 0),
                "piece_weight": row.get("piece_weight"),
                "piece_price": row.get("piece_price"),
                "dozens_price": row.get("dozens_price"),
                "case_price": row.get("case_price"),
                "case_size": row.get("case_size"),
                "each_sale_price": row.get("each_sale_price"),
                "sale_start_datetime": row.get("sale_start_datetime"),
                "sale_end_datetime": row.get("sale_end_datetime"),
                "unique_key": row.get("unique_key"),
                "discontinued_code": row.get("discontinued_code"),
            })
    return rows


@app.get("/health")
def health():
    return {"status": "ok", "epdd_cached": EPDD_LOCAL.exists(), "dip_cached": DIP_LOCAL.exists()}


@app.get("/sftp-debug")
def sftp_debug():
    s, transport, sftp = _open_sftp()
    try:
        cwd = sftp.getcwd()
        dot_entries = _safe_listdir(sftp, ".")
        root_entries = _safe_listdir(sftp, "/")
        return {
            "connected": True,
            "cwd": cwd,
            "configured_epdd_path": s["epdd_path"],
            "configured_dip_path": s["dip_path"],
            "dot_entries": dot_entries[:100],
            "root_entries": root_entries[:100],
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SFTP diagnostic failed: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            sftp.close()
        finally:
            transport.close()


@app.post("/refresh", response_model=RefreshResult)
def refresh():
    return refresh_files()


@app.get("/products/{style}")
def get_products(style: str, color: Optional[str] = Query(default=None), size: Optional[str] = Query(default=None)):
    rows = search_epdd(style, color, size)
    if not rows:
        raise HTTPException(status_code=404, detail="No matching SanMar product rows found.")
    return {"style": style, "count": len(rows), "variants": rows}


@app.get("/inventory/{style}")
def get_inventory(style: str, color: Optional[str] = Query(default=None), size: Optional[str] = Query(default=None), warehouse: Optional[str] = Query(default=None)):
    rows = search_dip(style, color, size)
    if warehouse:
        rows = [r for r in rows if r["warehouse"] == str(warehouse)]
    if not rows:
        raise HTTPException(status_code=404, detail="No matching SanMar inventory rows found.")
    grouped = defaultdict(lambda: {"total_quantity": 0, "warehouses": []})
    for row in rows:
        key = f'{row["style"]}|{row["color"]}|{row["size"]}|{row["unique_key"]}'
        grouped[key].update({
            "style": row["style"], "color": row["color"], "size": row["size"],
            "unique_key": row["unique_key"], "piece_price": row["piece_price"],
            "case_price": row["case_price"], "discontinued_code": row["discontinued_code"],
        })
        grouped[key]["total_quantity"] += row["quantity"]
        grouped[key]["warehouses"].append({"warehouse": row["warehouse"], "quantity": row["quantity"]})
    return {"style": style, "variants": list(grouped.values())}


@app.get("/product-summary/{style}")
def product_summary(style: str):
    product_rows = search_epdd(style)
    if not product_rows:
        raise HTTPException(status_code=404, detail="No matching SanMar product rows found.")
    inventory_rows = search_dip(style)
    inventory_by_key = defaultdict(int)
    for row in inventory_rows:
        inventory_by_key[row["unique_key"]] += row["quantity"]
    variants = []
    for row in product_rows:
        row = dict(row)
        row["total_inventory"] = inventory_by_key.get(row["unique_key"], 0)
        variants.append(row)
    first = variants[0]
    return {
        "style": style,
        "title": first.get("title"),
        "brand": first.get("brand"),
        "category": first.get("category"),
        "subcategory": first.get("subcategory"),
        "description": first.get("description"),
        "variant_count": len(variants),
        "variants": variants,
    }


@app.get("/privacy")
def privacy():
    return {
        "service": "SanMar Connector",
        "statement": "This private integration uses SanMar credentials only to retrieve authorized supplier catalog and inventory data. Credentials are stored as server environment variables and are not returned by the API.",
    }
