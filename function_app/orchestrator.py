"""Durable Orchestrator: coordinates the SP → Blob pipeline.

Workflow:
1. Call ListAndFilterFiles activity → get all approved PDF metadata
2. Resolve the drive ID for the SharePoint site
3. Chunk files into batches of BATCH_SIZE
4. Fan-out: dispatch up to MAX_CONCURRENT_DOWNLOADS DownloadBatch activities
5. Aggregate results
"""
import logging

import azure.durable_functions as df

from shared.config import BATCH_SIZE, MAX_CONCURRENT_DOWNLOADS

bp = df.Blueprint()
logger = logging.getLogger(__name__)


@bp.orchestration_trigger(context_name="context")
def sp_to_blob_orchestrator(context: df.DurableOrchestrationContext):
    """Main orchestrator — fan-out/fan-in pattern for SP file downloads."""

    if not context.is_replaying:
        logging.info("Orchestrator started: listing SharePoint files...")

    # Phase 1: List and filter files from SharePoint
    files = yield context.call_activity("list_and_filter_files", {"trigger": "scheduled"})

    if not files:
        return {"status": "no_files", "total": 0}

    # Phase 2: Resolve drive ID (sub-activity for determinism)
    drive_id = yield context.call_activity("resolve_drive_id", {})

    # Phase 3: Chunk files into batches
    batches = [files[i:i + BATCH_SIZE] for i in range(0, len(files), BATCH_SIZE)]

    if not context.is_replaying:
        logging.info(f"Dispatching {len(batches)} batches ({len(files)} total files)")

    # Phase 4: Fan-out with concurrency throttle
    # Dispatch batches in waves of MAX_CONCURRENT_DOWNLOADS
    all_results = []
    for wave_start in range(0, len(batches), MAX_CONCURRENT_DOWNLOADS):
        wave = batches[wave_start:wave_start + MAX_CONCURRENT_DOWNLOADS]
        tasks = [
            context.call_activity(
                "download_batch",
                {
                    "files": batch,
                    "drive_id": drive_id,
                    "batch_index": wave_start + idx,
                },
            )
            for idx, batch in enumerate(wave)
        ]
        wave_results = yield context.task_all(tasks)
        all_results.extend(wave_results)

    # Phase 5: Aggregate results
    total_succeeded = sum(r["succeeded"] for r in all_results)
    total_failed = sum(r["failed"] for r in all_results)
    all_errors = []
    for r in all_results:
        all_errors.extend(r.get("errors", []))

    return {
        "status": "completed",
        "total_files": len(files),
        "total_batches": len(batches),
        "succeeded": total_succeeded,
        "failed": total_failed,
        "sample_errors": all_errors[:20],
    }
