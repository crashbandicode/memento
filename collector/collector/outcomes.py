"""Typed upload results shared by the HTTP client and durable queue."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class UploadOutcomeState(str, Enum):
    """Disposition of one upload attempt."""

    SUCCESS = "success"
    TRANSIENT_RETRY = "transient_retry"
    AUTHENTICATION_BLOCKED = "authentication_blocked"
    SOURCE_REPAIR_REQUIRED = "source_repair_required"
    PERMANENT_QUARANTINE = "permanent_quarantine"


class SourceRepairAction(str, Enum):
    """Collector-owned repairs that can safely replace a rejected revision."""

    DELTA_BASE_CONFLICT = "delta_base_conflict"
    REBUILD_BOUNDED_DELTA = "rebuild_bounded_delta"


@dataclass(frozen=True)
class UploadOutcome:
    """Structured upload result with safe, persistable diagnostics."""

    state: UploadOutcomeState
    diagnostic: str = ""
    diagnostic_code: str = ""
    http_status: int | None = None
    repair_action: SourceRepairAction | None = None
    expected_hash: str | None = None
    expected_offset: int = 0

    @property
    def succeeded(self) -> bool:
        return self.state is UploadOutcomeState.SUCCESS

    def __bool__(self) -> bool:
        """Retain simple success assertions without collapsing failure states."""

        return self.succeeded

    @classmethod
    def success(cls, diagnostic: str = "") -> "UploadOutcome":
        return cls(UploadOutcomeState.SUCCESS, diagnostic=diagnostic)

    @classmethod
    def transient(
        cls,
        diagnostic: str,
        *,
        diagnostic_code: str = "transient_failure",
        http_status: int | None = None,
    ) -> "UploadOutcome":
        return cls(
            UploadOutcomeState.TRANSIENT_RETRY,
            diagnostic=diagnostic,
            diagnostic_code=diagnostic_code,
            http_status=http_status,
        )

    @classmethod
    def authentication_blocked(
        cls,
        diagnostic: str,
        *,
        http_status: int,
    ) -> "UploadOutcome":
        return cls(
            UploadOutcomeState.AUTHENTICATION_BLOCKED,
            diagnostic=diagnostic,
            diagnostic_code="authentication_rejected",
            http_status=http_status,
        )

    @classmethod
    def source_repair(
        cls,
        diagnostic: str,
        *,
        diagnostic_code: str,
        http_status: int | None = None,
        repair_action: SourceRepairAction | None = None,
        expected_hash: str | None = None,
        expected_offset: int = 0,
    ) -> "UploadOutcome":
        return cls(
            UploadOutcomeState.SOURCE_REPAIR_REQUIRED,
            diagnostic=diagnostic,
            diagnostic_code=diagnostic_code,
            http_status=http_status,
            repair_action=repair_action,
            expected_hash=expected_hash,
            expected_offset=max(0, int(expected_offset or 0)),
        )

    @classmethod
    def quarantine(
        cls,
        diagnostic: str,
        *,
        diagnostic_code: str = "permanent_failure",
        http_status: int | None = None,
    ) -> "UploadOutcome":
        return cls(
            UploadOutcomeState.PERMANENT_QUARANTINE,
            diagnostic=diagnostic,
            diagnostic_code=diagnostic_code,
            http_status=http_status,
        )
