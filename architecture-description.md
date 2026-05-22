# Architecture Description: Durable Functions + Graph API Pipeline

## AU-PCP Document Intelligence — 100K PDF Processing Pipeline

**Date:** May 2026  
**Customer:** ABB (OnePCP-DMS SharePoint Site — production target; PoC uses `QualityAlerts` site)  
**Workload:** 100K+ approved PDFs → OCR → LLM extraction → Snowflake  
**PoC Status:** ✅ SharePoint → Blob phase deployed and validated end-to-end (5/5 PDFs uploaded). OCR / LLM / Snowflake phases are next.

---

## 1. Executive Summary

This document describes the chosen production architecture for migrating ~100,000 approved PDF documents from SharePoint Online through Azure Document Intelligence (OCR) and Azure OpenAI (structured extraction) into Snowflake. The architecture uses **Azure Durable Functions on the Flex Consumption plan** with **Microsoft Graph API** as the SharePoint interaction layer.

Authentication uses the Function App's **system-assigned Managed Identity end-to-end** — to Microsoft Graph (for SharePoint), to Azure Blob Storage, and to the Durable Functions task hub. No certificates, no client secrets, no Key Vault required.

---

## 1A. PoC Implementation Status (May 2026)

The SharePoint-to-Blob phases (1–5) are deployed and verified in `westeurope`:

| Resource | Name | Notes |
|---|---|---|
| Resource group | `rg-docintell-poc` | West Europe |
| Function App | `func-docintell-poc` | Flex Consumption, Python 3.11, 2 GB memory, max 20 instances |
| Platform storage (AzureWebJobsStorage) | `stdocintellfunc` | Container `app-package` for one-deploy; hosts Durable task hub |
| Data storage | `stdocintellpoc` | Container `source-pdfs` — destination for PDFs + sidecar JSON |
| Application Insights | `func-docintell-poc` (auto-created) | Telemetry destination |
| SharePoint site (PoC) | `QualityAlerts` | 5 sample PDFs in `Shared Documents` |

**Deployed functions** (Python v2 model, blueprint-based):
- `http_start_orchestration` (HTTP starter, `POST /api/start`)
- `timer_start_orchestration` (CRON-based daily trigger)
- `sp_to_blob_orchestrator` (orchestrator)
- `list_and_filter_files`, `resolve_drive_id`, `download_batch` (activities)

