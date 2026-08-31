import csv
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import paramiko
from fastapi import FastAPI, HTTPException, Query, Response, status

app = FastAPI(
    title="SanMar Connector",
    version="0.4.0",
    description="Private API for searching SanMar EPDD product data and sanmar_dip inventory data.",
)

CACHE_DIR = Path(os.getenv("SANMAR_CACHE_DIR", "/tmp/sanmar"))
EPDD_LOCAL = CACHE_DIR / "SanMar_EPDD.csv"
DIP_LOCAL = CACHE_DIR / "sanmar_dip.txt"
CHUNK_SIZE = int(os.getenv("SANMAR_DOWNLOAD_CHUNK_SIZE", str(1024 * 1024)))
MAX_DOWNLOAD_ATTEMPTS = int(os.getenv("SANMAR_DOWNLOAD_ATTEMPTS", "8"))
RETRY_DELAY_SECONDS = int(os.getenv("SANMAR_RETRY_DELAY_SECONDS", "5"))

_refresh_lock = threading.Lock()
_refresh_state = {
    "state": "idle",
    "started_at": None,
    "finished_at": None,
    "error": None,
    "current_file": None,
    "attempt": 0,
    "epdd_cached": EPDD_LOCAL.exists(),
    "dip_cached": DIP_LOCAL.exists(),
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


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
        raise RuntimeError("SanMar SFTP credentials are not configured.")
    return s


def _open_sftp():
    s = _require_credentials()
    transport = paramiko.Transport((s["host"], s["port"]))
    transport.connect(username=s["username"], password=s["password"])
    transport.set_keepalive(30)
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
    parent = str(Path(configured_path).parent).replace("\\", "/")
    search_dirs = [".", "/"]
    if parent not in ("", "."):
        search_dirs.extend([parent, "/" + parent.lstrip("/")])

    for directory in search_dirs:
        for name in _safe_listdir(sftp, directory):
            if name.lower() == target_name:
                if directory in (".", ""):
                    return name
                return f"{directory.rstrip('/')}/{name}"

    visible = sorted(set(_safe_listdir(sftp, ".") + _safe_listdir(sftp, "/")))
    raise FileNotFoundError(
        f"Could not locate '{configured_path}' on SanMar SFTP. Visible top-level entries: {visible[:100]}"
    )


def _part_path(local_path: Path) -> Path:
    return local_path.with_suffix(local_path.suffix + ".part")


def _download_file_resumable(configured_remote_path: str, local_path: Path, label: str):
    local_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _part_path(local_path)
    last_error = None

    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        _refresh_state["current_file"] = label
        _refresh_state["attempt"] = attempt
        transport = None
        sftp = None
        remote_file = None
        try:
            _, transport, sftp = _open_sftp()
            resolved_path = _resolve_remote_path(sftp, configured_remote_path)
            remote_size = sftp.stat(resolved_path).st_size

            offset = temp_path.stat().st_size if temp_path.exists() else 0
            if offset > remote_size:
                temp_path.unlink(missing_ok=True)
                offset = 0

            if offset == remote_size and remote_size > 0:
                os.replace(temp_path, local_path)
                return resolved_path

            remote_file = sftp.open(resolved_path, "rb")
            remote_file.set_pipelined(False)
            remote_file.seek(offset)

            with temp_path.open("ab") as local_file:
                while offset < remote_size:
                    data = remote_file.read(min(CHUNK_SIZE, remote_size - offset))
                    if not data:
                        raise IOError(
                            f"Unexpected EOF while downloading {label}: {offset}/{remote_size} bytes"
                        )
                    local_file.write(data)
                    local_file.flush()
                    offset += len(data)

            if temp_path.stat().st_size != remote_size:
                raise IOError(
                    f"Incomplete {label} download: {temp_path.stat().st_size}/{remote_size} bytes"
                )

            os.replace(temp_path, local_path)
            return resolved_path

        except Exception as exc:
            last_error = exc
            if attempt >= MAX_DOWNLOAD_ATTEMPTS:
                break
            time.sleep(RETRY_DELAY_SECONDS)
        finally:
            try:
                if remote_file is not None:
                    remote_file.close()
            except Exception:
                pass
            try:
                if sftp is not None:
                    sftp.close()
            except Exception:
                pass
            try:
                if transport is not None:
                    transport.close()
            except Exception:
                pass

    raise RuntimeError(
        f"{label} download failed after {MAX_DOWNLOAD_ATTEMPTS} attempts. "
        f"Partial file retained for resume. Last error: {type(last_error).__name__}: {last_error}"
    )


def refresh_files():
    s = _require_credentials()
    _download_file_resumable(s["epdd_path"], EPDD_LOCAL, "EPDD")
    _download_file_resumable(s["dip_path"], DIP_LOCAL, "DIP")


def _refresh_worker():
    with _refresh_lock:
        _refresh_state.update({
            "state": "running",
            "started_at": _utc_now(),
            "finished_at": None,
            "error": None,
            "current_file": None,
            "attempt": 0,
            "epdd_cached": EPDD_LOCAL.exists(),
            "dip_cached": DIP_LOCAL.exists(),
        })
        try:
            refresh_files()
            _refresh_state.update({
                "state": "completed",
                "finished_at": _utc_now(),
                "error": None,
                "current_file": None,
                "attempt": 0,
                "epdd_cached": EPDD_LOCAL.exists(),
                "dip_cached": DIP_LOCAL.exists(),
            })
        except Exception as exc:
            _refresh_state.update({
                "state": "failed",
                "finished_at": _utc_now(),
                "error": f"{type(exc).__name__}: {exc}",
                "epdd_cached": EPDD_LOCAL.exists(),
                "dip_cached": DIP_LOCAL.exists(),
            })


def ensure_cache():
    if not EPDD_LOCAL.exists() or not DIP_LOCAL.exists():
        raise HTTPException(
            status_code=503,
            detail="SanMar data is not cached yet. POST /refresh, then check GET /refresh-status until state is completed.",
        )


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
    return {
        "status": "ok",
        "cache_dir": str(CACHE_DIR),
        "epdd_cached": EPDD_LOCAL.exists(),
        "dip_cached": DIP_LOCAL.exists(),
    }


@app.get("/sftp-debug")
def sftp_debug():
    s, transport, sftp = _open_sftp()
    try:
        return {
            "connected": True,
            "cwd": sftp.getcwd(),
            "configured_epdd_path": s["epdd_path"],
            "configured_dip_path": s["dip_path"],
            "dot_entries": _safe_listdir(sftp, ".")[:100],
            "root_entries": _safe_listdir(sftp, "/")[:100],
            "sanmarpdd_entries": _safe_listdir(sftp, "SanMarPDD")[:100],
        }
    finally:
        try:
            sftp.close()
        finally:
            transport.close()


@app.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
def refresh(response: Response):
    if _refresh_state["state"] == "running":
        response.status_code = status.HTTP_202_ACCEPTED
        return {"accepted": True, "message": "Refresh is already running.", **_refresh_state}

    thread = threading.Thread(target=_refresh_worker, daemon=True)
    thread.start()
    return {
        "accepted": True,
        "message": "SanMar refresh started in the background. Check /refresh-status for progress.",
        "status_url": "/refresh-status",
    }


@app.get("/refresh-status")
def refresh_status():
    epdd_part = _part_path(EPDD_LOCAL)
    dip_part = _part_path(DIP_LOCAL)
    return {
        **_refresh_state,
        "cache_dir": str(CACHE_DIR),
        "epdd_exists": EPDD_LOCAL.exists(),
        "dip_exists": DIP_LOCAL.exists(),
        "epdd_size_bytes": EPDD_LOCAL.stat().st_size if EPDD_LOCAL.exists() else 0,
        "dip_size_bytes": DIP_LOCAL.stat().st_size if DIP_LOCAL.exists() else 0,
        "epdd_partial_bytes": epdd_part.stat().st_size if epdd_part.exists() else 0,
        "dip_partial_bytes": dip_part.stat().st_size if dip_part.exists() else 0,
    }


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
            "style": row["style"],
            "color": row["color"],
            "size": row["size"],
            "unique_key": row["unique_key"],
            "piece_price": row["piece_price"],
            "case_price": row["case_price"],
            "discontinued_code": row["discontinued_code"],
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
