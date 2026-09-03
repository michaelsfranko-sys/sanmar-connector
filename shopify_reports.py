from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query

from shopify_connector import _graphql

router = APIRouter(prefix="/shopify", tags=["Shopify Reports"])

StoreKey = Literal["quality-image", "prestige"]


def _money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _order_line_items(order_id: str, store: StoreKey):
    query = """
    query OrderLineItems($id: ID!, $after: String) {
      order(id: $id) {
        lineItems(first: 100, after: $after) {
          nodes {
            id
            name
            title
            quantity
            sku
            variantTitle
            originalUnitPriceSet { shopMoney { amount currencyCode } }
            discountedUnitPriceAfterAllDiscountsSet { shopMoney { amount currencyCode } }
            product { id title handle }
            variant { id title sku }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
    """
    after = None
    items = []
    while True:
        data = _graphql(query, {"id": order_id, "after": after}, store_key=store)
        connection = ((data.get("order") or {}).get("lineItems") or {})
        items.extend(connection.get("nodes") or [])
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break
    return items


def _serialize_order(order: dict, store: StoreKey):
    money = (((order.get("currentTotalPriceSet") or {}).get("shopMoney") or {}))
    items = []
    total_units = 0
    for item in _order_line_items(order["id"], store):
        quantity = int(item.get("quantity") or 0)
        total_units += quantity
        item_money = (((item.get("discountedUnitPriceAfterAllDiscountsSet") or {}).get("shopMoney") or {}))
        items.append({
            "line_item_id": item.get("id"),
            "product_id": (item.get("product") or {}).get("id"),
            "product_title": (item.get("product") or {}).get("title") or item.get("title") or item.get("name"),
            "variant_id": (item.get("variant") or {}).get("id"),
            "variant_title": item.get("variantTitle") or (item.get("variant") or {}).get("title"),
            "sku": item.get("sku") or (item.get("variant") or {}).get("sku"),
            "quantity": quantity,
            "discounted_unit_price": _money(item_money.get("amount")),
            "currency": item_money.get("currencyCode") or money.get("currencyCode"),
        })
    return {
        "order_id": order.get("id"),
        "order_name": order.get("name"),
        "created_at": order.get("createdAt"),
        "cancelled": bool(order.get("cancelledAt")),
        "financial_status": order.get("displayFinancialStatus"),
        "fulfillment_status": order.get("displayFulfillmentStatus"),
        "current_total": _money(money.get("amount")),
        "currency": money.get("currencyCode"),
        "total_units": total_units,
        "line_items": items,
    }


@router.get("/orders/recent")
def recent_orders(
    store: StoreKey,
    limit: int = Query(default=5, ge=1, le=25),
):
    gql = """
    query RecentOrders($first: Int!) {
      orders(first: $first, sortKey: CREATED_AT, reverse: true) {
        nodes {
          id
          name
          createdAt
          cancelledAt
          displayFinancialStatus
          displayFulfillmentStatus
          currentTotalPriceSet { shopMoney { amount currencyCode } }
        }
      }
    }
    """
    data = _graphql(gql, {"first": limit}, store_key=store)
    orders = (data.get("orders") or {}).get("nodes") or []
    return {
        "store": store,
        "count": len(orders),
        "orders": [_serialize_order(order, store) for order in orders],
        "note": "Read-only recent order view. Line-item quantities are ordered quantities and are not refund-adjusted.",
    }


@router.get("/orders/report")
def orders_report(
    store: StoreKey,
    start_date: date,
    end_date: date,
    query: Optional[str] = Query(default=None, description="Optional additional Shopify order search filter."),
):
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date.")

    search_parts = [
        f"created_at:>={start_date.isoformat()}",
        f"created_at:<={end_date.isoformat()}T23:59:59",
    ]
    if query and query.strip():
        search_parts.append(query.strip())
    search_query = " ".join(search_parts)

    gql = """
    query OrdersReport($after: String, $query: String!) {
      orders(first: 100, after: $after, query: $query, sortKey: CREATED_AT) {
        nodes {
          id
          name
          createdAt
          cancelledAt
          displayFinancialStatus
          displayFulfillmentStatus
          currentTotalPriceSet { shopMoney { amount currencyCode } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """

    after = None
    orders = []
    while True:
        data = _graphql(gql, {"after": after, "query": search_query}, store_key=store)
        connection = data.get("orders") or {}
        orders.extend(connection.get("nodes") or [])
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break

    line_items = []
    total_sales = 0.0
    currency = None
    cancelled_orders = 0
    total_units = 0

    for order in orders:
        money = (((order.get("currentTotalPriceSet") or {}).get("shopMoney") or {}))
        total_sales += _money(money.get("amount"))
        currency = currency or money.get("currencyCode")
        if order.get("cancelledAt"):
            cancelled_orders += 1

        for item in _order_line_items(order["id"], store):
            quantity = int(item.get("quantity") or 0)
            total_units += quantity
            item_money = (((item.get("discountedUnitPriceAfterAllDiscountsSet") or {}).get("shopMoney") or {}))
            line_items.append({
                "order_id": order.get("id"),
                "order_name": order.get("name"),
                "created_at": order.get("createdAt"),
                "cancelled": bool(order.get("cancelledAt")),
                "financial_status": order.get("displayFinancialStatus"),
                "fulfillment_status": order.get("displayFulfillmentStatus"),
                "line_item_id": item.get("id"),
                "product_id": (item.get("product") or {}).get("id"),
                "product_title": (item.get("product") or {}).get("title") or item.get("title") or item.get("name"),
                "variant_id": (item.get("variant") or {}).get("id"),
                "variant_title": item.get("variantTitle") or (item.get("variant") or {}).get("title"),
                "sku": item.get("sku") or (item.get("variant") or {}).get("sku"),
                "quantity": quantity,
                "discounted_unit_price": _money(item_money.get("amount")),
                "currency": item_money.get("currencyCode") or currency,
            })

    grouped = {}
    for item in line_items:
        key = (item.get("product_title") or "", item.get("variant_title") or "", item.get("sku") or "")
        row = grouped.setdefault(key, {
            "product_title": key[0],
            "variant_title": key[1],
            "sku": key[2],
            "quantity": 0,
        })
        row["quantity"] += item["quantity"]

    return {
        "store": store,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "shopify_query": search_query,
        "order_count": len(orders),
        "cancelled_order_count": cancelled_orders,
        "total_units": total_units,
        "current_total_sales": round(total_sales, 2),
        "currency": currency,
        "grouped_items": sorted(grouped.values(), key=lambda x: (x["product_title"], x["variant_title"], x["sku"])),
        "line_items": line_items,
        "note": "This read-only report paginates all matching orders and all line items. Quantities are ordered quantities; refund-adjusted quantity reconciliation will be added before this endpoint is used as the final purchasing source of truth.",
    }


@router.get("/products/report")
def products_report(store: StoreKey, query: Optional[str] = None):
    gql = """
    query ProductsReport($after: String, $query: String) {
      products(first: 100, after: $after, query: $query, sortKey: TITLE) {
        nodes {
          id title handle status vendor productType tags
          variants(first: 100) {
            nodes { id title sku price inventoryQuantity }
            pageInfo { hasNextPage }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
    """
    after = None
    products = []
    while True:
        data = _graphql(gql, {"after": after, "query": query}, store_key=store)
        connection = data.get("products") or {}
        products.extend(connection.get("nodes") or [])
        page = connection.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = page.get("endCursor")
        if not after:
            break
    return {"store": store, "count": len(products), "products": products}
