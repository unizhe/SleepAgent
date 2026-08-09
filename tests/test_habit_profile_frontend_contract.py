from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "frontend/components/habit/HabitProfileWorkspace.tsx"
CLIENT = ROOT / "frontend/lib/habit-profile-api.ts"
TYPES = ROOT / "frontend/lib/habit-profile-types.ts"
BFF = ROOT / "frontend/app/api/radar/[...path]/route.ts"
PAGE = ROOT / "frontend/app/habit-profile/page.tsx"
RADAR = ROOT / "frontend/components/radar/DynamicRadarWorkspace.tsx"
RADAR_TYPES = ROOT / "frontend/lib/radar-types.ts"


def test_habit_profile_is_discoverable_but_never_starts_automatically() -> None:
    workspace = WORKSPACE.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")
    radar = RADAR.read_text(encoding="utf-8")

    assert "HabitProfileWorkspace" in page
    assert 'href="/habit-profile"' in radar
    assert "可选：回答 2–3 个问题" in workspace
    assert "startOptionalHabitIntake" not in workspace[
        workspace.index("useEffect(() => {") : workspace.index("  }, []);")
    ]
    assert "loadHabitProfile()" in workspace


def test_habit_ui_separates_current_use_from_exact_persistence_confirmation() -> None:
    workspace = WORKSPACE.read_text(encoding="utf-8")
    client = CLIENT.read_text(encoding="utf-8")
    types = TYPES.read_text(encoding="utf-8")

    for copy in (
        "您可以随时跳过",
        "长期保存前会再次逐项显示并请您整体确认",
        "请确认是否长期保存",
        "不保存这一项",
        "暂不保存",
        "更正",
        "遗忘",
        "不再用于个性化",
        "不生成睡眠健康分、固定类型或诊断",
    ):
        assert copy in workspace
    confirm_client = client[
        client.index("export async function confirmHabitChangeSet") :
        client.index("export async function pruneHabitChangeSet")
    ]
    assert "decision_id: decisionId" in confirm_client
    assert "idempotency_key: idempotencyKey" in confirm_client
    for legacy_field in (
        "confirmation_id",
        "change_set_id",
        "change_set_version",
        "manifest_hash",
    ):
        assert legacy_field not in confirm_client
    assert "decisionId: pending.decision_id" in workspace
    assert "idempotencyKey: `habit-confirm:${pending.decision_id}`" in workspace
    assert "confirmationId" not in workspace
    assert types.count("decision_id: string;") >= 2
    assert "response.decision_id !== decisionId" in confirm_client
    assert 'response.tool_receipt.outcome !== "succeeded"' in client


def test_habit_bff_keeps_identity_and_api_key_server_side_and_fails_closed() -> None:
    bff = BFF.read_text(encoding="utf-8")
    client = CLIENT.read_text(encoding="utf-8")

    assert 'upstreamPath.startsWith("/product/habit-profile/")' in bff
    assert "SLEEPAGENT_HABIT_PROFILE_ACTOR_ID" in bff
    assert "SLEEPAGENT_HABIT_PROFILE_ACTOR_ROLE" in bff
    assert "SLEEPAGENT_HABIT_PROFILE_SUBJECT_ID" in bff
    assert '"X-Subject-Id": HABIT_PROFILE_SUBJECT_ID!' in bff
    assert "睡眠习惯画像身份尚未在服务端配置" in bff
    assert 'const BASE = "/api/radar/product/habit-profile"' in client
    assert "SLEEPAGENT_PRODUCT_RADAR_API_KEY" not in client
    assert "X-Actor-Id" not in client


def test_runtime_hitl_shows_accountability_and_disables_wrong_role() -> None:
    workspace = RADAR.read_text(encoding="utf-8")
    types = RADAR_TYPES.read_text(encoding="utf-8")

    for copy in (
        "为什么现在",
        "影响谁",
        "持续多久",
        "如何撤回",
        "精确变更",
        "当前身份不能作出这个决定",
        "批准这个精确版本",
    ):
        assert copy in workspace
    assert "pendingConfirmation.allowed_roles.includes(detail.task.role)" in workspace
    assert 'risk_level: "R0" | "R1" | "R2" | "R3" | "R4"' in types
    assert '"hard_blocked"' in types
