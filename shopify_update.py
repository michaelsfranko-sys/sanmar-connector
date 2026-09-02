import os
from typing import Literal, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from main import search_epdd
from shopify_connector import _graphql
from shopify_import import _norm, _product_admin_url, _require_connector_key, _safe_tag

router = APIRouter(prefix="/shopify", tags=["Shopify Update"])


class SanMarDraftUpdateRequest(BaseModel):
    store: Literal["quality-image", "prestige"] = "quality-image"
    style: str = Field(min_length=1, max_length=40)
    colors: list[str] = Field(min_length=1, max_length=50)
    sizes: list[str] = Field(min_length=1, max_length=50)
    mode: Literal["add", "replace"] = "add"
    retail_price: Optional[float] = Field(default=None, ge=0)
    confirm: bool = False


def _existing_product(style: str, store_key: str | None = None):
    tag = f"sanmar_style_{_safe_tag(style)}"
    query = """
    query ExistingSanMarDraft($query: String!) {
      products(first: 1, query: $query) {
        nodes {
          id
          title
          handle
          status
          options {
            id
            name
            position
            optionValues { id name hasVariants }
          }
          variants(first: 250) {
            nodes {
              id
              title
              price
              compareAtPrice
              barcode
              selectedOptions { name value }
              inventoryItem { id sku tracked }
            }
          }
        }
      }
    }
    """
    data = _graphql(query, {"query": f"tag:{tag}"}, store_key=store_key)
    nodes = ((data.get("products") or {}).get("nodes") or [])
    return nodes[0] if nodes else None


def _selected_sanmar_rows(style: str, colors: list[str], sizes: list[str]):
    requested_colors = {_norm(c) for c in colors if c.strip()}
    requested_sizes = {_norm(s) for s in sizes if s.strip()}
    if not requested_colors or not requested_sizes:
        raise HTTPException(status_code=400, detail="At least one color and one size are required.")

    rows = search_epdd(style)
    if not rows:
        raise HTTPException(status_code=404, detail=f"SanMar style {style} was not found.")

    selected = []
    seen = set()
    for row in rows:
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
        raise HTTPException(status_code=404, detail="No SanMar variants matched the requested color and size selections.")
    return selected


def _combo_from_options(options):
    values = {str(o.get("name") or "").lower(): _norm(o.get("value")) for o in options or []}
    return (values.get("color", ""), values.get("size", ""))


