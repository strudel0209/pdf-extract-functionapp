"""Azure Functions App entry point — Python v2 programming model.

Registers:
- Timer trigger (daily at 02:00 UTC) that starts the Durable orchestration
- HTTP trigger for manual/on-demand orchestration start
- All Durable Functions blueprints (orchestrator + activities)
"""
import logging

import azure.durable_functions as df
import azure.functions as func

# Import blueprints
from activities.download_batch import bp as download_bp
from activities.list_files import bp as list_files_bp
from activities.resolve_drive import bp as resolve_drive_bp
from orchestrator import bp as orchestrator_bp

# Create the Function App
app = func.FunctionApp()

# Register all Durable Functions blueprints
app.register_functions(orchestrator_bp)
app.register_functions(list_files_bp)
app.register_functions(resolve_drive_bp)
app.register_functions(download_bp)

logger = logging.getLogger(__name__)


@app.timer_trigger(
    schedule="0 0 2 * * *",  # Daily at 02:00 UTC
    arg_name="timer",
    run_on_startup=False,
)
@app.durable_client_input(client_name="client")
async def timer_start_orchestration(timer: func.TimerRequest, client):
    """Timer trigger: starts the SP-to-Blob orchestration daily."""
    instance_id = await client.start_new("sp_to_blob_orchestrator", None, None)
    logger.info(f"Timer started orchestration: {instance_id}")


@app.route(route="start", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
@app.durable_client_input(client_name="client")
async def http_start_orchestration(req: func.HttpRequest, client):
    """HTTP trigger: start orchestration on demand (for testing / manual runs)."""
    instance_id = await client.start_new("sp_to_blob_orchestrator", None, None)
    logger.info(f"HTTP started orchestration: {instance_id}")
    return client.create_check_status_response(req, instance_id)
