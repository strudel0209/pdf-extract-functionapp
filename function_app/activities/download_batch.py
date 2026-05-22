"""Activity: Download a batch of files from SharePoint via Graph $batch API
and upload them to Azure Blob Storage.

Each invocation handles up to BATCH_SIZE files (default 200), using the
Graph $batch endpoint (20 requests per call) to minimize HTTP overhead.
Persists full SP metadata as a sidecar JSON blob alongside each PDF.
"""
import json
import logging

import azure.durable_functions as df
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from shared.config import (
    BATCH_SIZE,
    BLOB_CONTAINER_NAME,
    GRAPH_BATCH_SIZE,
    SP_SITE_ID,
    SP_SITE_PREFIX,
    STORAGE_ACCOUNT_URL,
)
from shared.graph_client import create_graph_client

bp = df.Blueprint()
logger = logging.getLogger(__name__)


def _build_blob_name(file_ref: str) -> str:
    """Convert SP FileRef to a blob name.

    Strategy:
    1. If SP_SITE_PREFIX is configured and FileRef starts with it, strip it.
       Example: "/sites/QualityAlerts/Shared Documents/doc.pdf" -> "Shared Documents/doc.pdf".
    2. Otherwise strip the first two path segments ("/sites/<sitename>/")
       which is the conventional SharePoint server-relative URL shape.
    3. Fallback: strip leading slash.
    """
    if SP_SITE_PREFIX and file_ref.startswith(SP_SITE_PREFIX):
        return file_ref[len(SP_SITE_PREFIX):]

    parts = file_ref.lstrip("/").split("/", 2)
    if len(parts) == 3 and parts[0] == "sites":
        return parts[2]
    return file_ref.lstrip("/")


@bp.activity_trigger(input_name="payload")
def download_batch(payload: dict) -> dict:
    """Download a batch of files and upload to Blob Storage.

    Input payload:
        files: list[dict] - file metadata dicts from list_and_filter_files
        drive_id: str - SharePoint drive ID for file download
        batch_index: int - for logging/tracking

    Returns:
        {succeeded: int, failed: int, errors: list[str]}
    """
    files = payload["files"]
    drive_id = payload["drive_id"]
    batch_index = payload.get("batch_index", 0)

    logger.info(f"[Batch {batch_index}] Processing {len(files)} files")

    # Initialize Graph client for downloads
    graph = create_graph_client()

    # Initialize Blob client (uses Managed Identity via DefaultAzureCredential)
    credential = DefaultAzureCredential()
    blob_service = BlobServiceClient(account_url=STORAGE_ACCOUNT_URL, credential=credential)
    container_client = blob_service.get_container_client(BLOB_CONTAINER_NAME)

    succeeded = 0
    failed = 0
    errors = []

    try:
        # Process in sub-batches of GRAPH_BATCH_SIZE (20) for $batch API
        item_ids = [f["item_id"] for f in files]
        file_map = {f["item_id"]: f for f in files}

        results = graph.batch_download(drive_id=drive_id, item_ids=item_ids)

        for result in results:
            item_id = result["item_id"]
            content = result["content"]
            status = result["status"]
            file_info = file_map.get(item_id, {})
            file_ref = file_info.get("file_ref", item_id)

            if status == 200 and content:
                blob_name = _build_blob_name(file_ref)
                try:
                    # Upload the PDF file
                    blob_client = container_client.get_blob_client(blob_name)
                    blob_client.upload_blob(
                        data=content,
                        overwrite=True,
                    )

                    # Upload full SP metadata as sidecar JSON
                    metadata_blob_name = f"{blob_name}.metadata.json"
                    metadata_payload = {
                        "sp_item_id": item_id,
                        "sp_file_ref": file_ref,
                        "sp_file_leaf_ref": file_info.get("file_leaf_ref", ""),
                        **file_info.get("metadata", {}),
                    }
                    meta_client = container_client.get_blob_client(metadata_blob_name)
                    meta_client.upload_blob(
                        data=json.dumps(metadata_payload, ensure_ascii=False, default=str),
                        overwrite=True,
                        content_settings=ContentSettings(content_type="application/json"),
                    )

                    succeeded += 1
                except Exception as e:
                    failed += 1
                    errors.append(f"Blob upload failed for {file_ref}: {str(e)[:200]}")
            elif status == 429:
                # Throttled items — report as failed so orchestrator can retry
                failed += 1
                errors.append(f"Throttled: {file_ref}")
            else:
                failed += 1
                errors.append(f"Download failed ({status}): {file_ref}")

    finally:
        graph.close()

    logger.info(f"[Batch {batch_index}] Done: {succeeded} ok, {failed} failed")
    return {"succeeded": succeeded, "failed": failed, "errors": errors[:50]}
