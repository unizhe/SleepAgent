from __future__ import annotations

import ast
from pathlib import Path

def test_reference_client_is_independent_and_never_models_chat_context() -> None:
    source_path = (
        Path(__file__).parents[2]
        / "reference_client"
        / "sleep_api_v1_client.py"
    )
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any("sleepagent" in ast.unparse(node).lower() for node in imports)
    lowered = source.lower()
    for forbidden in ("chat_context", "conversation_history", "full_chat"):
        assert forbidden not in lowered
