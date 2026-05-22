# AU-PCP-DocIntell

Azure Durable Functions pipeline that mirrors PDF documents (and their SharePoint metadata) from a SharePoint Online document library into Azure Blob Storage, as the first stage of a Document Intelligence → Azure OpenAI → Snowflake ingest pipeline.

- **Runtime:** Azure Functions (Python v2 model), Flex Consumption plan, Python 3.11
- **SharePoint access:** Microsoft Graph API (`/sites/{id}/lists/{id}/items` + `$batch` downloads)
- **Auth:** System-assigned Managed Identity end-to-end — no certificates, no client secrets, no Key Vault
- **Output:** `source-pdfs/<library-path>/<filename>.pdf` + sibling `<filename>.pdf.metadata.json` sidecar

See [architecture-description.md](architecture-description.md) for the full design rationale, cost model, and lessons learned.

<p align="center">
  <img src="architecture-durable-functions.svg" alt="Durable Functions + Graph API pipeline architecture" width="100%">
</p>

---

## Repository layout

```
.
├── architecture-description.md         # Design doc + lessons learned
├── architecture-durable-functions.svg  # Pipeline diagram
├── sp-to-blob.ipynb                    # Original notebook (baseline reference)
└── function_app/                       # Azure Functions project (Python v2)
    ├── function_app.py                 # App entrypoint — registers blueprints
    ├── orchestrator.py                 # Durable orchestrator
    ├── host.json                       # Functions host config (concurrency, ext bundle)
    ├── requirements.txt                # Python runtime deps
    ├── local.settings.sample.json      # Committed template — copy to local.settings.json
    ├── activities/
    │   ├── resolve_drive_id.py         # Resolves SP list → driveId
    │   ├── list_files.py               # Lists + filters list items, expands driveItem
    │   └── download_batch.py           # Graph $batch download → blob upload + JSON sidecar
    └── shared/
        ├── config.py                   # Env-var-driven configuration
        └── graph_client.py             # Async Graph HTTP client (MSI tokens via httpx)
```

---

## Prerequisites

