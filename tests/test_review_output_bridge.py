"""
review_output_bridge.py のテスト。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch


def _load_module():
    target = Path(__file__).resolve().parent.parent / "scripts" / "review_output_bridge.py"
    spec = importlib.util.spec_from_file_location("review_output_bridge_module", target)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bridge_mod = _load_module()


class TestReviewOutputBridge:
    def test_parse_legacy_markdown_auto_fixable_and_judgment(self):
        markdown = """
## 自動修正可
### quoted shell invocation
- severity: warning
- auto_fixable: true
- 何が起きるか: shell quoted subprocess is fragile
- 対策案:
  - 対象: hooks/review-feedback-session-check.js:42
  - 変更内容: use execFileSync

## 要確認
severity: warning
auto_fixable: false
問題: command contract mismatch
詳細: producer と skill の契約をそろえる必要がある
判断ポイント: machine block を必須にするか
─────────────────────────────
"""
        items = bridge_mod.parse_review_output(markdown, auto_fix_status="fixed")
        assert len(items) == 2
        assert items[0]["status"] == "fixed"
        assert items[0]["file_path"] == "hooks/review-feedback-session-check.js"
        assert items[0]["line"] == 42
        assert items[1]["status"] == "judgment-required"
        assert items[1]["needs_judgment"] is True

    def test_parse_legacy_judgment_block_captures_target_when_present(self):
        markdown = """
## 要確認
severity: warning
問題: retry budget policy
詳細: latency と durability の tradeoff
対象: src/worker.py:121
判断ポイント: 3回 retry にするか 5回にするか
─────────────────────────────
"""
        items = bridge_mod.parse_review_output(markdown)
        assert len(items) == 1
        assert items[0]["file_path"] == "src/worker.py"
        assert items[0]["line"] == 121

    def test_parse_machine_block_takes_precedence(self):
        markdown = """
## 自動修正可
### ignored
- severity: warning
- 何が起きるか: ignored

```review-outcome-json
[
  {"type": "finding", "summary": "from machine", "severity": "high", "status": "pending", "confidence": "high"}
]
```
"""
        items = bridge_mod.parse_review_output(markdown)
        assert len(items) == 1
        assert items[0]["summary"] == "from machine"

    def test_parse_legacy_markdown_accepts_capitalized_english_keys(self):
        markdown = """
## Auto-fixable
### fragile command quoting
- Severity: warning
- What happens: quoting breaks on special chars
- Target: scripts/run.py:9

## Requires confirmation
- Severity: warning
- Issue: retry budget policy
- Detail: latency と durability の tradeoff
- Target: src/worker.py:121
- Decision point: 3回 retry にするか 5回にするか
"""
        items = bridge_mod.parse_review_output(markdown, auto_fix_status="pending")
        assert len(items) == 2
        assert items[0]["severity"] == "warning"
        assert items[0]["file_path"] == "scripts/run.py"
        assert items[1]["severity"] == "warning"
        assert items[1]["file_path"] == "src/worker.py"
        assert items[1]["line"] == 121

    def test_parse_legacy_markdown_infers_path_from_text_and_target_files(self):
        markdown = """
## Auto-fixable
### install explanation should match install.ps1 behavior
- Severity: warning
- What happens: fresh fork users can misread README and think install.ps1 ships complete test skills

## Requires confirmation
- Severity: warning
- Issue: quickstart dispatch expectation is still ambiguous
- Detail: docs/quickstart-from-fork.md should say hook block is immediate but full dispatch depends on SKILL.md placement
"""
        items = bridge_mod.parse_review_output(
            markdown,
            auto_fix_status="pending",
            target_files=["README.md", "install.ps1", "docs/quickstart-from-fork.md"],
        )
        assert len(items) == 2
        assert items[0]["file_path"] == "install.ps1"
        assert items[1]["file_path"] == "docs/quickstart-from-fork.md"

    def test_parse_legacy_markdown_uses_single_target_file_fallback(self):
        markdown = """
## Auto-fixable
### local validation guidance is too weak
- severity: warning
- 何が起きるか: fresh fork users cannot tell what local validation to run first
"""
        items = bridge_mod.parse_review_output(
            markdown,
            auto_fix_status="pending",
            target_files=["README.md"],
        )
        assert len(items) == 1
        assert items[0]["file_path"] == "README.md"

    def test_parse_legacy_markdown_matches_target_hint_from_title(self):
        markdown = """
## Auto-fixable
### quickstart dispatch guidance is still ambiguous
- severity: warning
- 何が起きるか: first-time users may not realize dispatch is incomplete without separate skills
"""
        items = bridge_mod.parse_review_output(
            markdown,
            auto_fix_status="pending",
            target_files=["README.md", "docs/quickstart-from-fork.md"],
        )
        assert len(items) == 1
        assert items[0]["file_path"] == "docs/quickstart-from-fork.md"

    def test_main_prints_payload_json(self, capsys):
        markdown = """
## Auto-fixable
### fragile command quoting
- severity: warning
- What happens: quoting breaks on special chars
- Target: scripts/run.py:9
"""
        with patch(
            "sys.argv",
            [
                "review_output_bridge.py",
                "--input-text",
                markdown,
                "--reviewer",
                "sc-rfl",
                "--repo-root",
                "C:/repo",
                "--runtime",
                "codex",
            ],
        ):
            rc = bridge_mod.main()
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["reviewer"] == "review-fix-loop"
        assert payload["items"][0]["file_path"] == "scripts/run.py"

    def test_main_forwards_to_pdca(self, capsys):
        ok = subprocess.CompletedProcess(args=["python"], returncode=0, stdout='{"ok":true}', stderr="")
        markdown = """
## 自動修正可
### quoted shell invocation
- severity: warning
- 何が起きるか: shell quoted subprocess is fragile
"""
        with patch.object(bridge_mod, "forward_to_producer", return_value=ok) as mock_forward:
            with patch.object(bridge_mod, "resolve_producer_path", return_value="C:/pdca/scripts/record-review-outcome.py"):
                with patch(
                    "sys.argv",
                    [
                        "review_output_bridge.py",
                        "--input-text",
                        markdown,
                        "--reviewer",
                        "sc-ifr",
                        "--forward-to-pdca",
                        "--classify-patterns",
                    ],
                ):
                    rc = bridge_mod.main()
        captured = capsys.readouterr()
        assert rc == 0
        assert '{"ok":true}' in captured.out
        assert mock_forward.call_args.kwargs["classify_patterns"] is True

    def test_main_warns_when_repo_root_looks_like_bridge_repo(self, capsys):
        markdown = """
## 自動修正可
### quoted shell invocation
- severity: warning
- 何が起きるか: shell quoted subprocess is fragile
"""
        with patch.object(bridge_mod, "forward_to_producer", return_value=subprocess.CompletedProcess(args=["python"], returncode=0, stdout='{"ok":true}', stderr="")):
            with patch.object(bridge_mod, "resolve_producer_path", return_value="C:/pdca/scripts/record-review-outcome.py"):
                with patch.object(bridge_mod, "detect_repo_root", return_value=bridge_mod.normalize_path(str(bridge_mod.Path(__file__).resolve().parent.parent))):
                    with patch(
                        "sys.argv",
                        [
                            "review_output_bridge.py",
                            "--input-text",
                            markdown,
                            "--reviewer",
                            "sc-ifr",
                            "--forward-to-pdca",
                        ],
                    ):
                        rc = bridge_mod.main()
        captured = capsys.readouterr()
        assert rc == 0
        assert "review-fix-pipeline 自身を指しています" in captured.err
