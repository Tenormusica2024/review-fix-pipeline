from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "merge_parallel_reviews.py"

spec = importlib.util.spec_from_file_location("merge_parallel_reviews", MODULE_PATH)
assert spec is not None and spec.loader is not None
merge_parallel_reviews = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge_parallel_reviews)


def test_has_structured_review_markers_accepts_english_keys() -> None:
    text = """
Issue: Missing null guard on dashboard state
Detail: src/dashboard.py:42
Decision point: Treat missing payload as empty state
"""
    assert merge_parallel_reviews.has_structured_review_markers(text) is True


def test_parse_confirmation_item_supports_english_structured_review() -> None:
    text = """
Issue: Missing null guard on dashboard state
severity: warning
Decision point: Treat missing payload as empty state
Detail: src/dashboard.py:42
"""
    finding = merge_parallel_reviews._parse_confirmation_item(text, "codex")

    assert finding is not None
    assert finding["title"] == "Missing null guard on dashboard state"
    assert finding["file_path"] == "src/dashboard.py"
    assert finding["line"] == 42
    assert finding["severity"] == "warning"
    assert finding["auto_fixable"] is False
    assert finding["judgment"] == "Treat missing payload as empty state"
    assert finding["detected_by"] == "codex"


def test_merge_findings_combines_duplicate_models_conservatively() -> None:
    merged = merge_parallel_reviews.merge_findings(
        [
            {
                "title": "Missing null guard on dashboard state",
                "file_path": "src\\dashboard.py",
                "line": 40,
                "severity": "warning",
                "auto_fixable": True,
                "judgment": "",
                "detected_by": "opus",
                "raw_text": "a",
            },
            {
                "title": "Missing null guard on dashboard state branch",
                "file_path": "src/dashboard.py",
                "line": 42,
                "severity": "critical",
                "auto_fixable": False,
                "judgment": "Choose the empty-state fallback instead of raising.",
                "detected_by": "codex",
                "raw_text": "b",
            },
            {
                "title": "Missing null guard on dashboard state path",
                "file_path": "src/dashboard.py",
                "line": 41,
                "severity": "warning",
                "auto_fixable": True,
                "judgment": "",
                "detected_by": "glm",
                "raw_text": "c",
            },
        ],
        total_model_count=3,
    )

    assert len(merged) == 1
    finding = merged[0]
    assert finding["severity"] == "critical"
    assert finding["auto_fixable"] is False
    assert finding["detected_by"] == "all"
    assert finding["detection_count"] == 3
    assert finding["judgment"] == "Choose the empty-state fallback instead of raising."


def test_format_output_json_omits_raw_text() -> None:
    result = merge_parallel_reviews.format_output(
        [
            {
                "title": "Missing null guard on dashboard state",
                "file_path": "src/dashboard.py",
                "line": 42,
                "severity": "warning",
                "auto_fixable": False,
                "judgment": "Treat missing payload as empty state",
                "detected_by": "codex",
                "detection_count": 1,
                "raw_text": "debug only",
            }
        ],
        output_format="json",
    )

    assert "raw_text" not in result
    assert '"title": "Missing null guard on dashboard state"' in result


def test_format_output_markdown_keeps_sections_and_high_trust_marker() -> None:
    result = merge_parallel_reviews.format_output(
        [
            {
                "title": "Fix missing null guard",
                "file_path": "src/dashboard.py",
                "line": 42,
                "severity": "critical",
                "auto_fixable": True,
                "judgment": "",
                "detected_by": "all",
                "detection_count": 3,
                "raw_text": "debug only",
            },
            {
                "title": "Confirm fallback policy with product owner",
                "file_path": "src/dashboard.py",
                "line": 77,
                "severity": "warning",
                "auto_fixable": False,
                "judgment": "Use empty state unless analytics depends on a hard failure.",
                "detected_by": "codex+opus",
                "detection_count": 2,
                "raw_text": "debug only",
            },
            {
                "title": "Optional copy polish",
                "file_path": "src/dashboard.py",
                "line": 90,
                "severity": "info",
                "auto_fixable": True,
                "judgment": "",
                "detected_by": "codex",
                "detection_count": 1,
                "raw_text": "debug only",
            },
        ],
        output_format="markdown",
    )

    assert "## 自動修正可（1件）" in result
    assert "## 要確認（1件）" in result
    assert "## 対象外（Info以下）（1件）" in result
    assert "[高信頼]" in result
    assert "→ 方針: Use empty state unless analytics depends on a hard failure." in result
