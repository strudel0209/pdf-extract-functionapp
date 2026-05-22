"""Configuration loaded from environment variables / App Settings."""
import os


SP_SITE_ID = os.environ["SP_SITE_ID"]
SP_LIST_ID = os.environ["SP_LIST_ID"]

STORAGE_ACCOUNT_URL = os.environ["STORAGE_ACCOUNT_URL"]
BLOB_CONTAINER_NAME = os.environ.get("BLOB_CONTAINER_NAME", "source-pdfs")

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "5"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "200"))
GRAPH_BATCH_SIZE = int(os.environ.get("GRAPH_BATCH_SIZE", "20"))

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Server-relative path prefix to strip from FileRef when building blob names.
# Example: "/sites/QualityAlerts/" -> blob "Shared Documents/2024/doc.pdf".
# If unset, defaults to stripping the first two path segments of FileRef.
SP_SITE_PREFIX = os.environ.get("SP_SITE_PREFIX", "")

# File extensions to include (lowercase, no dot). Comma-separated env var.
SP_FILE_EXTENSIONS = [
    ext.strip().lower().lstrip(".")
    for ext in os.environ.get("SP_FILE_EXTENSIONS", "pdf").split(",")
    if ext.strip()
]

# Toggle the rich customer schema (ABB_Coll_* / NextECM_Mig_*). When False
# (PoC default), use only standard SharePoint columns and filter client-side.
SP_USE_CUSTOM_FIELDS = os.environ.get("SP_USE_CUSTOM_FIELDS", "false").lower() == "true"

# Minimal field set guaranteed to exist on any SharePoint document library.
_SP_STANDARD_FIELDS = [
    "FileRef", "FileDirRef", "FileLeafRef", "FileSystemObjectType",
    "Title", "Created", "Modified", "Author", "Editor",
]

# Full customer schema from sp-to-blob.ipynb (used only when SP_USE_CUSTOM_FIELDS=true).
_SP_CUSTOM_FIELDS = [
    "FileRef", "FileDirRef", "FileLeafRef", "FileSystemObjectType",
    "ABB_Coll_LifecycleStatus", "NextECM_Mig_File_Ext",
    "ABB_Coll_ApprovalDate", "ABB_Coll_ApprovedByPerson",
    "ABB_Coll_DocumentId", "ABB_Coll_DocumentKind",
    "ABB_Coll_DocumentPartID", "ABB_Coll_DocumentRevisionId",
    "ABB_Coll_LanguageCode", "DMS_LanguageCode",
    "ABB_Coll_Lifecycle", "ABB_Coll_OwningOrganization",
    "ABB_Coll_PreparedByPerson", "ABB_Coll_PreparedDate",
    "ABB_Coll_ResponsibleDepartment", "ABB_Coll_SecurityLevel",
    "SupplementaryTitle", "DMS_TAG", "Title", "ABB_Coll_TitleEnglish",
]

SP_SELECT_FIELDS = _SP_CUSTOM_FIELDS if SP_USE_CUSTOM_FIELDS else _SP_STANDARD_FIELDS
