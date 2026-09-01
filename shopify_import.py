import html
import os
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from main import search_epdd
from shopify_connector import _graphql, _shopify_settings

router = APIRouter(prefix="/shopify", tags=["Shopify Import"])


class SanMarDraftImportRequest(BaseModel):
    style: str = Field(min_length=1, max_length=40)
    colors: list[str] = Field(min_length=1, max_length=50)
    sizes: list[str] = Field(min_length=1, max_length=50)
    retail_price: Optional[float] = Field(default=None, ge=0)
    title: Optional[str] = Field(default=None, max_length=255)
    tags: list[str] = Field(default_factory=list, max_length=50)
    confirm: bool = False
    allow_duplicate: bool = False


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def _as_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_tag(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    return cleaned[:255]


def _product_admin_url(product_id: str) -> str:
    numeric_id = product_id.rsplit("/", 1)[-1]
    store = _shopify_settings()["store"]
    return f"https://admin.shopify.com/store/{store.split('.')[0]}/products/{numeric_id}"


def _find_existing_by_style(style: str):
    tag = f"sanmar_style_{_safe_tag(style)}"
    query = """
    query ExistingSanMarProduct($query: String!) {
      products(first: 1, query: $query) {
        nodes { id title status handle }
      }
    }
    """
    data = _graphql(query, {"query": f"tag:{tag}"})
    nodes = ((data.get("products") or {}).get("nodes") or [])
    return nodes[0] if nodes else None


@router.post("/import-sanmar-draft")
def import_sanmar_draft(request: SanMarDraftImportRequest):
    if not request.confirm:
        raise HTTPException(
            status_code=400,
            detail="Import not confirmed. Set confirm=true only after the user has approved the selected style, colors, and sizes.",
        )

    style = request.style.strip()
    requested_colors = {_norm(c) for c in request.colors if c.strip()}
    requested_sizes = {_norm(s) for s in request.sizes if s.strip()}
    if not requested_colors or not requested_sizes:
        raise HTTPException(status_code=400, detail="At least one color and one size are required.")

    if not request.allow_duplicate:
        existing = _find_existing_by_style(style)
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "A Shopify product for this SanMar style already exists.",
                    "existing_product": existing,
                    "hint": "Set allow_duplicate=true only if you intentionally want another Shopify product for the same SanMar style.",
                },
            )

    product_rows = search_epdd(style)
    if not product_rows:
        raise HTTPException(status_code=404, detail=f"SanMar style {style} was not found.")

    selected = []
    seen = set()
    for row in product_rows:
        color = row.get("color")
        size = row.get("size")
        if _norm(color) not in requested_colors or _norm(size) not in requested_sizes:
            continue
        key = (_norm(color), _norm(size))
        if key in seen:
            continue
        seen.add(key)
        selected.append(row)

    if not selected:
        raise HTTPException(
            status_code=404,
            detail="No SanMar variants matched the requested color and size selections.",
        )

    max_variants = int(os.getenv("SHOPIFY_IMPORT_MAX_VARIANTS", "100"))
    if len(selected) > max_variants:
        raise HTTPException(
            status_code=400,
            detail=f"This request would create {len(selected)} variants. The connector limit is {max_variants}; narrow the selected colors/sizes.",
        )

    # Preserve SanMar's canonical option spelling/order from the matched rows.
    colors = []
    sizes = []
    for row in selected:
        if row.get("color") and row["color"] not in colors:
            colors.append(row["color"])
        if row.get("size") and row["size"] not in sizes:
            sizes.append(row["size"])

    first = selected[0]
    product_title = request.title or first.get("title") or f"SanMar {style}"
    description = first.get("description") or ""
    description_html = f"<p>{html.escape(description)}</p>" if description else ""
    style_tag = f"sanmar_style_{_safe_tag(style)}"
    tags = list(dict.fromkeys(["SanMar", style_tag, style] + request.tags))

    product_input = {
        "title": product_title,
        "descriptionHtml": description_html,
        "vendor": first.get("brand") or "SanMar",
        "productType": first.get("category") or "",
        "status": "DRAFT",
        "tags": tags,
        "productOptions": [
            {"name": "Color", "values": [{"name": value} for value in colors]},
            {"name": "Size", "values": [{"name": value} for value in sizes]},
        ],
        "metafields": [
            {
                "namespace": "supplier",
                "key": "name",
                "type": "single_line_text_field",
                "value": "SanMar",
            },
            {
                "namespace": "supplier",
                "key": "style",
                "type": "single_line_text_field",
                "value": style,
            },
        ],
    }

    # Add representative SanMar model images for selected colors when URLs are available.
    media = []
    seen_urls = set()
    for row in selected:
        url = row.get("front_model_image_url")
        if not url or not str(url).startswith("https://") or url in seen_urls:
            continue
        seen_urls.add(url)
        media.append(
            {
                "originalSource": url,
                "alt": f"{product_title} - {row.get('color') or ''}".strip(" -"),
                "mediaContentType": "IMAGE",
            }
        )
        if len(media) >= 10:
            break

    create_mutation = """
    mutation CreateSanMarDraft($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
      productCreate(product: $product, media: $media) {
        product {
          id
          title
          handle
          status
          options { id name values }
        }
        userErrors { field message }
      }
    }
    """
    create_data = _graphql(create_mutation, {"product": product_input, "media": media})
    create_payload = create_data.get("productCreate") or {}
    create_errors = create_payload.get("userErrors") or []
    product = create_payload.get("product")
    if create_errors or not product:
        raise HTTPException(
            status_code=502,
            detail={"message": "Shopify productCreate failed.", "user_errors": create_errors},
        )

    variant_inputs = []
    for row in selected:
        variant = {
            "optionValues": [
                {"name": row.get("color"), "optionName": "Color"},
                {"name": row.get("size"), "optionName": "Size"},
            ],
            "price": f"{request.retail_price:.2f}" if request.retail_price is not None else "0.00",
            "inventoryItem": {
                "sku": str(row.get("unique_key") or f"{style}-{row.get('color')}-{row.get('size')}"),
                "tracked": False,
            },
            "metafields": [
                {
                    "namespace": "supplier",
                    "key": "piece_cost",
                    "type": "single_line_text_field",
                    "value": str(row.get("piece_price") or ""),
                },
                {
                    "namespace": "supplier",
                    "key": "msrp",
                    "type": "single_line_text_field",
                    "value": str(row.get("msrp") or ""),
                },
                {
                    "namespace": "supplier",
                    "key": "map",
                    "type": "single_line_text_field",
                    "value": str(row.get("map_pricing") or ""),
                },
            ],
        }
        gtin = row.get("gtin")
        if gtin:
            variant["barcode"] = str(gtin)
        msrp = _as_float(row.get("msrp"))
        if request.retail_price is not None and msrp is not None and msrp > request.retail_price:
            variant["compareAtPrice"] = f"{msrp:.2f}"
        variant_inputs.append(variant)

    variants_mutation = """
    mutation CreateSanMarVariants($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
      productVariantsBulkCreate(
        productId: $productId,
        variants: $variants,
        strategy: REMOVE_STANDALONE_VARIANT
      ) {
        product { id title status }
        productVariants {
          id
          title
          price
          selectedOptions { name value }
          inventoryItem { id sku tracked }
        }
        userErrors { field message }
      }
    }
    """
    variants_data = _graphql(
        variants_mutation,
        {"productId": product["id"], "variants": variant_inputs},
    )
    variants_payload = variants_data.get("productVariantsBulkCreate") or {}
    variant_errors = variants_payload.get("userErrors") or []
    created_variants = variants_payload.get("productVariants") or []
    if variant_errors:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "The draft product was created, but Shopify variant creation returned errors.",
                "product_id": product["id"],
                "admin_url": _product_admin_url(product["id"]),
                "user_errors": variant_errors,
            },
        )

    return {
        "status": "created",
        "shopify_status": "DRAFT",
        "product_id": product["id"],
        "title": product.get("title"),
        "handle": product.get("handle"),
        "admin_url": _product_admin_url(product["id"]),
        "sanmar_style": style,
        "selected_colors": colors,
        "selected_sizes": sizes,
        "variant_count": len(created_variants),
        "price_mode": "manual_placeholder" if request.retail_price is None else "specified",
        "retail_price": request.retail_price,
        "note": "Product was created as DRAFT. If no retail_price was supplied, variants were imported at $0.00 for manual pricing before publication.",
    }
