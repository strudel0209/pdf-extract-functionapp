"""Activity: List all files from a SharePoint document library and filter them.

Uses Graph API /sites/{siteId}/lists/{listId}/items with $expand=fields,driveItem.
Server-side $filter is only applied when SP_USE_CUSTOM_FIELDS=true (production
schema with ABB_Coll_*). In PoC mode we filter by extension client-side using
the standard FileLeafRef column.
"""
import logging

import azure.durable_functions as df

from shared.config import (
    GRAPH_BASE_URL,
    SP_FILE_EXTENSIONS,
    SP_LIST_ID,
    SP_SELECT_FIELDS,
    SP_SITE_ID,
    SP_USE_CUSTOM_FIELDS,
)
from shared.graph_client import create_graph_client

bp = df.Blueprint()
logger = logging.getLogger(__name__)


@bp.activity_trigger(input_name="payload")
def list_and_filter_files(payload: dict) -> list[dict]:
    """Query SharePoint list items via Graph API.

    Returns a list of file metadata dicts ready for download batching.
    Each dict contains: item_id (drive item ID), file_ref, file_leaf_ref, metadata.
    """
    graph = create_graph_client()
    try:
        fields_select = ",".join(SP_SELECT_FIELDS)

        url = f"/sites/{SP_SITE_ID}/lists/{SP_LIST_ID}/items"
        params: dict = {
            # Expand driveItem so we get the proper drive item id required by
            # the /drives/{driveId}/items/{itemId}/content endpoint.
            "$expand": f"fields($select={fields_select}),driveItem",
            "$top": "5000",
        }

        if SP_USE_CUSTOM_FIELDS:
            # Production schema: rely on customer-managed columns for filtering.
            params["$filter"] = (
                "fields/NextECM_Mig_File_Ext eq 'pdf' "
                "and fields/ABB_Coll_LifecycleStatus eq 'Approved'"
            )

        all_items = graph.get_paginated(url, params)
        logger.info(f"Total items retrieved from SharePoint: {len(all_items)}")

        allowed_exts = set(SP_FILE_EXTENSIONS)
        files = []
        for item in all_items:
            fields = item.get("fields", {})

            # Skip folders (FileSystemObjectType == 1 is a folder, 0 is a file).
            if fields.get("FileSystemObjectType", 0) != 0:
                continue

            file_leaf_ref = fields.get("FileLeafRef", "")

            # PoC client-side extension filter (cheap, runs on already-fetched rows).
            if not SP_USE_CUSTOM_FIELDS and allowed_exts:
                ext = file_leaf_ref.rsplit(".", 1)[-1].lower() if "." in file_leaf_ref else ""
                if ext not in allowed_exts:
                    continue

            # Use the driveItem.id (real drive item id) for downloading via /drives endpoint.
            drive_item = item.get("driveItem") or {}
            item_id = drive_item.get("id")
            if not item_id:
                logger.warning(f"Skipping item without driveItem.id: {file_leaf_ref}")
                continue

            files.append({
                "item_id": item_id,
                "file_ref": fields.get("FileRef", ""),
                "file_leaf_ref": file_leaf_ref,
                "metadata": {k: fields.get(k) for k in SP_SELECT_FIELDS if k in fields},
            })

        logger.info(f"Filtered to {len(files)} files for processing.")
        return files

    finally:
        graph.close()
