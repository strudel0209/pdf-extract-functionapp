"""Activity: Resolve the SharePoint drive ID for downloading files.

Separated as its own activity to keep the orchestrator deterministic
(no direct I/O calls in the orchestrator).
"""
import logging

import azure.durable_functions as df

from shared.config import SP_SITE_ID
from shared.graph_client import create_graph_client

bp = df.Blueprint()
logger = logging.getLogger(__name__)


@bp.activity_trigger(input_name="payload")
def resolve_drive_id(payload: dict) -> str:
    """Get the default document library drive ID for the SharePoint site."""
    graph = create_graph_client()
    try:
        response = graph._client.get(f"/sites/{SP_SITE_ID}/drive")
        response.raise_for_status()
        drive_id = response.json()["id"]
        logger.info(f"Resolved drive ID: {drive_id}")
        return drive_id
    finally:
        graph.close()
