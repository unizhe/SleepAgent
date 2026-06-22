from __future__ import annotations

import json
from datetime import datetime, timezone

from sleepagent.schemas import (
    AgentEvent,
    AgentEventType,
    Artifact,
    ArtifactExportFormat,
    ArtifactExportRequest,
    ArtifactExportResult,
    ArtifactReviseRequest,
    ArtifactStatus,
    ArtifactType,
    ArtifactVersion,
    SafetyReviewStatus,
)
from sleepagent.services.artifact_repository import (
    ArtifactNotFoundError,
    ArtifactRepository,
)
from sleepagent.services.task_repository import TaskRepository


class ArtifactServiceError(RuntimeError):
    """Base error for ArtifactService operations."""


class ArtifactExportBlockedError(ArtifactServiceError):
    """Raised when an artifact needs confirmation before export."""


class ArtifactSafetyBlockedError(ArtifactServiceError):
    """Raised when the current artifact version fails safety review."""


class ArtifactService:
    def __init__(
        self,
        *,
        artifact_repository: ArtifactRepository,
        task_repository: TaskRepository,
    ) -> None:
        self.artifact_repository = artifact_repository
        self.task_repository = task_repository

    def list_task_artifacts(self, task_id: str) -> list[Artifact]:
        return self.artifact_repository.list_artifacts(task_id)

    def get_artifact(self, artifact_id: str) -> Artifact:
        return self.artifact_repository.get_artifact(artifact_id)

    def revise_artifact(
        self,
        artifact_id: str,
        request: ArtifactReviseRequest,
    ) -> Artifact:
        artifact, version = self.artifact_repository.revise_artifact(
            artifact_id,
            content=request.content,
            revision_instruction=request.revision_instruction,
            created_by=request.created_by,
        )
        self._append_artifact_event(
            artifact,
            title="Artifact 已修改",
            message=f"已生成 {artifact.title} 的第 {version.version_number} 个版本。",
            payload={
                "artifact": artifact.model_dump(mode="json", by_alias=True),
                "version": version.model_dump(mode="json", by_alias=True),
            },
        )
        if version.safety_review_status == SafetyReviewStatus.BLOCKED:
            raise ArtifactSafetyBlockedError(
                "; ".join(version.blocked_reasons)
                or "Artifact version failed safety review."
            )
        return artifact

    def confirm_artifact(
        self,
        artifact_id: str,
        *,
        confirmed_by: str = "user",
    ) -> Artifact:
        artifact = self.artifact_repository.confirm_artifact(
            artifact_id,
            confirmed_by=confirmed_by,
        )
        self._append_artifact_event(
            artifact,
            title="Artifact 已确认",
            message=f"{artifact.title} 已由用户确认，可用于导出或启用。",
            payload={"confirmed_by": confirmed_by},
        )
        return artifact

    def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        return self.artifact_repository.list_versions(artifact_id)

    def export_artifact(
        self,
        artifact_id: str,
        request: ArtifactExportRequest,
    ) -> ArtifactExportResult:
        artifact = self.artifact_repository.get_artifact(artifact_id)
        _ensure_current_version_passed(
            artifact,
            self.artifact_repository.list_versions(artifact_id),
        )
        if _requires_confirmation_for_export(artifact):
            raise ArtifactExportBlockedError(
                f"{artifact.type.value} must be confirmed before export."
            )

        if request.format == ArtifactExportFormat.JSON:
            content = json.dumps(
                artifact.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                indent=2,
            )
            media_type = "application/json"
            suffix = "json"
        elif request.format == ArtifactExportFormat.CSV:
            content = _artifact_csv(artifact)
            media_type = "text/csv;charset=utf-8"
            suffix = "csv"
        else:
            content = _artifact_markdown(artifact)
            media_type = "text/markdown;charset=utf-8"
            suffix = "md"

        result = ArtifactExportResult(
            artifact_id=artifact.id,
            format=request.format,
            filename=f"{artifact.id}.{suffix}",
            media_type=media_type,
            content=content,
        )
        self._append_artifact_event(
            artifact,
            title="Artifact 已导出",
            message=f"{artifact.title} 已导出为 {request.format.value}。",
            payload={"filename": result.filename, "format": request.format.value},
        )
        return result

    def _append_artifact_event(
        self,
        artifact: Artifact,
        *,
        title: str,
        message: str,
        payload: dict | None = None,
    ) -> None:
        if artifact.task_id is None:
            return
        events = self.task_repository.list_events(artifact.task_id)
        event = AgentEvent(
            id=f"{artifact.task_id}-event-{len(events) + 1:04d}",
            type=AgentEventType.ARTIFACT_CREATED,
            step_id=artifact.created_by_step_id,
            title=title,
            message=message,
            timestamp=datetime.now(timezone.utc),
            payload={
                "artifact_id": artifact.id,
                "artifact_type": artifact.type.value,
                **(payload or {}),
            },
        )
        self.task_repository.append_events(artifact.task_id, [event])


def _requires_confirmation_for_export(artifact: Artifact) -> bool:
    if artifact.type not in {ArtifactType.DOCTOR_REPORT, ArtifactType.CARE_PLAN}:
        return False
    return artifact.status != ArtifactStatus.READY


def _ensure_current_version_passed(
    artifact: Artifact,
    versions: list[ArtifactVersion],
) -> None:
    if not versions:
        raise ArtifactSafetyBlockedError("Artifact has no reviewed versions.")
    current_version = next(
        (
            version
            for version in versions
            if version.id == artifact.current_version_id
        ),
        versions[-1],
    )
    if current_version.safety_review_status != SafetyReviewStatus.PASSED:
        reasons = "; ".join(current_version.blocked_reasons)
        raise ArtifactSafetyBlockedError(
            reasons or "Artifact current version failed safety review."
        )


def _artifact_markdown(artifact: Artifact) -> str:
    return "\n".join(
        [
            f"# {artifact.title}",
            "",
            f"- artifact_id: {artifact.id}",
            f"- task_id: {artifact.task_id or 'unknown'}",
            f"- subject_id: {artifact.subject_id or 'unknown'}",
            f"- record_id: {artifact.record_id or 'unknown'}",
            f"- type: {artifact.type.value}",
            f"- status: {artifact.status.value}",
            f"- created_by_step_id: {artifact.created_by_step_id}",
            "",
            "## Content",
            artifact.content,
            "",
            "## Disclaimer",
            "SleepAgent 输出仅用于睡眠健康辅助分析，不替代医生诊断或治疗建议。",
        ]
    )


def _artifact_csv(artifact: Artifact) -> str:
    escaped_content = artifact.content.replace('"', '""').replace("\n", "\\n")
    return "\n".join(
        [
            "artifact_id,task_id,subject_id,record_id,type,status,title,content",
            (
                f'"{artifact.id}","{artifact.task_id or ""}",'
                f'"{artifact.subject_id or ""}","{artifact.record_id or ""}",'
                f'"{artifact.type.value}","{artifact.status.value}",'
                f'"{artifact.title}","{escaped_content}"'
            ),
        ]
    )