### Local development
| Tool | Version | Install |
|---|---|---|
| Python | 3.11 (match the Flex runtime) | [python.org](https://www.python.org/downloads/) |
| Azure Functions Core Tools | v4 | `npm i -g azure-functions-core-tools@4 --unsafe-perm true` |
| Azure CLI | 2.60+ | [docs](https://learn.microsoft.com/cli/azure/install-azure-cli) |
| Azurite (local storage emulator) | latest | `npm i -g azurite` |
| VS Code + Azure Functions extension | — | Recommended |

> A devcontainer is provided — opening the repo in VS Code with the Dev Containers extension installs the full toolchain automatically.

### Azure
- An Azure subscription with permission to create resource groups, storage accounts, and Function Apps in your chosen region.
- Microsoft Entra tenant admin (or Application Administrator) to consent the Graph permission for the Function App's Managed Identity.
- A SharePoint Online site with a document library to read from.

---

## Local setup

```bash
# 1. Clone
git clone https://github.com/<your-org>/AU-PCP-DocIntell.git
cd AU-PCP-DocIntell/function_app

# 2. Python virtual environment
python3.11 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Configure
cp local.settings.sample.json local.settings.json
# Edit local.settings.json — fill in SP_SITE_ID, SP_LIST_ID, STORAGE_ACCOUNT_URL, SP_SITE_PREFIX

# 4. Run the storage emulator (in a separate terminal)
azurite --silent --location /tmp/azurite

# 5. Sign in so DefaultAzureCredential can mint tokens for Graph + Blob locally
az login
# (Your user account must have Storage Blob Data Contributor on the data storage account,
# and the SharePoint site must allow read for your user — or use Sites.Selected with a
# dedicated service principal via AZURE_CLIENT_ID/AZURE_CLIENT_SECRET env vars.)

# 6. Start the functions runtime
func start
```

The HTTP starter is exposed at `POST http://localhost:7071/api/start` and returns the standard Durable Functions status URLs.

### Required configuration values

| Setting | What it is | How to obtain |
|---|---|---|
| `SP_SITE_ID` | Graph composite site id (`hostname,site-guid,web-guid`) | `GET https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{site-name}` |
| `SP_LIST_ID` | Document-library list GUID | `GET https://graph.microsoft.com/v1.0/sites/{site-id}/lists` |
| `STORAGE_ACCOUNT_URL` | Blob endpoint of the destination data storage account | `https://<account>.blob.core.windows.net` |
| `BLOB_CONTAINER_NAME` | Destination container (default `source-pdfs`) | Created in setup script below |
| `SP_SITE_PREFIX` | Server-relative path stripped from `FileRef` to build the blob path | e.g. `/sites/QualityAlerts/` |
| `SP_FILE_EXTENSIONS` | Comma-separated allow-list (lowercase, no dot) | `pdf` |
| `SP_USE_CUSTOM_FIELDS` | `true` = full `ABB_Coll_*` schema with server-side `$filter`; `false` = standard fields only (PoC) | `false` |
| `MAX_CONCURRENT_DOWNLOADS` | Fan-out width | `5` |
| `BATCH_SIZE` | Files per `download_batch` activity | `200` |
| `GRAPH_BATCH_SIZE` | Items per Graph `$batch` call (max 20) | `20` |

---

## Deploying to Azure

The PoC was provisioned by hand for clarity; the steps below are the same commands executed against your subscription. Replace placeholders with your own values.

```bash
# Variables
SUBSCRIPTION_ID="<your-sub-id>"
RG="rg-docintell-poc"
LOCATION="westeurope"
FUNC="func-docintell-<unique>"            # globally unique
FUNC_STORAGE="stdocintellfunc<unique>"    # globally unique, 3–24 lowercase alphanumeric
DATA_STORAGE="stdocintellpoc<unique>"     # globally unique
DATA_CONTAINER="source-pdfs"
AI_NAME="appi-docintell-poc"

az account set --subscription "$SUBSCRIPTION_ID"
az group create -n "$RG" -l "$LOCATION"

# 1. Storage accounts
az storage account create -n "$FUNC_STORAGE" -g "$RG" -l "$LOCATION" --sku Standard_LRS --kind StorageV2
az storage account create -n "$DATA_STORAGE" -g "$RG" -l "$LOCATION" --sku Standard_LRS --kind StorageV2
az storage container create --account-name "$FUNC_STORAGE" -n app-package --auth-mode login
az storage container create --account-name "$DATA_STORAGE" -n "$DATA_CONTAINER" --auth-mode login

# 2. Application Insights
az monitor app-insights component create -g "$RG" --app "$AI_NAME" -l "$LOCATION" --kind web
AI_CONN=$(az monitor app-insights component show -g "$RG" -a "$AI_NAME" --query connectionString -o tsv)

# 3. Flex Consumption Function App
az functionapp create -g "$RG" -n "$FUNC" \
  --storage-account "$FUNC_STORAGE" \
  --flexconsumption-location "$LOCATION" \
  --runtime python --runtime-version 3.11 \
  --instance-memory 2048 --maximum-instance-count 20 \
  --deployment-container-image-name "" \
  --assign-identity '[system]'

FUNC_MSI=$(az functionapp identity show -g "$RG" -n "$FUNC" --query principalId -o tsv)

# 4. Grant the MSI data-plane access
FUNC_STORAGE_ID=$(az storage account show -g "$RG" -n "$FUNC_STORAGE" --query id -o tsv)
DATA_STORAGE_ID=$(az storage account show -g "$RG" -n "$DATA_STORAGE" --query id -o tsv)

# Durable Functions task hub needs all THREE on AzureWebJobsStorage
for ROLE in "Storage Blob Data Contributor" "Storage Queue Data Contributor" "Storage Table Data Contributor"; do
  az role assignment create --assignee-object-id "$FUNC_MSI" --assignee-principal-type ServicePrincipal \
    --role "$ROLE" --scope "$FUNC_STORAGE_ID"
done

az role assignment create --assignee-object-id "$FUNC_MSI" --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" --scope "$DATA_STORAGE_ID"

# 5. Grant Microsoft Graph application permission to the MSI
# Option A (PoC fallback): tenant-wide Sites.Read.All
GRAPH_SP_ID=$(az ad sp list --filter "appId eq '00000003-0000-0000-c000-000000000000'" --query "[0].id" -o tsv)
ROLE_ID=$(az ad sp show --id "$GRAPH_SP_ID" --query "appRoles[?value=='Sites.Read.All'].id | [0]" -o tsv)
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${FUNC_MSI}/appRoleAssignments" \
  --body "{\"principalId\":\"${FUNC_MSI}\",\"resourceId\":\"${GRAPH_SP_ID}\",\"appRoleId\":\"${ROLE_ID}\"}"
# Option B (production): Sites.Selected + per-site grant from Graph Explorer
# (the per-site grant requires the caller to hold Sites.FullControl.All — the az CLI
# delegated token typically does not, so do it from Graph Explorer or a dedicated SP)

# 6. App settings
az functionapp config appsettings set -g "$RG" -n "$FUNC" --settings \
  "APPLICATIONINSIGHTS_CONNECTION_STRING=$AI_CONN" \
  "SP_SITE_ID=<your-site-id>" \
  "SP_LIST_ID=<your-list-id>" \
  "STORAGE_ACCOUNT_URL=https://${DATA_STORAGE}.blob.core.windows.net" \
  "BLOB_CONTAINER_NAME=${DATA_CONTAINER}" \
  "SP_SITE_PREFIX=/sites/<your-site-name>/" \
  "SP_FILE_EXTENSIONS=pdf" \
  "SP_USE_CUSTOM_FIELDS=false" \
  "MAX_CONCURRENT_DOWNLOADS=5" \
  "BATCH_SIZE=200" \
  "GRAPH_BATCH_SIZE=20"

# 7. Deploy code (remote Oryx build)
cd function_app
func azure functionapp publish "$FUNC" --python --build remote
```

> ⚠️ `az functionapp create` auto-creates its own Application Insights component and overwrites `APPLICATIONINSIGHTS_CONNECTION_STRING`. Set the connection string **after** the create call (as shown above) and verify with `az functionapp config appsettings list`.

---

## Smoke test

```bash
HTTP_KEY=$(az functionapp function keys list -g "$RG" -n "$FUNC" \
  --function-name http_start_orchestration --query default -o tsv)

# Start the orchestration
curl -sS -X POST "https://${FUNC}.azurewebsites.net/api/start?code=${HTTP_KEY}"
# → returns { id, statusQueryGetUri, ... }

# Poll statusQueryGetUri until runtimeStatus == "Completed"
# Successful output looks like:
#   { "status": "completed", "total_files": N, "succeeded": N, "failed": 0, ... }

# Verify blobs landed
az storage blob list --account-name "$DATA_STORAGE" --container-name source-pdfs \
  --auth-mode login --query "[].name" -o tsv
```

---

## Security notes

- The Function App uses its **system-assigned Managed Identity** to obtain tokens for both Microsoft Graph and Azure Blob Storage at runtime — there are no application secrets to rotate or store.
- The HTTP starter is protected with a Functions function key. Treat it like a secret; rotate it with `az functionapp keys set` if exposed.
- The `local.settings.json` file is ignored by Git but does contain a real SharePoint site identifier in this codebase's history-free state — never check it in.
- For production, switch the Graph permission from `Sites.Read.All` to `Sites.Selected` and grant per-site access (see lesson #5 in [architecture-description.md](architecture-description.md)).

---

