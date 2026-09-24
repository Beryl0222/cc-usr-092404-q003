"""跨境义诊器材交接：版本边界、原子导入、守恒台账与最终清单。"""

from .contracts import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_LEGACY_VERSIONS,
    AWAITING_UPGRADE,
    KNOWN_UNITS,
    DomainRecord,
    HandoffRecord,
    SealedDocument,
    document_from_dict,
    load_document,
    load_record,
    migrate_v1_to_v2,
    seal_document,
    seal_text,
)
from .errors import (
    ContractError,
    MalformedDocumentError,
    MigrationRequiredError,
    MissionKitError,
    UnsupportedVersionError,
)
from .ledger import (
    CalibrationFrozenError,
    ContradictionError,
    CustodyGapError,
    InventoryLedger,
    LedgerError,
    Node,
)
from .report import build_final_report, render_text
from .service import ImportRejectedError, MissionService
from .store import StateStore

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "SUPPORTED_LEGACY_VERSIONS",
    "AWAITING_UPGRADE",
    "KNOWN_UNITS",
    "DomainRecord",
    "HandoffRecord",
    "SealedDocument",
    "document_from_dict",
    "load_document",
    "load_record",
    "migrate_v1_to_v2",
    "seal_document",
    "seal_text",
    "CalibrationFrozenError",
    "ContractError",
    "MalformedDocumentError",
    "MigrationRequiredError",
    "MissionKitError",
    "UnsupportedVersionError",
    "ContradictionError",
    "CustodyGapError",
    "InventoryLedger",
    "LedgerError",
    "Node",
    "build_final_report",
    "render_text",
    "ImportRejectedError",
    "MissionService",
    "StateStore",
]
