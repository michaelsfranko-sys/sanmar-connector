# SanMar Connector

Private FastAPI service for retrieving SanMar product and inventory data from the authorized SanMar SFTP account.

## Data sources

- `SanMar_EPDD.csv` for product metadata, descriptions, categories, colors, sizes, images, MSRP/MAP and product status.
- `sanmar_dip.txt` for warehouse-level inventory, pricing, sale pricing and discontinued status.

The API joins these feeds through SanMar `unique_key` values.

## Endpoints

- `GET /health`
- `POST /refresh`
- `GET /products/{style}`
- `GET /inventory/{style}`
- `GET /product-summary/{style}`
- `GET /openapi.json`

Optional query parameters for product/inventory filtering include `color`, `size`, and `warehouse` where applicable.

## Render environment variables

Set these in Render. Never commit actual credentials to GitHub.

- `SANMAR_SFTP_HOST=ftp.sanmar.com`
- `SANMAR_SFTP_PORT=2200`
- `SANMAR_SFTP_USERNAME=<SanMar customer number>`
- `SANMAR_SFTP_PASSWORD=<SanMar FTP password>`
- `SANMAR_EPDD_REMOTE_PATH=/SanMar_EPDD.csv`
- `SANMAR_DIP_REMOTE_PATH=/sanmar_dip.txt`

## Render deployment

This repository includes `render.yaml`. Create a Render Blueprint or Web Service from this repository and provide the two secret credential variables when prompted.

After deployment, visit `/health`, then call `POST /refresh` once to download the SanMar files to the service cache. The first product request will also refresh automatically if the cache is empty.

The FastAPI-generated OpenAPI schema is available at `/openapi.json` and can be used as the basis for a Custom GPT Action after the Render hostname is known.
