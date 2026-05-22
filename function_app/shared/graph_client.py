"""Microsoft Graph API client using Managed Identity + httpx.

Provides:
- Token acquisition via DefaultAzureCredential (Function App MSI in Azure,
  `az login` user locally) — no certificates or secrets
- Paginated list items with server-side $filter
- $batch API for bulk file downloads
- RateLimit-aware retry with Retry-After respect
"""
import logging
import time

import httpx
from azure.identity import DefaultAzureCredential

from shared.config import GRAPH_BASE_URL

logger = logging.getLogger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"


def _acquire_graph_token() -> str:
    """Acquire an access token for Microsoft Graph via Managed Identity.

    Uses DefaultAzureCredential so the same code path works:
    - In Azure: system-assigned managed identity of the Function App
    - Locally: developer credentials from `az login`
    """
    credential = DefaultAzureCredential()
    token = credential.get_token(GRAPH_SCOPE)
    return token.token


class GraphClient:
    """Lightweight Graph API client with $batch support and throttle handling."""

    def __init__(self, access_token: str):
        self._token = access_token
        self._client = httpx.Client(
            base_url=GRAPH_BASE_URL,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "AU-PCP-DocIntell/1.0 (Durable Functions Pipeline)",
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    def close(self):
        self._client.close()

    def _handle_throttle(self, response: httpx.Response) -> None:
        """If throttled (429), sleep for Retry-After duration."""
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "30"))
            logger.warning(f"Throttled by Graph API. Sleeping {retry_after}s...")
            time.sleep(retry_after)

    def get_paginated(self, url: str, params: dict | None = None) -> list[dict]:
        """GET with automatic @odata.nextLink pagination."""
        all_items = []
        next_url = url
        query_params = params

        while next_url:
            response = self._client.get(next_url, params=query_params)
            if response.status_code == 429:
                self._handle_throttle(response)
                continue
            response.raise_for_status()
            data = response.json()
            all_items.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
            query_params = None  # nextLink includes params
            logger.info(f"Fetched {len(all_items)} items so far...")

        return all_items

    def batch_download(self, drive_id: str, item_ids: list[str]) -> list[dict]:
        """Download multiple files using Graph $batch API (max 20 per call).

        Returns list of {item_id, content (bytes), status_code}.
        For file content, Graph returns 302 → download URL. In $batch mode,
        the response includes the content directly or a redirect URL.
        """
        results = []

        for i in range(0, len(item_ids), 20):
            chunk = item_ids[i:i + 20]
            batch_requests = [
                {
                    "id": item_id,
                    "method": "GET",
                    "url": f"/drives/{drive_id}/items/{item_id}/content",
                }
                for item_id in chunk
            ]
            batch_body = {"requests": batch_requests}

            response = self._client.post("/$batch", json=batch_body)
            if response.status_code == 429:
                self._handle_throttle(response)
                # Retry this chunk
                response = self._client.post("/$batch", json=batch_body)

            response.raise_for_status()
            batch_response = response.json()

            for resp in batch_response.get("responses", []):
                item_id = resp["id"]
                status = resp["status"]

                if status == 302:
                    # Follow redirect to get actual file content
                    download_url = resp.get("headers", {}).get("Location", "")
                    if download_url:
                        file_resp = httpx.get(download_url, timeout=120.0)
                        if file_resp.status_code == 200:
                            results.append({"item_id": item_id, "content": file_resp.content, "status": 200})
                        else:
                            results.append({"item_id": item_id, "content": None, "status": file_resp.status_code})
                    else:
                        results.append({"item_id": item_id, "content": None, "status": 302})
                elif status == 200:
                    # Inline binary (unlikely for large files, but handle it)
                    body = resp.get("body", b"")
                    if isinstance(body, str):
                        body = body.encode("latin-1")
                    results.append({"item_id": item_id, "content": body, "status": 200})
                elif status == 429:
                    retry_after = int(resp.get("headers", {}).get("Retry-After", "30"))
                    logger.warning(f"Item {item_id} throttled in batch. Will retry later.")
                    results.append({"item_id": item_id, "content": None, "status": 429})
                else:
                    logger.error(f"Item {item_id} failed with status {status}")
                    results.append({"item_id": item_id, "content": None, "status": status})

        return results


def create_graph_client() -> GraphClient:
    """Factory: acquires a Graph token via Managed Identity and returns a client."""
    return GraphClient(access_token=_acquire_graph_token())