@router.post("/update-sanmar-draft")
def update_sanmar_draft(
    request: SanMarDraftUpdateRequest,
    authorization: Optional[str] = Header(default=None),
):
    _require_connector_key(authorization)
    if not request.confirm:
        raise HTTPException(
            status_code=400,
            detail="Update not confirmed. Set confirm=true only after the user approves the style, colors, sizes, and update mode.",
        )

    store_key = request.store
    style = request.style.strip()
    product = _existing_product(style, store_key)
    if not product:
        raise HTTPException(status_code=404, detail=f"No Shopify product tagged for SanMar style {style} was found in {store_key}.")
    if product.get("status") != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "The matching Shopify product is not a draft. This endpoint only modifies DRAFT products.",
                "store": store_key,
                "product_id": product.get("id"),
                "status": product.get("status"),
                "admin_url": _product_admin_url(product.get("id"), store_key),
            },
        )

    selected = _selected_sanmar_rows(style, request.colors, request.sizes)
    max_variants = int(os.getenv("SHOPIFY_IMPORT_MAX_VARIANTS", "100"))
    if len(selected) > max_variants:
        raise HTTPException(
            status_code=400,
            detail=f"This request selects {len(selected)} variants. The connector limit is {max_variants}; narrow the colors/sizes.",
        )

    existing_variants = ((product.get("variants") or {}).get("nodes") or [])
    existing_by_combo = {_combo_from_options(v.get("selectedOptions")): v for v in existing_variants}
    desired_by_combo = {(_norm(r.get("color")), _norm(r.get("size"))): r for r in selected}

    if request.mode == "add":
        new_rows = [row for combo, row in desired_by_combo.items() if combo not in existing_by_combo]
        if not new_rows:
            return {
                "status": "no_change",
                "store": store_key,
                "mode": "add",
                "product_id": product["id"],
                "admin_url": _product_admin_url(product["id"], store_key),
                "sanmar_style": style,
                "existing_variant_count": len(existing_variants),
                "added_variant_count": 0,
                "note": "All requested variants already exist on the draft product.",
            }

        inputs = []
        for row in new_rows:
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
            }
            if row.get("gtin"):
                variant["barcode"] = str(row.get("gtin"))
            inputs.append(variant)

        mutation = """
        mutation AddSanMarVariants($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkCreate(productId: $productId, variants: $variants) {
            product { id title status }
            productVariants { id title price selectedOptions { name value } }
            userErrors { field message }
          }
        }
        """
        data = _graphql(mutation, {"productId": product["id"], "variants": inputs}, store_key=store_key)
        payload = data.get("productVariantsBulkCreate") or {}
        errors = payload.get("userErrors") or []
        if errors:
            raise HTTPException(status_code=502, detail={"message": "Shopify variant add failed.", "user_errors": errors})

        created = payload.get("productVariants") or []
        return {
            "status": "updated",
            "store": store_key,
            "mode": "add",
            "product_id": product["id"],
            "admin_url": _product_admin_url(product["id"], store_key),
            "sanmar_style": style,
            "existing_variant_count": len(existing_variants),
            "added_variant_count": len(created),
            "resulting_variant_count": len(existing_variants) + len(created),
            "retail_price": request.retail_price,
            "note": "Only missing requested variants were added. Existing variants were left unchanged.",
        }

    set_variants = []
    for combo, row in desired_by_combo.items():
        current = existing_by_combo.get(combo)
        item = {
            "optionValues": [
                {"optionName": "Color", "name": row.get("color")},
                {"optionName": "Size", "name": row.get("size")},
            ],
        }
        if current:
            item["id"] = current["id"]
            item["price"] = f"{request.retail_price:.2f}" if request.retail_price is not None else str(current.get("price") or "0.00")
            if current.get("barcode"):
                item["barcode"] = current.get("barcode")
        else:
            item["price"] = f"{request.retail_price:.2f}" if request.retail_price is not None else "0.00"
            item["sku"] = str(row.get("unique_key") or f"{style}-{row.get('color')}-{row.get('size')}")
            if row.get("gtin"):
                item["barcode"] = str(row.get("gtin"))
        set_variants.append(item)

    colors = []
    sizes = []
    for row in selected:
        if row.get("color") and row["color"] not in colors:
            colors.append(row["color"])
        if row.get("size") and row["size"] not in sizes:
            sizes.append(row["size"])

    mutation = """
    mutation ReplaceSanMarVariants($identifier: ProductSetIdentifiers!, $input: ProductSetInput!) {
      productSet(identifier: $identifier, synchronous: true, input: $input) {
        product {
          id
          title
          status
          variants(first: 250) { nodes { id title price selectedOptions { name value } } }
        }
        userErrors { code field message }
      }
    }
    """
    variables = {
        "identifier": {"id": product["id"]},
        "input": {
            "status": "DRAFT",
            "productOptions": [
                {"name": "Color", "position": 1, "values": [{"name": c} for c in colors]},
                {"name": "Size", "position": 2, "values": [{"name": s} for s in sizes]},
            ],
            "variants": set_variants,
        },
    }
    data = _graphql(mutation, variables, store_key=store_key)
    payload = data.get("productSet") or {}
    errors = payload.get("userErrors") or []
    updated = payload.get("product")
    if errors or not updated:
        raise HTTPException(status_code=502, detail={"message": "Shopify replace update failed.", "user_errors": errors})

    resulting = ((updated.get("variants") or {}).get("nodes") or [])
    removed_count = max(0, len(existing_variants) - len([c for c in desired_by_combo if c in existing_by_combo]))
    added_count = len([c for c in desired_by_combo if c not in existing_by_combo])
    return {
        "status": "updated",
        "store": store_key,
        "mode": "replace",
        "product_id": product["id"],
        "admin_url": _product_admin_url(product["id"], store_key),
        "sanmar_style": style,
        "selected_colors": colors,
        "selected_sizes": sizes,
        "previous_variant_count": len(existing_variants),
        "resulting_variant_count": len(resulting),
        "added_variant_count": added_count,
        "removed_variant_count": removed_count,
        "retail_price": request.retail_price,
        "note": "The draft product's variant set now matches the approved SanMar colors and sizes. Existing matching variant prices were preserved unless retail_price was supplied.",
    }
