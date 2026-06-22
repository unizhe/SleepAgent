from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sleepagent.preprocessing import (
    SHHSPathError,
    build_shhs_record_paths,
    extract_shhs_respiratory_event_sequence,
)
from sleepagent.schemas import (
    AnalysisNodeResult,
    AnalysisNodeStatus,
    AnalysisRequest,
    AnalysisRunResult,
    PatientProfile,
    RespiratoryEvent,
    RespiratorySummary,
    Sex,
    SignalChannel,
    SleepAnalysisResult,
    SleepRecordMetadata,
    SleepStagingMetrics,
    SleepStageSummary,
)
from sleepagent.services.risk_assessment import (
    build_risk_assessment_result,
    infer_risk_level_from_respiratory_summary,
    summarize_respiratory_events,
)
from sleepagent.services.respiratory_model_gate import (
    RESPIRATORY_MODEL_STATUS_NOT_VALIDATED,
    not_validated_respiratory_model_gate,
)
from sleepagent.services.signal_quality import (
    build_failed_quality_result,
    build_signal_quality_result,
)
from sleepagent.services.yasa_runner import (
    EDFSignalInfo,
    YASARunnerResult,
    inspect_edf_signal,
    run_yasa_sleep_staging,
)


RESPIRATORY_MODEL_UNVALIDATED_CAVEAT = "respiratory_model_unvalidated"
RESPIRATORY_DEMO_ONLY_CAVEAT = "pipeline_demo_only"
YASA_ASSISTIVE_CAVEAT = (
    "YASA sleep staging is an assistive research output and requires scorer review."
)
REAL_ANALYSIS_CAVEAT = (
    "Real SHHS-derived analysis; still not a diagnosis or treatment recommendation."
)


class AnalysisServiceError(RuntimeError):
    """Raised when real analysis cannot produce a SleepAnalysisResult."""


@dataclass(frozen=True)
class _ResolvedRecordPaths:
    record_id: str
    edf_path: Path
    nsrr_xml_path: Path | None
    profusion_xml_path: Path | None

    @property
    def source_paths(self) -> dict[str, str]:
        paths = {"edf": str(self.edf_path)}
        if self.nsrr_xml_path is not None:
            paths["nsrr_xml"] = str(self.nsrr_xml_path)
        if self.profusion_xml_path is not None:
            paths["profusion_xml"] = str(self.profusion_xml_path)
        return paths

    @property
    def respiratory_xml_path(self) -> Path | None:
        if self.profusion_xml_path is not None and self.profusion_xml_path.exists():
            return self.profusion_xml_path
        if self.nsrr_xml_path is not None and self.nsrr_xml_path.exists():
            return self.nsrr_xml_path
        return None


