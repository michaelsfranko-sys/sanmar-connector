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
    version="0.1.0",
    description="Private API for searching SanMar EPDD product data and sanmar_dip inventory data."
)

CACHE_DIR = Path(os.getenv("SANMAR_CACHE_DIR", "/tmp/sanmar"))
EPDD_LOCAL = CACHE_DIR / "SanMar_EPDD.csv"
DIP_LOCAL = CACHE_DIR / "sanmar_dip.txt"


class RefreshResult(BaseModel):
    epdd_downloaded: bool
    dip_downloaded: bool


def _settings():
    return {
        "host": os.getenv("SANMAR_SFTP_HOST", "ftp.sanmar.com"),
        "port": int(os.getenv("SANMAR_SFTP_PORT", "2200")),
        "username": os.getenv("SANMAR_SFTP_USERNAME"),
        "password": os.getenv("SANMAR_SFTP_PASSWORD"),
        "epdd_path": os.getenv("SANMAR_EPDD_REMOTE_PATH", "/SanMar_EPDD.csv"),
        "dip_path": os.getenv("SANMAR_DIP_REMOTE_PATH", "/sanmar_dip.txt"),
    }


def _require_credentials():
    s = _settings()
    if not s["username"] or not s["password"]:
        raise HTTPException(status_code=500, detail="SanMar SFTP credentials are not configured.")
    return s


def _download_sftp_file(sftp, remote_path: str, local_path: Path):
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = local_path.with_suffix(local_path.suffix + ".part")
    sftp.get(remote_path, str(temp_path))
    shutil.move(str(temp_path), str(local_path))


def refresh_files() -> RefreshResult:
    s = _require_credentials()
    transport = paramiko.Transport((s["host"], s["port"]))
    try:
        transport.connect(username=s["username"], password=s["password"])
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            _download_sftp_file(sftp, s["epdd_path"], EPDD_LOCAL)
            _download_sftp_file(sftp, s["dip_path"], DIP_LOCAL)
        finally:
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
    return value.replace("|", " ").replace("  ", " ").strip()


def search_epdd(style: str, color: Optional[str] = None, size: Optional[str] = None):
    ensure_cache()
    target_style = _norm(style)
    target_color = _norm(color)
    target_size = _norm(size)
    rows = []

    with EPDD_LOCAL.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if _norm(row.get("STYLE#")) != target_style:
                continue
            if color and _norm(row.get("COLOR_NAME")) != target_color:
                continue
            if size and _norm(row.get("SIZE")) != target_size:
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
    target_style = _norm(style)
    target_color = _norm(color)
    target_size = _norm(size)
    rows = []

    with DIP_LOCAL.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            if _norm(row.get("catalog_no")) != target_style:
                continue
            if color and _norm(row.get("catalog_color")) != target_color:
                continue
            if size and _norm(row.get("size")) != target_size:
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
    return {
        "status": "ok",
        "epdd_cached": EPDD_LOCAL.exists(),
        "dip_cached": DIP_LOCAL.exists(),
    }


@app.post("/refresh", response_model=RefreshResult)
def refresh():
    return refresh_files()


@app.get("/products/{style}")
def get_products(
    style: str,
    color: Optional[str] = Query(default=None),
    size: Optional[str] = Query(default=None),
):
    rows = search_epdd(style, color, size)
    if not rows:
        raise HTTPException(status_code=404, detail="No matching SanMar product rows found.")
    return {"style": style, "count": len(rows), "variants": rows}


@app.get("/inventory/{style}")
def get_inventory(
    style: str,
    color: Optional[str] = Query(default=None),
    size: Optional[str] = Query(default=None),
    warehouse: Optional[str] = Query(default=None),
):
    rows = search_dip(style, color, size)
    if warehouse:
        rows = [r for r in rows if r["warehouse"] == str(warehouse)]
    if not rows:
        raise HTTPException(status_code=404, detail="No matching SanMar inventory rows found.")

    grouped = defaultdict(lambda: {"total_quantity": 0, "warehouses": []})
    for row in rows:
        key = f'{row["style"]}|{row["color"]}|{row["size"]}|{row["unique_key"]}'
        grouped[key]["style"] = row["style"]
        grouped[key]["color"] = row["color"]
        grouped[key]["size"] = row["size"]
        grouped[key]["unique_key"] = row["unique_key"]
        grouped[key]["piece_price"] = row["piece_price"]
        grouped[key]["case_price"] = row["case_price"]
        grouped[key]["discontinued_code"] = row["discontinued_code"]
        grouped[key]["total_quantity"] += row["quantity"]
        grouped[key]["warehouses"].append({
            "warehouse": row["warehouse"],
            "quantity": row["quantity"],
        })

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
        "statement": "This private integration uses SanMar credentials only to retrieve authorized supplier catalog and inventory data. Credentials are stored as server environment variables and are not returned by the API."
    }
