import os
import re
import uuid
from io import BytesIO
from pathlib import Path
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from PIL import Image
from pydantic import BaseModel, Field, HttpUrl

from main import search_epdd
from shopify_connector import _graphql
from shopify_import import _find_existing_by_style, _norm, _product_admin_url, _require_connector_key

router = APIRouter(prefix="/shopify", tags=["Shopify Media"])

MOCKUP_DIR = Path(os.getenv("MOCKUP_DIR", "/var/data/sanmar/mockups"))
MOCKUP_DIR.mkdir(parents=True, exist_ok=True)
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "https://sanmar-connector.onrender.com").rstrip("/")


class AttachImageRequest(BaseModel):
    style: str = Field(min_length=1, max_length=40)
    image_url: HttpUrl
    alt_text: Optional[str] = Field(default=None, max_length=255)
    confirm: bool = False


class MockupRequest(BaseModel):
    style: str = Field(min_length=1, max_length=40)
    color: str = Field(min_length=1, max_length=100)
    artwork_url: HttpUrl
    placement: Literal["left_chest", "center_chest", "full_front"] = "left_chest"
    scale: float = Field(default=1.0, ge=0.5, le=1.5)
    attach_to_shopify: bool = False
    confirm_attach: bool = False
    alt_text: Optional[str] = Field(default=None, max_length=255)


def _download_image(url: str) -> Image.Image:
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").lower()
            if "image" not in content_type:
                raise HTTPException(status_code=400, detail=f"URL did not return an image: {url}")
            return Image.open(BytesIO(response.content)).convert("RGBA")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to download image: {exc}")


def _find_sanmar_image(style: str, color: str) -> tuple[str, str]:
    rows = search_epdd(style)
    if not rows:
        raise HTTPException(status_code=404, detail=f"SanMar style {style} was not found.")
    matches = [r for r in rows if _norm(r.get("color")) == _norm(color)]
    if not matches:
        raise HTTPException(status_code=404, detail=f"Color {color} was not found for SanMar style {style}.")
    for row in matches:
        for key in ("front_model_image_url", "color_product_image", "front_flat_image"):
            value = str(row.get(key) or "").strip()
            if value.startswith("https://") or value.startswith("http://"):
                return value, str(row.get("color") or color)
    raise HTTPException(status_code=404, detail="No usable public SanMar front image URL was found for that color.")


def _placement_box(width: int, height: int, placement: str, scale: float):
    # Coordinates are relative to a typical front-view SanMar model/product image.
    presets = {
        "left_chest": (0.61, 0.34, 0.16, 0.14),
        "center_chest": (0.50, 0.34, 0.27, 0.16),
        "full_front": (0.50, 0.48, 0.38, 0.34),
    }
    cx, cy, max_w, max_h = presets[placement]
    return int(width * cx), int(height * cy), int(width * max_w * scale), int(height * max_h * scale)


def _composite(garment: Image.Image, artwork: Image.Image, placement: str, scale: float) -> Image.Image:
    gw, gh = garment.size
    cx, cy, max_w, max_h = _placement_box(gw, gh, placement, scale)
    aw, ah = artwork.size
    ratio = min(max_w / max(aw, 1), max_h / max(ah, 1))
    new_size = (max(1, int(aw * ratio)), max(1, int(ah * ratio)))
    logo = artwork.resize(new_size, Image.Resampling.LANCZOS)
    x = int(cx - logo.width / 2)
    y = int(cy - logo.height / 2)
    result = garment.copy()
    result.alpha_composite(logo, (x, y))
    return result


def _attach_product_media(product_id: str, image_url: str, alt_text: str):
    mutation = """
    mutation AddProductMedia($product: ProductUpdateInput!, $media: [CreateMediaInput!]) {
      productUpdate(product: $product, media: $media) {
        product {
          id
          title
          status
          media(first: 20) {
            nodes { id alt mediaContentType preview { status } }
          }
        }
        userErrors { field message }
      }
    }
    """
    variables = {
        "product": {"id": product_id},
        "media": [{"originalSource": image_url, "alt": alt_text, "mediaContentType": "IMAGE"}],
    }
    data = _graphql(mutation, variables)
    payload = data.get("productUpdate") or {}
    errors = payload.get("userErrors") or []
    product = payload.get("product")
    if errors or not product:
        raise HTTPException(status_code=502, detail={"message": "Shopify media attachment failed.", "user_errors": errors})
    return product


@router.post("/attach-image")
def attach_image(request: AttachImageRequest, authorization: Optional[str] = Header(default=None)):
    _require_connector_key(authorization)
    if not request.confirm:
        raise HTTPException(status_code=400, detail="Image attachment not confirmed. Set confirm=true only after user approval.")
    product = _find_existing_by_style(request.style.strip())
    if not product:
        raise HTTPException(status_code=404, detail=f"No Shopify product for SanMar style {request.style} was found.")
    alt = request.alt_text or f"{product.get('title') or request.style} product image"
    updated = _attach_product_media(product["id"], str(request.image_url), alt)
    return {
        "status": "attached",
        "product_id": product["id"],
        "admin_url": _product_admin_url(product["id"]),
        "image_url": str(request.image_url),
        "media_count_returned": len(((updated.get("media") or {}).get("nodes") or [])),
    }


@router.post("/create-mockup")
def create_mockup(request: MockupRequest, authorization: Optional[str] = Header(default=None)):
    _require_connector_key(authorization)
    garment_url, resolved_color = _find_sanmar_image(request.style.strip(), request.color)
    garment = _download_image(garment_url)
    artwork = _download_image(str(request.artwork_url))
    result = _composite(garment, artwork, request.placement, request.scale)

    safe_style = re.sub(r"[^A-Za-z0-9_-]+", "-", request.style.strip())
    safe_color = re.sub(r"[^A-Za-z0-9_-]+", "-", resolved_color.strip())
    filename = f"{safe_style}-{safe_color}-{request.placement}-{uuid.uuid4().hex[:10]}.png"
    path = MOCKUP_DIR / filename
    result.save(path, format="PNG", optimize=True)
    mockup_url = f"{PUBLIC_BASE_URL}/shopify/mockups/{filename}"

    response = {
        "status": "created",
        "style": request.style,
        "color": resolved_color,
        "placement": request.placement,
        "garment_image_url": garment_url,
        "mockup_url": mockup_url,
        "attached_to_shopify": False,
    }

    if request.attach_to_shopify:
        if not request.confirm_attach:
            raise HTTPException(status_code=400, detail="Mockup was created, but Shopify attachment requires confirm_attach=true after user approval.")
        product = _find_existing_by_style(request.style.strip())
        if not product:
            raise HTTPException(status_code=404, detail=f"Mockup was created, but no Shopify product for SanMar style {request.style} was found.")
        alt = request.alt_text or f"{product.get('title') or request.style} - {resolved_color} - {request.placement.replace('_', ' ')} mockup"
        _attach_product_media(product["id"], mockup_url, alt)
        response.update({
            "attached_to_shopify": True,
            "product_id": product["id"],
            "admin_url": _product_admin_url(product["id"]),
        })

    return response


@router.get("/mockups/{filename}", include_in_schema=False)
def get_mockup(filename: str):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
        raise HTTPException(status_code=400, detail="Invalid mockup filename.")
    path = MOCKUP_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Mockup not found.")
    return FileResponse(path, media_type="image/png", filename=filename)