**Identity & permissions (cert-free)**:
- Function App system-assigned MSI used for **all** outbound calls (`DefaultAzureCredential` → token for `https://graph.microsoft.com/.default` and for `https://storage.azure.com/.default`).
- MSI role assignments:
  - `Storage Blob Data Contributor` + `Storage Queue Data Contributor` + `Storage Table Data Contributor` on `stdocintellfunc` (Blob+Queue+Table are **all required** for the Durable task hub — missing Queue/Table causes orchestration start to return HTTP 500 with an empty body).
  - `Storage Blob Data Contributor` on `stdocintellpoc`.
  - Microsoft Graph application permission `Sites.Read.All` (PoC fallback — see lesson #2 below).

**PoC simplifications** controlled by env vars in `function_app/shared/config.py`:
- `SP_USE_CUSTOM_FIELDS=false` — the PoC SP site has no `ABB_Coll_*` columns, so the server-side `$filter` on `ABB_Coll_LifecycleStatus` is skipped and PDFs are selected client-side from `FileLeafRef` extension. The toggle flips back to the production schema with one app-setting change.
- `SP_FILE_EXTENSIONS=pdf` — client-side extension allow-list.
- `SP_SITE_PREFIX=/sites/QualityAlerts` — stripped from `FileRef` to derive the blob path. Falls back to a regex strip if unset.

---

## 2. Why This Architecture Was Selected

### 2.1 Customer's Current Solution (Baseline)

The customer operates a Python notebook (`sp-to-blob.ipynb`) using the `office365-rest-python-client` library with the following pattern:

```
OnlineSharepointConnector (cert auth)
  → lists.get_by_title(listName).items.select(fields).get_all(5000)
  → Filter in Python memory (FileSystemObjectType==0, ext==pdf, status==Approved)
  → ThreadPoolExecutor(max_workers=10)
    → download_and_upload() per file (with tenacity retry)
    → upload_blob(overwrite=True)
```

### 2.2 Problems with the Current Solution at 100K Scale

| Category | Risk | Detail |
|----------|------|--------|
| **Throttling** | HTTP 429 | SharePoint throttles based on resource units per time window. 10 concurrent threads significantly increases risk. Microsoft states: "Avoid making concurrent requests" |
| **Tenant Blocking** | Full app block | If sustained excessive traffic is detected, Microsoft can block the entire app or tenant — removal requires contacting support |
| **API Cost** | Higher resource units | `office365-rest-python-client` uses legacy SharePoint REST/CSOM. Microsoft explicitly states these "usually consume more resource units than Microsoft Graph APIs" |
| **Traffic Decoration** | No prioritization | The library may not properly set User-Agent and app ID headers that Microsoft uses for caller identification and prioritization |
| **List View Threshold** | 5,000 item limit | `get_all(5000)` retrieves ALL items then filters in Python memory — extremely inefficient for 100K+ items. Filters on unindexed columns will fail or get throttled |
| **No Delta Sync** | Full re-process | Every run re-downloads everything. No incremental change tracking |
| **Certificate on Disk** | Security risk | PEM cert written to filesystem (`selfsigncert.pem`) — operational burden and security concern |
| **Connection Churn** | Auth multiplication | Each worker thread creates a new `OnlineSharepointConnector`, multiplying auth handshakes and throttle risk |
| **No Checkpointing** | No resume** | If process fails at file 50,000 — no way to resume without re-downloading all 50K already-transferred files |
| **Single Machine** | No horizontal scale | ThreadPoolExecutor with 10 workers on one machine = hard ceiling on throughput |

### 2.3 Approaches Evaluated

| Approach | Pros | Cons | Verdict |
|----------|------|------|---------|
| **ADF + Graph API** | Visual monitoring, no-code, built-in retry | Graph `/children` doesn't support `$filter`, ADF expression language painful, complex pagination via Until loops, can't access custom SP metadata without extra calls | ❌ Too complex for this use case |
| **Customer code → Azure Functions (lift-and-shift)** | Reuses existing code, easy migration | **Same throttling problems** — actually WORSE because fan-out creates 200+ concurrent SP connections instead of 10 | ❌ Solves wrong problem |
| **Durable Functions + Graph API** | Orchestration + checkpointing, Graph's lower resource cost, server-side filtering, $batch API, delta queries, cert from Key Vault, throttle-aware | Requires rewriting SP interaction layer from CSOM to Graph SDK | ✅ **Selected** |

### 2.4 Why Durable Functions + Graph API Wins

1. **Graph API costs fewer resource units** — Microsoft's own recommendation for SharePoint access at scale
2. **$batch API** — 20 file downloads per single HTTP call → 5,000 calls instead of 100,000
3. **Server-side `$filter`** — Reduces dataset at source on `/lists/{id}/items` endpoint (unlike `/drive/root/children`); second filter applied client-side due to Graph's single-indexed-field limitation
4. **Delta query** — After initial run, only process new/changed files
5. **Durable Functions checkpointing** — Knows exactly which files succeeded/failed; resume from failure point
6. **Flex Consumption plan** — No timeout limit, auto-scale to 1000 instances, pay-per-execution
7. **Throttle control** — Limit concurrency to 5 activities (not 200+ threads), respect `Retry-After` headers
8. **Cert-free Managed Identity auth** — Function App MSI tokens both Graph and Storage. No certificate on disk, no secret rotation, no Key Vault dependency. Production uses Graph `Sites.Selected` (per-site grant); the PoC falls back to `Sites.Read.All` because the per-site grant requires `Sites.FullControl.All` on the calling principal.

---

## 3. Step-by-Step Workflow

### Phase 1: Trigger & Initialization

```
Timer Trigger (Daily 02:00 UTC)   OR   HTTP starter (POST /api/start, function key)
  └── Starts Orchestrator Function (sp_to_blob_orchestrator)
        └── DefaultAzureCredential → MSI token for https://graph.microsoft.com/.default
        └── DefaultAzureCredential → MSI token for https://storage.azure.com/.default
```

- **Timer Trigger** (`timer_start_orchestration`) fires daily on a CRON expression; an HTTP starter (`http_start_orchestration`) is also available for ad-hoc runs.
- Orchestrator function is the central coordinator — manages state, checkpoints, and retry logic.
- No secrets are read at startup. The Function App's system-assigned Managed Identity provides bearer tokens for both Graph and Storage at call time.

### Phase 2: List & Filter Files (Graph API — Hybrid Filter)

```
Orchestrator
  └── Activity: list_and_filter_files
        └── GET /sites/{siteId}/lists/{listId}/items
              ?$expand=fields,driveItem      ← driveItem.id is needed for download
                       ($select on fields when SP_USE_CUSTOM_FIELDS=true)
              &$filter=fields/ABB_Coll_LifecycleStatus eq 'Approved'   ← prod only
              &$top=5000
        └── Follow @odata.nextLink for pagination (~6-7 pages × 5K = ~30K approved items)
        └── Client-side filter: file extension allow-list via FileLeafRef
        └── Returns list of { item_id (= driveItem.id), file_ref, file_leaf_ref, metadata }
        └── Optional: Use deltaLink for subsequent runs (only fetch changes)
```

> ⚠️ **Implementation gotcha (verified during PoC):** the items endpoint returns a SharePoint **list-item id** by default, which is *not* accepted by `/drives/{driveId}/items/{itemId}/content`. You must `$expand=driveItem` and use `driveItem.id` for the subsequent download. The PoC activity emits this real drive-item id.
>
> **PoC mode (`SP_USE_CUSTOM_FIELDS=false`)**: the server-side `$filter` on `ABB_Coll_LifecycleStatus` is omitted (the PoC site has no such column) and only the client-side extension filter applies.

**Why hybrid filter (server-side + client-side)?**

Microsoft Graph API documentation states: *"When filtering on indexed fields, the service can only filter one indexed field at a time."* Compound filters on two indexed columns (e.g., `field1 eq 'x' and field2 eq 'y'`) are not reliably supported for lists with >5,000 items. The pragmatic approach:

1. **Server-side `$filter`** on the most selective indexed column (`ABB_Coll_LifecycleStatus eq 'Approved'`) — reduces the 100K+ total items to ~30K approved items at the API level
2. **Client-side filter** for `NextECM_Mig_File_Ext == 'pdf'` — a trivial Python `if` on the paginated results

This is one extra line of code and eliminates all uncertainty about compound filter support.

**Key improvement over current solution:**
- Server-side filtering reduces dataset by ~70% at source (only approved items returned)
- Client-side filter is applied during pagination (no memory bloat — filter as you page)
- Custom metadata fields (all 20 ABB_Coll_* columns) available via `$expand=fields`
- Never pulls 100K items into memory — pages through ~30K items with 5K per page
- Delta query support: first run gets all items + deltaLink; subsequent runs use deltaLink to get only changes

**API calls:** ~6-7 paginated requests for ~30K approved items (5,000 items per page)

### Phase 3: Fan-Out File Downloads (Graph $batch API)

```
Orchestrator
  └── Chunks filtered items into batches of 200 files
  └── Dispatches max 5 concurrent Activity functions
        └── Activity: DownloadBatch
              └── Groups 200 files into 10 $batch requests (20 downloads per batch)
              └── POST /$batch
                    { requests: [
                        { id: "1", method: "GET", url: "/sites/{id}/drive/items/{itemId}/content" },
                        { id: "2", method: "GET", url: "/sites/{id}/drive/items/{itemId}/content" },
                        ... (20 per batch call)
                    ]}
              └── Upload each downloaded file to Blob Storage
              └── Respect RateLimit-Remaining and Retry-After headers
              └── Report success/failure per file back to orchestrator
```

**Throttle control strategy:**
- Max 5 concurrent download activities (not 200+ threads)
- Each activity makes 10 $batch calls (20 downloads/call = 200 files)
- Effective max: 5 activities × 1 $batch call at a time = **5 concurrent Graph HTTP requests**
- Each $batch request counts as 20 individual requests for throttle limits, but only 1 HTTP connection
- RateLimit headers (`RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset`) guide pacing
- On HTTP 429: respect `Retry-After` header, Durable Functions retry policy handles backoff

**API calls for 100K files:** 100,000 / 20 per batch = **5,000 HTTP calls** (vs. 100,000 individual calls today)

### Phase 4: Upload to Blob Storage (PDF + sidecar JSON)

```
Each download_batch Activity
  └── For each downloaded file:
        └── container_client.get_blob_client(blob_name).upload_blob(
              data=pdf_bytes, overwrite=True)
        └── container_client.get_blob_client(f"{blob_name}.metadata.json").upload_blob(
              data=json.dumps(metadata),
              overwrite=True,
              content_settings=ContentSettings(content_type="application/json"))
```

- Blob layout: `source-pdfs/{relative_sp_path}/{filename}.pdf` plus a sibling `{filename}.pdf.metadata.json` carrying the full SP item fields. Sidecar JSON is preferred over Azure blob metadata because blob metadata has a 8 KB hard limit and ASCII-only keys/values — SP custom columns often violate both.
- `SP_SITE_PREFIX` (e.g. `/sites/QualityAlerts`) is stripped from `FileRef` to derive the relative path; PoC successfully produced `Shared Documents/<filename>.pdf` blobs.
- Managed Identity authentication to Storage Account (`Storage Blob Data Contributor` on the data account).

> ⚠️ **Implementation gotcha (verified during PoC):** `azure-storage-blob`'s `upload_blob(content_settings=…)` requires a typed `ContentSettings` object. Passing a `dict` raises `'dict' object has no attribute 'cache_control'` deep inside the SDK. Always import `from azure.storage.blob import ContentSettings`.

### Phase 5: Fan-In & Status Report

```
Orchestrator
  └── task_all() waits for all DownloadBatch activities
  └── Collects results: {success_count, fail_count, failed_files[]}
  └── If failures > threshold: raise alert (Activity: SendAlert)
  └── Checkpoint: save deltaLink for next run
```

- Durable Functions automatically tracks which activities completed
- If orchestrator crashes mid-way: replays from last checkpoint (all completed downloads are NOT re-done)
- Failed files are logged with error details for retry or manual investigation

### Phase 6: Document Intelligence Batch Processing

```
Orchestrator
  └── Activity: TriggerDocIntelBatches
        └── Chunks 100K blobs into 10 batches (max 10K docs/batch)
        └── POST /documentModels/prebuilt-read:analyzeBatch
              { azureBlobSource: { containerUrl: "...", prefix: "batch-1/" } }
        └── Poll for completion (analyzeResult available)
        └── Output: structured JSON per document → doc-intel-output/ container
```

- Uses Doc Intelligence Batch API (api-version 2024-11-30 GA)
- Managed Identity authentication (no keys)
- 10 batches × 10K docs = 100K docs processed
- Batches can run in parallel (10 concurrent batch operations)
- Output: JSON with extracted text, tables, key-value pairs per page

### Phase 7: LLM Structured Extraction (Azure OpenAI Batch)

```
Orchestrator
  └── Activity: TriggerLLMBatch
        └── Prepares JSONL file with prompts (one per document)
        └── POST /batches (Azure OpenAI Batch API)
              { input_file_id: "...", endpoint: "/chat/completions", model: "gpt-4o" }
        └── Poll for completion
        └── Output: structured JSON → llm-output/ container
```

- Azure OpenAI Batch API provides 50% discount vs. real-time calls
- System prompt instructs extraction of specific fields from OCR text
- Output: structured JSON matching Snowflake table schema
- Batch API has 24-hour SLA for completion

### Phase 8: Snowpipe Ingestion (Event-Driven)

```
Blob Storage (llm-output/)
  └── Event Grid subscription: Microsoft.Storage.BlobCreated
        └── Storage Queue notification
              └── Snowflake Notification Integration
                    └── Snowpipe: COPY INTO target_table
                          FROM @azure_stage/llm-output/
                          FILE_FORMAT = (TYPE = JSON)
```

- Fully event-driven — no polling or scheduling needed
- Each blob creation triggers automatic ingestion
- Snowpipe's new pricing (Dec 2025): 0.0037 credits per GB ingested
- No compute warehouse needed (serverless)

---

## 4. Architecture Comparison vs. Customer's Current Code

| Aspect | Customer's Current (`sp-to-blob.ipynb`) | Proposed (Durable Functions + Graph) |
|--------|----------------------------------------|--------------------------------------|
| **SP API** | `office365-rest-python-client` (CSOM/REST) | Microsoft Graph API v1.0 |
| **Resource cost per call** | HIGH (legacy API) | LOW (Microsoft's recommended path) |
| **HTTP calls for 100K files** | 100,000 individual calls | ~5,000 ($batch groups of 20) |
| **Filtering** | In-memory (pull all, filter in Python) | Hybrid: server-side `$filter` on 1 indexed column + lightweight client-side filter |
| **Incremental sync** | ❌ Full re-process every run | ✅ Delta query (only changes after first run) |
| **Concurrency** | 10 threads, 1 machine | 5 activities × 20 batch = controlled parallelism |
| **Throttle risk** | HIGH (10 concurrent CSOM calls) | LOW (5 HTTP calls, each carrying 20 requests) |
| **Certificate** | PEM file on disk | Key Vault (Managed Identity) |
| **Resume on failure** | ❌ Re-download everything | ✅ Checkpoint per activity (only retry failed batches) |
| **Cost visibility** | None (runs on VM/notebook) | Application Insights + Durable Functions Monitor |
| **Scale ceiling** | ~10 parallel downloads | Up to 1000 instances (Flex Consumption) |
| **Timeout** | None (notebook) / machine uptime | No limit (Flex Consumption plan) |

---

## 5. Remaining Caveats & Mitigations

### 5.1 Caveats That Still Apply

| Caveat | Detail | Mitigation |
|--------|--------|------------|
| **SharePoint throttling still exists** | Graph API reduces but doesn't eliminate throttle risk. SharePoint applies throttling per-app per-tenant regardless of API choice | Max 5 concurrent activities; respect `RateLimit-Remaining` headers; implement exponential backoff via Durable Functions retry policies |
| **Graph $batch limits** | Each request inside a $batch is evaluated individually against throttle limits. A 429 on one request in a batch doesn't fail the whole batch, but that request returns error | Parse individual responses in batch; retry only failed items |
| **$filter single-indexed-field limitation** | Microsoft Graph docs state: *"the service can only filter one indexed field at a time"* for lists >5000 items. Compound `$filter` on two indexed columns is unreliable. Additionally, the column used in `$filter` must be indexed. | Use hybrid approach: server-side `$filter` on ONE indexed column (`ABB_Coll_LifecycleStatus`), client-side filter for second condition (`NextECM_Mig_File_Ext == 'pdf'`). Verify `ABB_Coll_LifecycleStatus` is indexed in SP admin. |
| **Delta query staleness** | Delta queries have a retention window (varies, typically 30 days). If no sync runs for >30 days, must do full re-sync | Schedule daily runs; monitor deltaLink validity |
| **Durable Functions history table growth** | At 100K activities, the orchestration history table in Azure Storage grows large. This can slow down orchestrator replay | Chunk into sub-orchestrators (each handles 10K files); purge completed orchestration history |
| **Rewrite required** | Customer's existing Python code cannot be lifted-and-shifted as-is. The SP interaction layer must be rewritten to use `msgraph-sdk-python` or raw Graph HTTP calls | Business logic (filtering rules, blob naming, metadata mapping) transfers directly; only the download/upload mechanism changes |
| **Graph download URL differences** | Graph returns file content via `/drive/items/{id}/content` (302 redirect to temp URL). This is different from SP REST's `get_file_by_server_relative_url()` | Use driveItem ID from list items response; handle 302 redirect in download logic |
| **Batch Read API doesn't use commitment tiers** | Azure billing confirmed that Doc Intelligence Batch API does NOT honor commitment tier pricing — always billed at pay-as-you-go rates | Factor pay-as-you-go pricing ($1.50/1K pages) into cost estimates |

### 5.2 Caveats That Are Eliminated

| Former Risk | How It's Resolved |
|-------------|-------------------|
| Connection churn (new SP connection per thread) | Single Graph API auth token shared across batch calls; no per-file connection setup |
| No idempotency tracking | Durable Functions orchestration state tracks exactly which batches completed |
| Certificate on disk | Certificate stored in Key Vault; retrieved via Managed Identity at runtime |
| Single-machine bottleneck | Flex Consumption scales to 1000 instances |
| No timeout awareness | No execution timeout on Flex Consumption plan |
| 100K individual HTTP calls | $batch reduces to ~5,000 HTTP calls |
| In-memory filtering of entire list | Hybrid filter: server-side `$filter` reduces dataset ~70% at source; lightweight client-side filter during pagination |

---

## 6. Estimated Costs (2026 Pricing)

### Assumptions

- **100,000 PDF files**, average 5 pages per document = 500,000 pages
- **Average file size:** 2 MB → Total data: ~200 GB
- **LLM tokens per document:** ~2,000 input tokens (OCR text), ~500 output tokens (structured JSON)
- **Runs:** 1 initial full load + daily incremental (delta query, ~500 new files/day)
- **Region:** Australia East
- **Snowflake credit:** ~$3.00 per credit (Enterprise tier)

### 6.1 Initial Full Load (One-Time)

| Service | Calculation | Cost |
|---------|-------------|------|
| **Azure Functions (Flex Consumption)** | | |
| — Executions | 500 batch activities + 7 list activities + 10 orchestrator replays = ~517 executions | Free (within 250K free grant) |
| — Compute (GB-seconds) | 530 activities × avg 120s × 2 GB memory = 127,200 GB-s | (127,200 - 100,000 free) × $0.000016 = **$0.43** |
| — On-demand baseline | 530 activities × avg 120s × 2 GB = 127,200 GB-s | 127,200 × $0.000004 = **$0.51** |
| **Azure Blob Storage (Hot LRS)** | | |
| — Storage | 200 GB × $0.0184/GB/month | **$3.68/month** |
| — Write operations | 100K uploads = 100K ops | 100K / 1M × $5.00 = **$0.50** |
| — Read operations (Doc Intel reads) | 100K reads | 100K / 1M × $0.40 = **$0.04** |
| **Azure Document Intelligence** | | |
| — Read model (Batch API) | 500,000 pages × $1.50/1,000 pages | **$750.00** |
| **Azure OpenAI (GPT-4o Batch API)** | | |
| — Input tokens | 100K docs × 2,000 tokens = 200M tokens × $1.25/1M | **$250.00** |
| — Output tokens | 100K docs × 500 tokens = 50M tokens × $5.00/1M | **$250.00** |
| **Snowpipe Ingestion** | | |
| — Per-GB pricing | ~5 GB JSON output × 0.0037 credits/GB × $3.00/credit | **$0.06** |
| **Azure Key Vault** | | |
| — Secret operations | ~1,000 reads (cert + secrets) | 1K / 10K × $0.03 = **$0.003** |
| **Azure Storage (Durable Functions state)** | | |
| — Queue + Table operations | ~500K operations (orchestration history) | ~**$0.20** |
| | | |
| **TOTAL (Initial Full Load)** | | **~$1,255** |

### 6.2 Daily Incremental Run (Ongoing)

| Service | Calculation | Cost/Day |
|---------|-------------|----------|
| **Azure Functions** | 5 activities × 60s × 2 GB = 600 GB-s | **Free** (within monthly grant) |
| **Document Intelligence** | 500 docs × 5 pages = 2,500 pages | $1.50/1K × 2.5 = **$3.75** |
| **Azure OpenAI (Batch)** | 500 docs × 2K input + 500 output tokens | Input: $0.63 + Output: $1.25 = **$1.88** |
| **Blob Storage** | 1 GB new data + 500 write ops | **~$0.02** |
| **Snowpipe** | ~0.05 GB × 0.0037 × $3.00 | **$0.001** |
| | | |
| **TOTAL (Daily Incremental)** | | **~$5.65/day** |
| **Monthly Incremental** | | **~$170/month** |

### 6.3 Monthly Total (After Initial Load)

| Component | Monthly Cost |
|-----------|-------------|
| Blob Storage (200 GB hot) | $3.68 |
| Azure Functions compute | ~$1 (mostly free tier) |
| Document Intelligence (incremental) | ~$112 |
| Azure OpenAI (incremental) | ~$56 |
| Snowpipe | ~$0.03 |
| Key Vault | ~$0.01 |
| Application Insights (logs) | ~$5 |
| **Monthly Total (steady state)** | **~$178/month** |

### 6.4 Cost Comparison

| Approach | Initial Load | Monthly (steady state) |
|----------|-------------|----------------------|
| **Proposed (Durable Functions + Graph)** | ~$1,255 | ~$178/month |
| **ADF + Graph API** | ~$1,280 (+ $25 ADF activities) | ~$200/month (ADF overhead) |
| **Customer's current code on VM** | ~$1,200 (+ VM cost ~$150/mo) | ~$330/month (VM always-on + no delta) |

> **Note:** The dominant cost is Document Intelligence ($750) and Azure OpenAI ($500) for the initial load. The compute layer (Functions vs. ADF vs. VM) is a minor cost factor. The real savings come from **delta query** — after the first load, you only process new/changed files, reducing Doc Intelligence + OpenAI costs by ~95%.

---

## 7. Timeline Estimate

| Phase | Duration | Notes |
|-------|----------|-------|
| Phase 2: List & Filter (Graph API) | ~1 minute | ~7 paginated requests (server-side $filter on 1 indexed col) + client-side PDF filter |
| Phase 3: Download (5 activities × $batch) | ~4-6 hours | Throttle-aware pacing; 5,000 HTTP calls |
| Phase 4: Upload to Blob | Included in Phase 3 | Upload happens inline with download |
| Phase 6: Doc Intelligence Batch | ~3-5 hours | 10 parallel batches of 10K docs |
| Phase 7: LLM Batch (Azure OpenAI) | ~4-8 hours | 24-hour SLA; usually completes in 4-8h |
| Phase 8: Snowpipe ingestion | ~minutes | Event-driven, near real-time |
| **Total end-to-end** | **~12-18 hours** | Suitable for overnight batch |

---

## 8. Technology Stack Summary

| Layer | Technology | Version/SKU |
|-------|-----------|-------------|
| Orchestration | Azure Durable Functions (Python v2 model) | Flex Consumption, Python 3.11, ext bundle `[4.0.0, 5.0.0)` |
| SharePoint Access | Microsoft Graph API (raw HTTP via `httpx`) | v1.0 (GA) |
| Identity | Function App system-assigned Managed Identity | `DefaultAzureCredential` for Graph + Storage (no certs, no KV) |
| Storage | Azure Blob Storage | Hot tier, LRS, identity-based connection |
| OCR | Azure Document Intelligence | S0, Batch API (2024-11-30 GA) |
| LLM | Azure OpenAI Service | GPT-4o, Batch API |
| Ingestion | Snowpipe | Credit-per-GB model (Dec 2025) |
| Data Warehouse | Snowflake | Enterprise tier |
| Monitoring | Application Insights (auto-created during `az functionapp create`) | OpenTelemetry distro |

---

## 9. Security & Compliance

| Concern | Implementation |
|---------|---------------|
| SharePoint authentication | Function App MSI → Microsoft Graph (app permission `Sites.Selected` in production; `Sites.Read.All` in PoC). No certificate, no client secret. |
| Authentication to Azure services | System-assigned Managed Identity for Blob + Queue + Table data planes |
| Network isolation | Flex Consumption supports VNet integration |
| Data encryption at rest | Blob Storage: Azure-managed keys (default) |
| Data encryption in transit | TLS 1.2+ for all API calls |
| Access control | RBAC on all resources; principle of least privilege |
| Audit logging | Application Insights + Azure Monitor |
| Throttle compliance | Respects Microsoft's RateLimit headers; max 5 concurrent activities |
| Secret sprawl | Zero application secrets in code, settings, or Key Vault for the SP→Blob phase |

---

## 10. What Transfers from Customer's Current Code

| Component from `sp-to-blob.ipynb` | Reuse in New Architecture |
|-----------------------------------|--------------------------|
| Filtering logic (`FileSystemObjectType==0`, `ext in {pdf}`, `status==Approved`) | `status==Approved` → Graph `$filter`; `ext==pdf` → client-side filter during pagination |
| `select_fields` list (20 ABB_Coll_* columns) | Translates to `$expand=fields($select=...)` |
| `_build_blob_name()` path logic | Reused as-is in download activity |
| `upload_blob(name=blob_name, data=file_buffer, overwrite=True)` | Reused as-is (same Azure Storage SDK) |
| `tenacity` retry decorator | Replaced by Durable Functions native retry policy |
| `ThreadPoolExecutor(max_workers=10)` | Replaced by fan-out/fan-in (5 concurrent activities) |
| `OnlineSharepointConnector` + `connect_via_cert()` | Replaced by Function App MSI + `DefaultAzureCredential` → raw Graph HTTP (`httpx`). No certificate, no Key Vault. |
| `get_file_by_server_relative_url()` | Replaced by `GET /drives/{driveId}/items/{driveItemId}/content` (note: drive-item id, not list-item id — expand `driveItem` to obtain it) |
| `get_all(5000, print_progress)` | Replaced by `$top=5000` + `@odata.nextLink` pagination |

---

## 11. Risks & Recommendations

### High Priority
1. **Verify SP column index** — `$filter` on `/lists/{id}/items` requires the filtered column to be indexed for lists >5000 items. Confirm `ABB_Coll_LifecycleStatus` is indexed (this is the server-side filter column). `NextECM_Mig_File_Ext` is filtered client-side so indexing is optional but recommended for future flexibility.
2. **Graph API permissions** — Production target is `Sites.Selected` (admin grants per-site access on `/sites/{id}/permissions`). Note: the per-site grant call itself requires the *calling principal* to hold `Sites.FullControl.All`, which the default Azure CLI delegated token does not include — grant from Graph Explorer or a dedicated service principal. PoC currently uses tenant-wide `Sites.Read.All`.
3. **Durable Functions history purge** — Implement automatic purge of completed orchestration instances to prevent storage bloat.
4. **Required MSI roles on AzureWebJobsStorage** — the Durable task hub requires **Blob + Queue + Table** Data Contributor on the platform storage account. Missing Queue/Table results in HTTP 500 with an empty body on orchestration start (no useful error in logs).

### Medium Priority
4. **Sub-orchestrator pattern** — For 100K files, use sub-orchestrators (each handles 10K files) to avoid oversized orchestration history.
5. **Dead-letter handling** — Permanently failed files (corrupt, access denied) should be routed to a dead-letter blob container with alert notification.
6. **Monitoring dashboard** — Build Application Insights dashboard showing: files processed, throttle events (429s), batch completion %, estimated time remaining.

### Low Priority
7. **Graph Data Connect** — For future consideration if volumes exceed 1M files. Microsoft explicitly recommends Graph Data Connect for "extracting large volumes of data without being subject to throttling limits."
8. **Blob lifecycle policy** — Archive `source-pdfs/` to Cool tier after 30 days; archive `doc-intel-output/` after 7 days.

---

## 12. Lessons Learned from PoC Deployment

Documented here so the production rollout doesn't re-discover them.

| # | Lesson | Detail |
|---|--------|--------|
| 1 | **`ContentSettings` must be typed** | `azure-storage-blob.upload_blob(content_settings=...)` requires the `ContentSettings` class — a `dict` raises `'dict' object has no attribute 'cache_control'` deep in the SDK. `from azure.storage.blob import ContentSettings`. |
| 2 | **Use `driveItem.id`, not list-item id** | `/sites/{id}/lists/{id}/items` returns the SP list-item id by default, but `/drives/{driveId}/items/{itemId}/content` only accepts the drive-item id. Always `$expand=driveItem` and read `driveItem.id`. |
| 3 | **Durable needs Blob+Queue+Table on AzureWebJobsStorage** | First orchestration start returned opaque HTTP 500 with empty body. Root cause: MSI only had `Storage Blob Data Contributor`. Granting Queue + Table fixed it. |
| 4 | **`az functionapp create` auto-creates an App Insights component** | And it overwrites `APPLICATIONINSIGHTS_CONNECTION_STRING` *after* any explicit `appsettings set`. Either let the auto-created one win and query that for telemetry, or set the connection string *after* `functionapp create` and confirm. |
| 5 | **`Sites.Selected` per-site grant requires `Sites.FullControl.All`** | The `az rest POST /sites/{id}/permissions` call returns the unhelpful `"Invalid request"` when the caller lacks that permission. Use Graph Explorer or a dedicated SP, or fall back to tenant-wide `Sites.Read.All` for PoC. |
| 6 | **`SP_SITE_PREFIX` should be configurable** | Hard-coding `/sites/OnePCP-DMS/` breaks any other site. The PoC config exposes `SP_SITE_PREFIX` (e.g. `/sites/QualityAlerts`) with a regex-strip fallback. |
| 7 | **PoC-friendly schema toggle** | The production design assumes 20 `ABB_Coll_*` columns. The `SP_USE_CUSTOM_FIELDS` env toggle short-circuits the server-side `$filter` and field selection so the same code runs against arbitrary document libraries during early testing. |
| 8 | **Sidecar JSON beats blob metadata** | Azure blob metadata caps at ~8 KB total with ASCII-only keys/values. SP custom columns routinely violate both. Writing `{name}.metadata.json` next to the blob keeps the full payload and survives downstream consumers. |
