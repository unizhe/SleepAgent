"""Habit 的核心合同与纯领域规则。

本模块只保留经审核的概念、问答取证和 Profile 修订语义。它不提供
repository、数据库、API、Tool、worker 或 continuation；调用方必须在未来的
production integration 中另行提供持久化和身份认证。
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import date, datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Literal, Mapping

from pydantic import Field, model_validator

from sleepagent.domain.contracts import SleepDomainContract


CATALOG_VERSION = "sleep-habit-concepts.zh-CN.v1"
REVIEW_REF = "sleep-habit-profile-plan:approved-round-5"
MAX_EPISODE_QUESTIONS = 3

HabitRole = Literal["elder", "family"]
HabitOrigin = Literal["elder_self_report", "family_observation"]
DayType = Literal["all_days", "weekday", "weekend", "variable"]


def _stable_hash(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contract_hash(value: SleepDomainContract, excluded_field: str) -> str:
    return _stable_hash(
        value.model_dump(mode="json", exclude={excluded_field})
    )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


class HabitAnswerType(str, Enum):
    CHOICE = "choice"
    SCALE = "scale"
    BOUNDED_NUMBER = "bounded_number"
    SHORT_TEXT = "short_text"


class HabitRespondent(str, Enum):
    ELDER_ONLY = "elder_only"
    ELDER_OR_OBSERVER = "elder_or_observer"


class HabitDisposition(str, Enum):
    ANSWERED = "answered"
    VARIABLE = "variable"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"
    PREFER_NOT_TO_ANSWER = "prefer_not_to_answer"
    SKIPPED = "skipped"
    NEVER_ASK = "never_ask"


class HabitConceptStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    STALE = "stale"
    DISPUTED = "disputed"


class HabitOperation(str, Enum):
    REMEMBER = "remember"
    CORRECT = "correct"
    EXPIRE = "expire"
    FORGET = "forget"


class HabitConcept(SleepDomainContract):
    """一个经人工审核、不能被模型动态扩展的 Habit 概念。"""

    concept_id: str = Field(pattern=r"^habit\.[a-z0-9_]+$")
    version: Literal["1.0.0"] = "1.0.0"
    domain: str
    purpose: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=160)
    observer_question: str | None = Field(default=None, max_length=160)
    answer_type: HabitAnswerType
    options: tuple[str, ...] = ()
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    respondent: HabitRespondent
    profile_eligible: bool = True
    valid_for_days: int = Field(ge=1, le=730)
    cooldown_hours: int = Field(default=168, ge=0, le=8760)
    decision_uses: tuple[str, ...] = ()
    negative_requires_direct_observation: bool = False
    safety_route: bool = False
    catalog_version: Literal["sleep-habit-concepts.zh-CN.v1"] = CATALOG_VERSION
    review_ref: Literal[
        "sleep-habit-profile-plan:approved-round-5"
    ] = REVIEW_REF

    @model_validator(mode="after")
    def validate_reviewed_shape(self) -> "HabitConcept":
        if self.answer_type in {HabitAnswerType.CHOICE, HabitAnswerType.SCALE}:
            if len(self.options) < 2:
                raise ValueError("reviewed choice requires at least two options")
        elif self.options:
            raise ValueError("only choice/scale may define options")
        if self.answer_type == HabitAnswerType.BOUNDED_NUMBER:
            if self.unit is None or self.minimum is None or self.maximum is None:
                raise ValueError("bounded answer requires unit and limits")
            if self.minimum > self.maximum:
                raise ValueError("Habit numeric range is reversed")
        elif self.minimum is not None or self.maximum is not None:
            raise ValueError("numeric limits require bounded_number")
        if self.respondent == HabitRespondent.ELDER_OR_OBSERVER:
            if not self.observer_question:
                raise ValueError("observer-eligible concept requires observer wording")
        return self


def _concept(
    concept_id: str,
    domain: str,
    purpose: str,
    question: str,
    answer_type: HabitAnswerType,
    *,
    options: tuple[str, ...] = (),
    observer: str | None = None,
    respondent: HabitRespondent = HabitRespondent.ELDER_ONLY,
    unit: str | None = None,
    limits: tuple[float, float] | None = None,
    days: int = 90,
    uses: tuple[str, ...] = (),
    profile: bool = True,
    cooldown: int = 168,
    direct_negative: bool = False,
    safety: bool = False,
) -> HabitConcept:
    return HabitConcept(
        concept_id=concept_id,
        domain=domain,
        purpose=purpose,
        question=question,
        observer_question=observer,
        answer_type=answer_type,
        options=options,
        unit=unit,
        minimum=limits[0] if limits else None,
        maximum=limits[1] if limits else None,
        respondent=respondent,
        profile_eligible=profile,
        valid_for_days=days,
        cooldown_hours=cooldown,
        decision_uses=uses,
        negative_requires_direct_observation=direct_negative,
        safety_route=safety,
    )


_OBSERVER = HabitRespondent.ELDER_OR_OBSERVER
REVIEWED_HABIT_CONCEPTS = (
    _concept(
        "habit.primary_goal", "personal_goal", "非临床改善目标",
        "您现在最想先改善哪一件睡眠方面的事？", HabitAnswerType.CHOICE,
        options=("更规律", "白天更有精神", "夜里少受打扰", "暂时没有", "不固定"),
        uses=("care_goal", "care_burden"),
    ),
    _concept(
        "habit.schedule_constraint", "schedule_constraint", "限制行动安排的作息约束",
        "平时有没有必须按时起床或晚上必须做的事情？", HabitAnswerType.CHOICE,
        options=("没有固定约束", "必须早起", "夜间需照护他人", "固定活动", "不固定"),
        days=180, uses=("care_timing", "schedule_interpretation"),
    ),
    _concept(
        "habit.nap_pattern", "nap", "午睡模式上下文", "您平时会午睡吗？",
        HabitAnswerType.CHOICE, options=("通常不午睡", "偶尔午睡", "多数天午睡", "不固定"),
        observer="您实际观察到老人平时会午睡吗？", respondent=_OBSERVER,
        uses=("daytime_context", "care_timing"),
    ),
    _concept(
        "habit.nap_duration_minutes", "nap", "大致午睡时长", "一般一次午睡大约多久？",
        HabitAnswerType.BOUNDED_NUMBER, observer="您观察到老人一次午睡通常大约多久？",
        respondent=_OBSERVER, unit="minute", limits=(0, 240),
        uses=("daytime_context", "care_timing"),
    ),
    _concept(
        "habit.pre_sleep_behavior", "pre_sleep_behavior", "重复睡前行为", "睡前通常会做什么？",
        HabitAnswerType.CHOICE, options=("看屏幕", "阅读", "洗漱", "聊天", "放松活动", "不固定"),
        uses=("care_action_selection", "evidence_alternative"),
    ),
    _concept(
        "habit.environment_preference", "environment", "中性睡眠环境偏好",
        "睡觉时，您更喜欢怎样的光线和声音？", HabitAnswerType.CHOICE,
        options=("较暗安静", "留一点光", "有背景声音", "没有固定偏好"),
        days=180, uses=("care_environment",),
    ),
    _concept(
        "habit.stimulant_timing", "stimulant_timing", "咖啡或浓茶的时间模式",
        "平时咖啡或浓茶一般在什么时候喝？", HabitAnswerType.CHOICE,
        options=("通常不喝", "上午", "下午", "晚间", "不固定"),
        uses=("evidence_alternative", "care_action_selection"),
    ),
    _concept(
        "habit.sleep_satisfaction_recent", "subjective_context", "近期主观睡眠感受",
        "最近一周，您对自己的睡眠感觉怎么样？", HabitAnswerType.SCALE,
        options=("满意", "还可以", "不太满意", "很不满意"),
        days=14, uses=("subjective_context", "care_goal"),
    ),
    _concept(
        "habit.observed_snoring", "observable_night_behavior", "直接观察的打鼾或憋醒，不形成诊断",
        "近期您知道自己有明显打鼾或憋醒吗？", HabitAnswerType.CHOICE,
        options=("观察到", "没有观察到", "偶尔", "不清楚"),
        observer="近期您是否亲自听到或看到老人明显打鼾或憋醒？", respondent=_OBSERVER,
        days=30, uses=("evidence_uncertainty", "safety_route"), direct_negative=True, safety=True,
    ),
    _concept(
        "habit.night_toileting_pattern", "night_activity", "通常夜间如厕模式",
        "您平时夜里会起床去卫生间吗？", HabitAnswerType.CHOICE,
        options=("通常不会", "偶尔", "多数夜晚", "不固定"),
        observer="您亲自观察到老人平时会因如厕在夜间离床吗？", respondent=_OBSERVER,
        uses=("night_event_interpretation", "evidence_alternative"),
    ),
    _concept(
        "habit.night_out_of_bed_frequency", "night_activity", "通常每夜离床次数",
        "平时一晚上大约会起床几次？", HabitAnswerType.BOUNDED_NUMBER,
        observer="您亲自观察到老人平时一晚上大约离床几次？", respondent=_OBSERVER,
        unit="count_per_night", limits=(0, 20), uses=("night_event_interpretation", "trend_comparison"),
    ),
    _concept(
        "habit.night_out_of_bed_time_window", "night_activity", "通常夜间离床时段",
        "平时夜里大约几点会起床？说个大概时段就可以。", HabitAnswerType.SHORT_TEXT,
        observer="您亲自观察到老人通常在夜里什么时段离床？", respondent=_OBSERVER,
        uses=("night_event_interpretation", "care_timing"),
    ),
    _concept(
        "habit.night_out_of_bed_duration_minutes", "night_activity", "通常一次夜间离床时长",
        "平时夜里一次起床大约多久会回床？", HabitAnswerType.BOUNDED_NUMBER,
        observer="您亲自观察到老人一次夜间离床通常持续多久？", respondent=_OBSERVER,
        unit="minute", limits=(0, 240), uses=("night_event_interpretation", "risk_attention"),
    ),
    _concept(
        "habit.night_activity_assistance_need", "night_activity", "夜间离床协助需要",
        "平时夜里起床，您一般能自己完成还是需要帮忙？", HabitAnswerType.CHOICE,
        options=("通常可自行完成", "偶尔需要协助", "通常需要协助", "不固定"),
        observer="您亲自观察到老人夜间离床通常是否需要协助？", respondent=_OBSERVER,
        uses=("care_modality", "family_coordination", "risk_context"),
    ),
    _concept(
        "habit.observed_night_leaving", "observable_night_behavior", "直接观察的夜间离床",
        "近期您记得自己夜里离开床吗？", HabitAnswerType.CHOICE,
        options=("观察到", "没有观察到", "偶尔", "不清楚"),
        observer="近期您是否亲自观察到老人夜间离床？", respondent=_OBSERVER,
        days=30, uses=("evidence_uncertainty", "night_event_interpretation"), direct_negative=True,
    ),
    _concept(
        "habit.delivery_timing_preference", "care_delivery_preference", "非紧急提醒时间偏好",
        "不是紧急情况时，您希望当时提醒，还是早晨再说？", HabitAnswerType.CHOICE,
        options=("立即", "早晨", "视情况", "不希望提醒"),
        days=180, uses=("care_timing", "care_burden"),
    ),
    _concept(
        "habit.delivery_modality_preference", "care_delivery_preference", "非紧急提醒方式偏好",
        "不是紧急情况时，您更接受语音、灯光，还是先不打扰？", HabitAnswerType.CHOICE,
        options=("语音", "灯光", "静默记录", "没有固定偏好"),
        days=180, uses=("care_modality", "care_burden"),
    ),
    _concept(
        "habit.interruption_burden", "care_delivery_preference", "非紧急交互打扰上限",
        "夜里如果不是紧急情况，您能接受多大程度的提醒？", HabitAnswerType.CHOICE,
        options=("不打扰", "轻微", "一般", "可以明显提醒"),
        days=180, uses=("care_burden", "care_modality"),
    ),
    _concept(
        "habit.family_notification_preference", "care_delivery_preference", "非紧急家属通知偏好",
        "不是紧急情况时，夜里有情况要不要通知家里人？", HabitAnswerType.CHOICE,
        options=("不通知", "早晨通知", "当时通知", "视情况"),
        days=180, uses=("family_coordination", "care_burden"),
    ),
    _concept(
        "habit.quiet_hours", "care_delivery_preference", "非紧急提醒安静时段",
        "您希望从几点到几点尽量不被非紧急提醒打扰？", HabitAnswerType.SHORT_TEXT,
        days=180, uses=("quiet_hours", "care_timing"),
    ),
    _concept(
        "habit.voice_volume_preference", "care_delivery_preference", "非紧急语音音量偏好",
        "如果用语音提醒，您希望音量大约是多少？", HabitAnswerType.BOUNDED_NUMBER,
        unit="percent", limits=(0, 100), days=180, uses=("voice_volume", "care_burden"),
    ),
    _concept(
        "habit.device_position_last_night", "data_quality", "当前单夜设备位置变化",
        "昨晚床边设备的位置动过吗？", HabitAnswerType.CHOICE,
        options=("没有", "可能移动过", "不清楚"),
        observer="昨晚雷达设备位置或遮挡情况是否变化？", respondent=_OBSERVER,
        days=1, uses=("data_quality",), profile=False, cooldown=0,
    ),
)
if len({item.concept_id for item in REVIEWED_HABIT_CONCEPTS}) != len(REVIEWED_HABIT_CONCEPTS):
    raise RuntimeError("reviewed Habit catalog contains duplicate concept ids")
HABIT_CONCEPTS: Mapping[str, HabitConcept] = MappingProxyType(
    {item.concept_id: item for item in REVIEWED_HABIT_CONCEPTS}
)


class HabitQuestionState(SleepDomainContract):
    """合并 selection 的请求上下文和不可伪造结果，避免两套重复 DTO。"""

    phase: Literal["request", "selected"] = "request"
    episode_id: str
    subject_id: str
    actor_id: str
    role: HabitRole
    concept_states: dict[str, HabitConceptStatus] = Field(default_factory=dict)
    candidate_concept_ids: tuple[str, ...] = ()
    suppressed_concept_ids: tuple[str, ...] = ()
    cooldown_until: dict[str, datetime] = Field(default_factory=dict)
    remaining_episode_budget: int = Field(ge=0, le=MAX_EPISODE_QUESTIONS)
    selection_id: str | None = None
    selected_concepts: tuple[tuple[str, str], ...] = ()
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_phase(self) -> "HabitQuestionState":
        for value in self.cooldown_until.values():
            _require_aware(value, "Habit cooldown")
        result_fields = (
            self.selection_id,
            self.issued_at,
            self.expires_at,
            self.receipt_hash,
        )
        if self.phase == "request" and (any(result_fields) or self.selected_concepts):
            raise ValueError("unselected Habit request cannot carry a receipt")
        if self.phase == "selected" and (not all(result_fields)):
            raise ValueError("selected Habit state requires a complete receipt")
        if len(self.selected_concepts) > self.remaining_episode_budget:
            raise ValueError("Habit selection exceeds its question budget")
        if len({item[0] for item in self.selected_concepts}) != len(
            self.selected_concepts
        ):
            raise ValueError("Habit selection repeats a concept")
        return self


def select_habit_questions(
    state: HabitQuestionState,
    *,
    now: datetime,
    catalog: Mapping[str, HabitConcept] = HABIT_CONCEPTS,
) -> HabitQuestionState:
    """按缺口、负担和审核目录确定性选择最多三个问题。"""

    _require_aware(now, "Habit selection time")
    if state.phase != "request":
        raise ValueError("Habit question state was already selected")
    requested = set(state.candidate_concept_ids or catalog)
    suppressed = set(state.suppressed_concept_ids)
    rank = {
        HabitConceptStatus.DISPUTED: 0,
        HabitConceptStatus.STALE: 1,
        HabitConceptStatus.UNKNOWN: 2,
        HabitConceptStatus.KNOWN: 3,
    }
    burden = {
        HabitAnswerType.CHOICE: 0,
        HabitAnswerType.SCALE: 1,
        HabitAnswerType.BOUNDED_NUMBER: 2,
        HabitAnswerType.SHORT_TEXT: 3,
    }
    intake = {
        "habit.primary_goal": 0,
        "habit.schedule_constraint": 1,
        "habit.sleep_satisfaction_recent": 2,
        "habit.nap_pattern": 3,
    }
    eligible: list[tuple[tuple[int, int, int, str], str]] = []
    for concept_id in requested:
        concept = catalog.get(concept_id)
        if concept is None or concept_id in suppressed:
            continue
        if state.cooldown_until.get(concept_id, now) > now:
            continue
        if state.role == "family" and concept.respondent != _OBSERVER:
            continue
        status = state.concept_states.get(concept_id, HabitConceptStatus.UNKNOWN)
        if status == HabitConceptStatus.KNOWN and not state.candidate_concept_ids:
            continue
        eligible.append(
            ((rank[status], intake.get(concept_id, 4), burden[concept.answer_type], concept_id), concept_id)
        )
    count = state.remaining_episode_budget
    selected = tuple(
        (concept_id, catalog[concept_id].version)
        for _priority, concept_id in sorted(eligible)[:count]
    )
    selection_id = f"habit-selection:{_stable_hash({'request': state.model_dump(mode='json'), 'issued_at': now})[:32]}"
    unsigned = state.model_copy(
        update={
            "phase": "selected",
            "selection_id": selection_id,
            "selected_concepts": selected,
            "issued_at": now,
            "expires_at": now + timedelta(minutes=30),
            "receipt_hash": "0" * 64,
        }
    )
    return unsigned.model_copy(
        update={"receipt_hash": _contract_hash(unsigned, "receipt_hash")}
    )


class HabitAnswer(SleepDomainContract):
    concept_id: str
    concept_version: str = "1.0.0"
    disposition: HabitDisposition
    value: Any = None
    observation_date_start: date | None = None
    observation_date_end: date | None = None
    timezone_name: str = "Asia/Shanghai"
    day_type: DayType = "all_days"
    direct_observation: bool = False
    observation_description: str | None = Field(default=None, max_length=240)
    observation_confidence: float = Field(default=0, ge=0, le=1)
    opt_out_acknowledged: Literal[True] | None = None

    @model_validator(mode="after")
    def validate_authority_fields(self) -> "HabitAnswer":
        if self.disposition == HabitDisposition.NEVER_ASK:
            if self.opt_out_acknowledged is not True:
                raise ValueError("never_ask requires explicit acknowledgement")
        elif self.opt_out_acknowledged is not None:
            raise ValueError("opt-out acknowledgement only belongs to never_ask")
        if self.direct_observation:
            text = (self.observation_description or "").lower()
            if not text or self.observation_confidence <= 0:
                raise ValueError("direct observation requires description and confidence")
            if any(term in text for term in ("转述", "听说", "hearsay", "told me")):
                raise ValueError("hearsay is not direct observation")
        elif self.observation_confidence != 0:
            raise ValueError("indirect answer cannot claim observation confidence")
        if self.observation_date_start and self.observation_date_end:
            if self.observation_date_start > self.observation_date_end:
                raise ValueError("Habit observation window is reversed")
        return self


class HabitEvidence(SleepDomainContract):
    """一次问答产生的短期、带来源且默认不进入 Profile 的证据。"""

    evidence_id: str
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_id: str
    episode_id: str
    subject_id: str
    actor_id: str
    role: HabitRole
    concept_id: str
    concept_version: str
    disposition: HabitDisposition
    value: Any = None
    origin: HabitOrigin
    observation_date_start: date | None = None
    observation_date_end: date | None = None
    timezone_name: str
    day_type: DayType
    direct_observation: bool = False
    observation_description: str | None = None
    observation_confidence: float = 0
    profile_eligible: bool = False
    safety_reason: str | None = None
    suppressed_until: datetime | None = None
    captured_at: datetime
    valid_until: datetime
    trust_label: Literal["user_data"] = "user_data"


_NON_VALUES = {
    HabitDisposition.NOT_APPLICABLE,
    HabitDisposition.UNKNOWN,
    HabitDisposition.PREFER_NOT_TO_ANSWER,
    HabitDisposition.SKIPPED,
    HabitDisposition.NEVER_ASK,
}
_PROFILE_VALUES = {
    HabitDisposition.ANSWERED,
    HabitDisposition.VARIABLE,
    HabitDisposition.NOT_APPLICABLE,
}


def _normalize_answer(concept: HabitConcept, answer: HabitAnswer) -> Any:
    if answer.disposition in _NON_VALUES:
        if answer.value not in (None, ""):
            raise ValueError("non-answer disposition cannot carry a value")
        return None
    if answer.disposition == HabitDisposition.VARIABLE:
        if answer.value not in (None, "", "不固定"):
            raise ValueError("variable disposition cannot carry a fixed value")
        return "variable"
    if answer.value is None:
        raise ValueError("answered Habit question requires a value")
    if concept.answer_type in {HabitAnswerType.CHOICE, HabitAnswerType.SCALE}:
        value = str(answer.value).strip()
        if value not in concept.options:
            raise ValueError("Habit answer is outside reviewed options")
        return value
    if concept.answer_type == HabitAnswerType.BOUNDED_NUMBER:
        if isinstance(answer.value, bool) or not isinstance(answer.value, (int, float)):
            raise ValueError("bounded Habit answer must be numeric")
        value = float(answer.value)
        if concept.minimum is None or concept.maximum is None:
            raise ValueError("reviewed numeric bounds are missing")
        if not concept.minimum <= value <= concept.maximum:
            raise ValueError("Habit answer is outside reviewed bounds")
        return {"value": value, "unit": concept.unit}
    value = unicodedata.normalize("NFKC", str(answer.value)).strip()
    if not value or len(value) > 160:
        raise ValueError("short Habit answer must contain 1..160 characters")
    return value


def _clinical(value: Any) -> bool:
    text = str(value or "").lower()
    return any(
        marker in text
        for marker in (
            "过敏", "疾病", "诊断", "用药", "药物", "剂量", "高血压", "糖尿病",
            "呼吸暂停", "psqi", "isi", "ess", "allergy", "diagnosis",
            "medication", "dose", "apnea",
        )
    )


def _safety_reason(concept: HabitConcept, value: Any) -> str | None:
    if concept.safety_route and value in {"观察到", "偶尔"}:
        return "observed_breathing_signal"
    text = str(value or "").lower()
    for marker, reason in (
        ("胸痛", "urgent_chest_pain"),
        ("呼吸困难", "urgent_breathing_difficulty"),
        ("无法唤醒", "urgent_unresponsive"),
        ("跌倒", "urgent_fall"),
        ("chest pain", "urgent_chest_pain"),
        ("cannot breathe", "urgent_breathing_difficulty"),
    ):
        if marker in text:
            return reason
    return None


def _reviewed_concept_version(
    catalog: Mapping[str, HabitConcept],
    concept_id: str,
    concept_version: str,
) -> HabitConcept | None:
    current = catalog.get(concept_id)
    if current is not None and current.version == concept_version:
        return current
    return next(
        (
            item
            for item in REVIEWED_HABIT_CONCEPTS
            if (item.concept_id, item.version) == (concept_id, concept_version)
        ),
        None,
    )


def capture_habit_answers(
    state: HabitQuestionState,
    answers: Iterable[HabitAnswer],
    *,
    episode_id: str,
    subject_id: str,
    actor_id: str,
    role: HabitRole,
    now: datetime,
    catalog: Mapping[str, HabitConcept] = HABIT_CONCEPTS,
) -> tuple[HabitEvidence, ...]:
    """验证 receipt 后规范化回答；Safety 命中后立即停止剩余 capture。"""

    _require_aware(now, "Habit capture time")
    if (
        state.phase != "selected"
        or state.receipt_hash != _contract_hash(state, "receipt_hash")
    ):
        raise ValueError("Habit selection receipt is forged")
    if (state.episode_id, state.subject_id, state.actor_id, state.role) != (
        episode_id, subject_id, actor_id, role
    ):
        raise ValueError("Habit selection binding mismatch")
    if (
        state.issued_at is None
        or state.expires_at is None
        or not state.issued_at <= now < state.expires_at
    ):
        raise ValueError("Habit selection expired")
    issued = dict(state.selected_concepts)
    captured: list[HabitEvidence] = []
    seen: set[str] = set()
    for index, answer in enumerate(answers):
        if answer.concept_id in seen or answer.concept_id not in issued:
            raise ValueError("Habit answer is duplicate or was not selected")
        seen.add(answer.concept_id)
        if issued[answer.concept_id] != answer.concept_version:
            raise ValueError("Habit answer version does not match selection receipt")
        concept = _reviewed_concept_version(
            catalog,
            answer.concept_id,
            answer.concept_version,
        )
        if concept is None:
            raise ValueError("Habit concept version is no longer reviewed")
        if role == "family" and concept.respondent != _OBSERVER:
            raise PermissionError("family cannot answer elder-only Habit concept")
        if role == "family" and answer.disposition == HabitDisposition.NEVER_ASK:
            raise PermissionError("only the elder may suppress a Habit question")
        value = _normalize_answer(concept, answer)
        disposition = answer.disposition
        if (
            role == "family"
            and concept.negative_requires_direct_observation
            and value == "没有观察到"
            and not answer.direct_observation
        ):
            disposition, value = HabitDisposition.UNKNOWN, None
        safety_reason = _safety_reason(concept, value)
        identity = {
            "selection_id": state.selection_id,
            "index": index,
            "answer": answer.model_dump(mode="json"),
        }
        unsigned = HabitEvidence(
            evidence_id=f"habit-evidence:{_stable_hash(identity)[:32]}",
            evidence_hash="0" * 64,
            selection_id=state.selection_id or "",
            episode_id=episode_id,
            subject_id=subject_id,
            actor_id=actor_id,
            role=role,
            concept_id=concept.concept_id,
            concept_version=concept.version,
            disposition=disposition,
            value=value,
            origin="elder_self_report" if role == "elder" else "family_observation",
            observation_date_start=answer.observation_date_start,
            observation_date_end=answer.observation_date_end,
            timezone_name=answer.timezone_name,
            day_type=answer.day_type,
            direct_observation=answer.direct_observation,
            observation_description=answer.observation_description,
            observation_confidence=answer.observation_confidence,
            profile_eligible=(
                concept.profile_eligible
                and disposition in _PROFILE_VALUES
                and safety_reason is None
                and not _clinical(value)
            ),
            safety_reason=safety_reason,
            suppressed_until=(
                now + timedelta(days=365)
                if disposition == HabitDisposition.NEVER_ASK
                else None
            ),
            captured_at=now,
            valid_until=now + timedelta(hours=24),
        )
        captured.append(
            unsigned.model_copy(
                update={"evidence_hash": _contract_hash(unsigned, "evidence_hash")}
            )
        )
        if safety_reason:
            break
    return tuple(captured)


class HabitFact(SleepDomainContract):
    """Append-only Profile revision；expire/forget 使用无值 tombstone。"""

    fact_id: str
    fact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    operation: HabitOperation
    subject_id: str
    concept_id: str
    concept_version: str
    value: Any = None
    value_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    unit: str | None = None
    evidence: HabitEvidence | None = None
    confirmed_at: datetime
    valid_until: datetime
    confirmation_ref: str
    change_id: str
    replaces_fact_id: str | None = None


class HabitChange(SleepDomainContract):
    change_id: str
    change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: HabitOperation
    subject_id: str
    concept_id: str
    concept_version: str
    evidence: HabitEvidence | None = None
    target_fact_id: str | None = None
    expected_profile_version: int = Field(ge=0)
    confirmation_actor_id: str = Field(min_length=1)
    created_at: datetime
    confirmation_expires_at: datetime

    @model_validator(mode="after")
    def validate_operation(self) -> "HabitChange":
        if self.confirmation_expires_at <= self.created_at:
            raise ValueError("Habit confirmation window is invalid")
        if self.operation == HabitOperation.REMEMBER:
            if self.evidence is None or self.target_fact_id is not None:
                raise ValueError("remember requires evidence and no target")
        elif self.operation == HabitOperation.CORRECT:
            if self.evidence is None or self.target_fact_id is None:
                raise ValueError("correct requires evidence and target")
        elif self.evidence is not None or self.target_fact_id is None:
            raise ValueError("expire/forget require only a target")
        if self.evidence is not None:
            if (
                self.evidence.subject_id != self.subject_id
                or self.evidence.concept_id != self.concept_id
                or self.evidence.concept_version != self.concept_version
            ):
                raise ValueError("Habit change and evidence binding mismatch")
            if not self.evidence.evidence_id.startswith("habit-evidence:"):
                raise ValueError("Habit Profile rejects non-Habit evidence")
        if any(value.startswith("memory:") for value in (self.change_id, self.target_fact_id or "")):
            raise ValueError("Habit and Longitudinal Memory cannot share change identity")
        return self


class HabitConfirmation(SleepDomainContract):
    confirmation_id: str
    actor_id: str = Field(min_length=1)
    actor_role: Literal["elder"]
    subject_id: str
    target_change_id: str
    target_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_at: datetime
    expires_at: datetime


class HabitProfileState(SleepDomainContract):
    subject_id: str
    version: int = Field(default=0, ge=0)
    revisions: tuple[HabitFact, ...] = ()

    @model_validator(mode="after")
    def require_unique_revision_ids(self) -> "HabitProfileState":
        if len({item.fact_id for item in self.revisions}) != len(self.revisions):
            raise ValueError("Habit Profile repeats a fact revision")
        return self

    def current(self, now: datetime) -> tuple[HabitFact, ...]:
        replaced = {item.replaces_fact_id for item in self.revisions if item.replaces_fact_id}
        return tuple(
            item
            for item in self.revisions
            if item.fact_id not in replaced
            and item.operation not in {HabitOperation.EXPIRE, HabitOperation.FORGET}
            and now < item.valid_until
        )

    def stale_concept_ids(self, now: datetime) -> tuple[str, ...]:
        replaced = {item.replaces_fact_id for item in self.revisions if item.replaces_fact_id}
        return tuple(sorted({
            item.concept_id
            for item in self.revisions
            if item.fact_id not in replaced
            and item.operation not in {HabitOperation.EXPIRE, HabitOperation.FORGET}
            and now >= item.valid_until
        }))

    def disputed_concept_ids(self, now: datetime) -> tuple[str, ...]:
        values: dict[str, set[str]] = {}
        for item in self.current(now):
            values.setdefault(item.concept_id, set()).add(item.value_hash)
        return tuple(sorted(key for key, hashes in values.items() if len(hashes) > 1))


def propose_habit_change(
    *,
    operation: HabitOperation,
    subject_id: str,
    concept_id: str,
    expected_profile_version: int,
    confirmation_actor_id: str,
    now: datetime,
    evidence: HabitEvidence | None = None,
    target_fact_id: str | None = None,
    confirmation_ttl: timedelta = timedelta(minutes=30),
    catalog: Mapping[str, HabitConcept] = HABIT_CONCEPTS,
) -> HabitChange:
    """把短期证据变成待 elder 精确确认的单一 Profile 变更。"""

    _require_aware(now, "Habit proposal time")
    concept = catalog.get(concept_id)
    if concept is None or not concept.profile_eligible:
        raise ValueError("concept is not eligible for Habit Profile")
    if evidence is not None:
        if evidence.evidence_hash != _contract_hash(evidence, "evidence_hash"):
            raise ValueError("Habit evidence hash mismatch")
        if not evidence.profile_eligible or evidence.valid_until <= now:
            raise ValueError("Habit evidence is not active or Profile eligible")
        if evidence.safety_reason or _clinical(evidence.value):
            raise ValueError("clinical or Safety evidence cannot enter Habit Profile")
    material = {
        "operation": operation.value,
        "subject_id": subject_id,
        "concept_id": concept_id,
        "evidence_id": evidence.evidence_id if evidence else None,
        "target_fact_id": target_fact_id,
        "expected_profile_version": expected_profile_version,
        "confirmation_actor_id": confirmation_actor_id,
        "created_at": now,
    }
    unsigned = HabitChange(
        change_id=f"habit-change:{_stable_hash(material)[:32]}",
        change_hash="0" * 64,
        operation=operation,
        subject_id=subject_id,
        concept_id=concept_id,
        concept_version=concept.version,
        evidence=evidence,
        target_fact_id=target_fact_id,
        expected_profile_version=expected_profile_version,
        confirmation_actor_id=confirmation_actor_id,
        created_at=now,
        confirmation_expires_at=now + confirmation_ttl,
    )
    return unsigned.model_copy(
        update={"change_hash": _contract_hash(unsigned, "change_hash")}
    )


def apply_confirmed_habit_change(
    state: HabitProfileState,
    change: HabitChange,
    confirmation: HabitConfirmation,
    *,
    now: datetime,
) -> HabitProfileState:
    """校验 exact-target elder 确认并追加 revision，绝不原地改写旧事实。"""

    _require_aware(now, "Habit confirmation time")
    expected_hash = _contract_hash(change, "change_hash")
    if change.change_hash != expected_hash:
        raise ValueError("Habit change hash mismatch")
    exact_confirmation = (
        confirmation.actor_id == change.confirmation_actor_id
        and confirmation.subject_id == change.subject_id
        and confirmation.target_change_id == change.change_id
        and confirmation.target_hash == change.change_hash
        and confirmation.expires_at == change.confirmation_expires_at
        and change.created_at <= confirmation.approved_at <= now
        and now < confirmation.expires_at
    )
    if not exact_confirmation:
        raise PermissionError("Habit confirmation is not exactly bound or active")
    if (
        state.subject_id != change.subject_id
        or state.version != change.expected_profile_version
    ):
        raise ValueError("Habit Profile subject/version is stale")
    if any(
        item.fact_hash != _contract_hash(item, "fact_hash")
        for item in state.revisions
    ):
        raise ValueError("Habit Profile revision hash mismatch")
    replaced = {item.replaces_fact_id for item in state.revisions if item.replaces_fact_id}
    by_id = {item.fact_id: item for item in state.revisions}
    target = by_id.get(change.target_fact_id or "")
    if change.target_fact_id:
        if target is None or target.fact_id in replaced:
            raise ValueError("Habit mutation target is missing or inactive")
        if target.subject_id != change.subject_id or target.concept_id != change.concept_id:
            raise ValueError("Habit mutation target binding mismatch")
    if change.operation == HabitOperation.REMEMBER and change.evidence is not None:
        same_source = any(
            item.concept_id == change.concept_id
            and item.evidence is not None
            and item.evidence.actor_id == change.evidence.actor_id
            for item in state.current(now)
        )
        if same_source:
            raise ValueError("existing Habit fact requires explicit correction")
    evidence = change.evidence
    value = evidence.value if evidence else None
    unit = None
    if isinstance(value, dict) and set(value) == {"value", "unit"}:
        value, unit = value["value"], value["unit"]
    concept = HABIT_CONCEPTS[change.concept_id]
    valid_until = evidence.captured_at + timedelta(days=concept.valid_for_days) if evidence else now
    value_hash = (
        _stable_hash({"concept_id": change.concept_id, "value": value, "unit": unit})
        if evidence
        else target.value_hash if target else _stable_hash(None)
    )
    revision = target.revision + 1 if target else 1
    identity = {"change_id": change.change_id, "revision": revision}
    unsigned = HabitFact(
        fact_id=f"habit-fact:{_stable_hash(identity)[:32]}",
        fact_hash="0" * 64,
        revision=revision,
        operation=change.operation,
        subject_id=change.subject_id,
        concept_id=change.concept_id,
        concept_version=change.concept_version,
        value=value,
        value_hash=value_hash,
        unit=unit,
        evidence=evidence,
        confirmed_at=now,
        valid_until=valid_until,
        confirmation_ref=confirmation.confirmation_id,
        change_id=change.change_id,
        replaces_fact_id=target.fact_id if target else None,
    )
    fact = unsigned.model_copy(
        update={"fact_hash": _contract_hash(unsigned, "fact_hash")}
    )
    return HabitProfileState(
        subject_id=state.subject_id,
        version=state.version + 1,
        revisions=(*state.revisions, fact),
    )


__all__ = [
    "CATALOG_VERSION",
    "HABIT_CONCEPTS",
    "MAX_EPISODE_QUESTIONS",
    "REVIEWED_HABIT_CONCEPTS",
    "HabitAnswer",
    "HabitAnswerType",
    "HabitChange",
    "HabitConcept",
    "HabitConceptStatus",
    "HabitConfirmation",
    "HabitDisposition",
    "HabitEvidence",
    "HabitFact",
    "HabitOperation",
    "HabitProfileState",
    "HabitQuestionState",
    "HabitRespondent",
    "apply_confirmed_habit_change",
    "capture_habit_answers",
    "propose_habit_change",
    "select_habit_questions",
]
