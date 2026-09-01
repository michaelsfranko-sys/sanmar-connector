from main import app
from shopify_connector import router as shopify_router
from shopify_import import router as shopify_import_router
from shopify_update import router as shopify_update_router
from shopify_media import router as shopify_media_router

app.include_router(shopify_router)
app.include_router(shopify_import_router)
app.include_router(shopify_update_router)
app.include_router(shopify_media_router)