class AnalysisService:
    """Phase 1 real analysis entrypoint for local SHHS EDF/XML data."""

    def __init__(
        self,
        *,
        edf_inspector: Callable[[str | Path], EDFSignalInfo] = inspect_edf_signal,
        yasa_runner: Callable[..., YASARunnerResult] = run_yasa_sleep_staging,
        respiratory_event_extractor: Callable[
            [str | Path], object
        ] = extract_shhs_respiratory_event_sequence,
    ) -> None:
        self._edf_inspector = edf_inspector
        self._yasa_runner = yasa_runner
        self._respiratory_event_extractor = respiratory_event_extractor

    def run_analysis(self, request: AnalysisRequest) -> AnalysisRunResult:
        generated_at = datetime.now(timezone.utc)
        record_result, resolved_paths = self._load_record(request)
        if resolved_paths is None:
            return self._failed_run(
                record_result=record_result,
                failure_name="LoadRecord",
                failure_message=record_result.error or "Could not resolve SHHS record.",
                generated_at=generated_at,
            )

        quality_result, edf_info = self._quality_check(request, resolved_paths)
        if edf_info is None:
            return self._failed_run(
                record_result=record_result,
                quality_result=quality_result,
                failure_name="QualityCheck",
                failure_message=quality_result.error or "Could not read EDF metadata.",
                generated_at=generated_at,
            )

        sleep_staging_result, sleep_summary, epochs, sleep_staging_metrics = (
            self._sleep_staging(
                request,
                resolved_paths,
            )
        )
        if sleep_summary is None or epochs is None:
            return self._failed_run(
                record_result=record_result,
                quality_result=quality_result,
                sleep_staging_result=sleep_staging_result,
                failure_name="SleepStaging",
                failure_message=sleep_staging_result.error or "YASA staging failed.",
                generated_at=generated_at,
            )

        respiratory_result, respiratory_events, respiratory_summary = (
            self._respiratory_detection(request, resolved_paths, sleep_summary)
        )
        risk_caveats = [
            *respiratory_result.caveats,
            RESPIRATORY_MODEL_STATUS_NOT_VALIDATED,
            (
                "Respiratory ML checkpoint is not validated for the Agent risk "
                "conclusion and is not used as negative evidence."
            ),
            (
                "Respiratory risk is based on mapped SHHS annotation events and "
                "YASA-derived sleep time."
            ),
        ]
        risk_result = build_risk_assessment_result(
            respiratory_summary,
            caveats=risk_caveats,
            source_paths=respiratory_result.source_paths,
        )
        analysis = self._build_sleep_analysis_result(
            request=request,
            resolved_paths=resolved_paths,
            edf_info=edf_info,
            sleep_summary=sleep_summary,
            epochs=epochs,
            sleep_staging_metrics=sleep_staging_metrics,
            respiratory_events=respiratory_events,
            respiratory_summary=respiratory_summary,
            generated_at=generated_at,
            caveats=self._collect_caveats(
                record_result,
                quality_result,
                sleep_staging_result,
                respiratory_result,
                risk_result,
            ),
        )

        return AnalysisRunResult(
            record_status=AnalysisNodeStatus.COMPLETED,
            record_result=record_result,
            quality_result=quality_result,
            sleep_staging_result=sleep_staging_result,
            respiratory_result=respiratory_result,
            risk_result=risk_result,
            sleep_analysis_result=analysis,
            caveats=analysis.notes,
            generated_at=generated_at,
        )

    def run_sleep_analysis(self, request: AnalysisRequest) -> SleepAnalysisResult:
        result = self.run_analysis(request)
        if result.sleep_analysis_result is None:
            raise AnalysisServiceError(
                result.record_result.error
                or result.sleep_staging_result.error
                or result.quality_result.error
                or "Real analysis did not produce a SleepAnalysisResult."
            )
        return result.sleep_analysis_result

    def _load_record(
        self,
        request: AnalysisRequest,
    ) -> tuple[AnalysisNodeResult, _ResolvedRecordPaths | None]:
        try:
            resolved_paths = self._resolve_record_paths(request)
        except (SHHSPathError, ValueError) as exc:
            error = str(exc)
            return (
                AnalysisNodeResult(
                    name="LoadRecord",
                    status=AnalysisNodeStatus.FAILED,
                    warnings=[error],
                    caveats=["Provide SHHS root or explicit local EDF/XML paths."],
                    error=error,
                ),
                None,
            )

        missing_required = []
        if not resolved_paths.edf_path.exists():
            missing_required.append("edf")
        if resolved_paths.respiratory_xml_path is None:
            missing_required.append("nsrr_or_profusion_xml")

        source_paths = resolved_paths.source_paths
        payload = {
            "record_id": resolved_paths.record_id,
            "edf_exists": resolved_paths.edf_path.exists(),
            "nsrr_xml_exists": (
                resolved_paths.nsrr_xml_path.exists()
                if resolved_paths.nsrr_xml_path is not None
                else False
            ),
            "profusion_xml_exists": (
                resolved_paths.profusion_xml_path.exists()
                if resolved_paths.profusion_xml_path is not None
                else False
            ),
            "missing_required_roles": missing_required,
        }
        if missing_required:
            error = "Missing required local SHHS file role(s): " + ", ".join(
                missing_required
            )
            return (
                AnalysisNodeResult(
                    name="LoadRecord",
                    status=AnalysisNodeStatus.FAILED,
                    payload=payload,
                    warnings=[error],
                    caveats=[
                        "Real analysis requires a local EDF and at least one SHHS XML annotation file."
                    ],
                    source_paths=source_paths,
                    error=error,
                ),
                None,
            )

        return (
            AnalysisNodeResult(
                name="LoadRecord",
                status=AnalysisNodeStatus.COMPLETED,
                payload=payload,
                caveats=[
                    "Raw SHHS EDF/XML files are referenced by local path only and are not embedded."
                ],
                source_paths=source_paths,
                metrics={"missing_required_role_count": 0},
            ),
            resolved_paths,
        )

    def _quality_check(
        self,
        request: AnalysisRequest,
        paths: _ResolvedRecordPaths,
    ) -> tuple[AnalysisNodeResult, EDFSignalInfo | None]:
        try:
            edf_info = self._edf_inspector(paths.edf_path)
        except Exception as exc:
            error = str(exc)
            return (
                build_failed_quality_result(
                    error=error,
                    source_paths=paths.source_paths,
                ),
                None,
            )

        return (
            build_signal_quality_result(
                edf_info,
                eeg_channel=request.eeg_channel,
                eog_channel=request.eog_channel,
                emg_channel=request.emg_channel,
                respiratory_channels=request.respiratory_channels,
                source_paths=paths.source_paths,
            ),
            edf_info,
        )

    def _sleep_staging(
        self,
        request: AnalysisRequest,
        paths: _ResolvedRecordPaths,
    ) -> tuple[
        AnalysisNodeResult,
        SleepStageSummary | None,
        list | None,
        SleepStagingMetrics | None,
    ]:
        try:
            runner_result = self._yasa_runner(
                paths.edf_path,
                eeg_name=request.eeg_channel,
                eog_name=request.eog_channel,
                emg_name=request.emg_channel,
            )
        except Exception as exc:
            error = str(exc)
            return (
                AnalysisNodeResult(
                    name="SleepStaging",
                    status=AnalysisNodeStatus.FAILED,
                    warnings=[error],
                    caveats=[YASA_ASSISTIVE_CAVEAT],
                    source_paths=paths.source_paths,
                    error=error,
                ),
                None,
                None,
                None,
            )

        metrics = runner_result.staging.sleep_staging_metrics
        metrics_payload = metrics.model_dump(mode="json") if metrics is not None else {}
        sleep_summary = runner_result.staging.sleep_summary
        return (
            AnalysisNodeResult(
                name="SleepStaging",
                status=AnalysisNodeStatus.COMPLETED,
                payload={
                    "epoch_count": len(runner_result.staging.epochs),
                    "channels": {
                        "eeg": runner_result.eeg_name,
                        "eog": runner_result.eog_name,
                        "emg": runner_result.emg_name,
                    },
                    "sleep_summary": sleep_summary.model_dump(mode="json"),
                },
                caveats=[YASA_ASSISTIVE_CAVEAT],
                source_paths={"edf": str(paths.edf_path)},
                metrics=metrics_payload,
            ),
            sleep_summary,
            runner_result.staging.epochs,
            metrics,
        )

    def _respiratory_detection(
        self,
        request: AnalysisRequest,
        paths: _ResolvedRecordPaths,
        sleep_summary: SleepStageSummary,
    ) -> tuple[AnalysisNodeResult, list[RespiratoryEvent], RespiratorySummary]:
        warnings: list[str] = []
        caveats = [
            "Respiratory events are mapped from SHHS XML annotations; model inference is not used by default."
        ]
        source_artifacts: dict[str, str] = {}
        status = AnalysisNodeStatus.COMPLETED

        if request.use_respiratory_model:
            status = AnalysisNodeStatus.SKIPPED_WITH_WARNING
            warnings.append(
                "Respiratory model inference was requested but is gated off until Phase 5 validation passes."
            )
            caveats.append(RESPIRATORY_MODEL_UNVALIDATED_CAVEAT)
            caveats.append(RESPIRATORY_MODEL_STATUS_NOT_VALIDATED)
            if request.allow_demo_respiratory_model:
                caveats.append(RESPIRATORY_DEMO_ONLY_CAVEAT)
            if request.respiratory_checkpoint_path is not None:
                source_artifacts["respiratory_checkpoint"] = (
                    request.respiratory_checkpoint_path
                )
        gate_result = not_validated_respiratory_model_gate(
            "runtime_model_inference_not_validated_for_risk_conclusion"
        )

        xml_path = paths.respiratory_xml_path
        if xml_path is None:
            warnings.append("No SHHS respiratory XML annotation path is available.")
            caveats.append(
                "No respiratory annotation summary is available; do not treat this as no abnormal events."
            )
            events: list[RespiratoryEvent] = []
        else:
            sequence = self._respiratory_event_extractor(xml_path)
            events = list(getattr(sequence, "events"))

        respiratory_summary = summarize_respiratory_events(
            events,
            sleep_summary=sleep_summary,
        )
        return (
            AnalysisNodeResult(
                name="RespiratoryDetection",
                status=status,
                payload={
                    "event_count": len(events),
                    "respiratory_summary": respiratory_summary.model_dump(mode="json"),
                    "model_used": False,
                    "respiratory_model_status": gate_result.respiratory_model_status,
                    "respiratory_model_gate": gate_result.model_dump(),
                },
                warnings=warnings,
                caveats=caveats,
                source_paths=(
                    {"xml": str(xml_path)} if xml_path is not None else paths.source_paths
                ),
                source_artifacts=source_artifacts,
                metrics={
                    "ahi": respiratory_summary.ahi,
                    "hypopnea_count": respiratory_summary.hypopnea_count,
                    "suspected_apnea_count": respiratory_summary.suspected_apnea_count,
                },
            ),
            events,
            respiratory_summary,
        )

    def _build_sleep_analysis_result(
        self,
        *,
        request: AnalysisRequest,
        resolved_paths: _ResolvedRecordPaths,
        edf_info: EDFSignalInfo,
        sleep_summary: SleepStageSummary,
        epochs: list,
        sleep_staging_metrics: SleepStagingMetrics | None,
        respiratory_events: list[RespiratoryEvent],
        respiratory_summary: RespiratorySummary,
        generated_at: datetime,
        caveats: list[str],
    ) -> SleepAnalysisResult:
        subject_id = _resolve_subject_id(request, resolved_paths.record_id)
        metadata = SleepRecordMetadata(
            record_id=resolved_paths.record_id,
            source_dataset="shhs",
            patient=PatientProfile(
                subject_id=subject_id,
                sex=Sex.UNKNOWN,
                notes=["Real SHHS local sample; demographic metadata not loaded in Phase 1."],
            ),
            duration_seconds=edf_info.duration_seconds,
            channels=[
                SignalChannel(
                    name=channel_name,
                    sampling_rate_hz=edf_info.sampling_rate_hz,
                    source="SHHS EDF",
                )
                for channel_name in edf_info.channel_names
            ],
        )
        return SleepAnalysisResult(
            metadata=metadata,
            epochs=epochs,
            respiratory_events=respiratory_events,
            respiratory_trend=[],
            sleep_summary=sleep_summary,
            respiratory_summary=respiratory_summary,
            sleep_staging_metrics=sleep_staging_metrics,
            respiratory_detection_metrics=None,
            risk_level=infer_risk_level_from_respiratory_summary(respiratory_summary),
            generated_at=generated_at,
            notes=caveats,
        )

    def _resolve_record_paths(self, request: AnalysisRequest) -> _ResolvedRecordPaths:
        if request.edf_path is None:
            record_paths = build_shhs_record_paths(
                root=request.shhs_root,
                record_id=request.record_id,
            )
            return _ResolvedRecordPaths(
                record_id=record_paths.record_id,
                edf_path=record_paths.edf_path,
                nsrr_xml_path=record_paths.nsrr_annotation_path,
                profusion_xml_path=record_paths.profusion_annotation_path,
            )

        return _ResolvedRecordPaths(
            record_id=request.record_id,
            edf_path=_resolve_optional_path(request.edf_path),
            nsrr_xml_path=_resolve_optional_path(request.nsrr_xml_path),
            profusion_xml_path=_resolve_optional_path(request.profusion_xml_path),
        )

    def _failed_run(
        self,
        *,
        record_result: AnalysisNodeResult,
        failure_name: str,
        failure_message: str,
        generated_at: datetime,
        quality_result: AnalysisNodeResult | None = None,
        sleep_staging_result: AnalysisNodeResult | None = None,
    ) -> AnalysisRunResult:
        resolved_quality_result = quality_result or self._skipped_node("QualityCheck")
        resolved_sleep_staging_result = sleep_staging_result or self._skipped_node(
            "SleepStaging"
        )
        respiratory_result = self._skipped_node("RespiratoryDetection")
        risk_result = self._skipped_node("RiskAssessment")
        caveats = self._collect_caveats(
            record_result,
            resolved_quality_result,
            resolved_sleep_staging_result,
            respiratory_result,
            risk_result,
        )
        if failure_message not in caveats:
            caveats.append(failure_message)
        return AnalysisRunResult(
            record_status=AnalysisNodeStatus.FAILED,
            record_result=record_result,
            quality_result=resolved_quality_result,
            sleep_staging_result=resolved_sleep_staging_result,
            respiratory_result=respiratory_result,
            risk_result=risk_result,
            caveats=caveats,
            generated_at=generated_at,
        )

    def _skipped_node(self, name: str) -> AnalysisNodeResult:
        return AnalysisNodeResult(
            name=name,
            status=AnalysisNodeStatus.SKIPPED,
            caveats=["Skipped because an earlier real analysis node failed."],
        )

    def _collect_caveats(self, *nodes: AnalysisNodeResult) -> list[str]:
        caveats: list[str] = [REAL_ANALYSIS_CAVEAT]
        for node in nodes:
            for caveat in node.caveats:
                if caveat not in caveats:
                    caveats.append(caveat)
        return caveats


def _resolve_optional_path(value: str | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser().resolve()


def _resolve_subject_id(request: AnalysisRequest, record_id: str) -> str:
    if request.subject_id and not (
        request.subject_id == "mock-subject-0001" and record_id.startswith("shhs")
    ):
        return request.subject_id
    if "-" in record_id:
        return record_id.split("-", maxsplit=1)[1]
    return record_id
