from main import app
from shopify_connector import router as shopify_router

app.include_router(shopify_router)
