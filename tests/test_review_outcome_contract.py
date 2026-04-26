"""
review_outcome_contract.py のテスト。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch


def _load_module():
    target = Path(__file__).resolve().parent.parent / "scripts" / "review_outcome_contract.py"
    spec = importlib.util.spec_from_file_location("review_outcome_contract_module", target)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


contract_mod = _load_module()


class TestReviewOutcomeContract:
    def test_normalize_reviewer_aliases(self):
        assert contract_mod.normalize_reviewer("/ifr") == "intent-first-review"
        assert contract_mod.normalize_reviewer("sc-rfl") == "review-fix-loop"
        assert contract_mod.normalize_reviewer("sc-ir") == "intent-review-light"

    def test_normalize_item_makes_repo_relative_path(self):
        item = {
            "type": "finding",
            "summary": "quoted shell invocation",
            "severity": "warning",
            "file_path": "C:/repo/hooks/review-feedback-session-check.js",
            "status": "fixed",
            "confidence": "high",
        }
        normalized = contract_mod.normalize_item(item, "C:/repo")
        assert normalized["file_path"] == "hooks/review-feedback-session-check.js"
        assert normalized["status"] == "fixed"
        assert normalized["confidence"] == "high"

    def test_build_payload_normalizes_targets_and_items(self):
        payload = contract_mod.build_payload(
            reviewer="sc-ifr",
            repo_root="C:/repo",
            session_id="sess-1",
            runtime="codex",
            mode="normal",
            target_files=["C:\\repo\\src\\app.py", "src\\util.py"],
            verification_commands=["pytest -q"],
            verification_summary="ok",
            items=[
                {
                    "type": "finding",
                    "summary": "pending issue",
                    "severity": "warning",
                    "category": "logic",
                    "file_path": "C:/repo/src/app.py",
                    "status": "pending",
                    "confidence": "high",
                    "auto_fixable": False,
                    "needs_judgment": True,
                }
            ],
        )
        assert payload["reviewer"] == "intent-first-review"
        assert payload["target"]["files"] == ["C:/repo/src/app.py", "src/util.py"]
        assert payload["items"][0]["file_path"] == "src/app.py"
        assert payload["verification"]["commands"] == ["pytest -q"]

    def test_main_prints_payload_json(self, capsys):
        items = [
            {
                "type": "finding",
                "summary": "pending issue",
                "severity": "warning",
                "file_path": "src/app.py",
                "status": "pending",
                "confidence": "high",
            }
        ]
        with patch(
            "sys.argv",
            [
                "review_outcome_contract.py",
                "--items-json",
                json.dumps(items, ensure_ascii=False),
                "--reviewer",
                "sc-rfl",
                "--repo-root",
                "C:/repo",
                "--runtime",
                "codex",
            ],
        ):
            rc = contract_mod.main()
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["reviewer"] == "review-fix-loop"
        assert payload["runtime"] == "codex"
        assert payload["items"][0]["summary"] == "pending issue"

    def test_main_forwards_to_producer(self, capsys):
        ok = subprocess.CompletedProcess(args=["python"], returncode=0, stdout='{"ok":true}', stderr="")
        items = [
            {
                "type": "finding",
                "summary": "pending issue",
                "severity": "warning",
                "file_path": "src/app.py",
                "status": "pending",
                "confidence": "high",
            }
        ]
        with patch.object(contract_mod, "forward_to_producer", return_value=ok) as mock_forward:
            with patch(
                "sys.argv",
                [
                    "review_outcome_contract.py",
                    "--items-json",
                    json.dumps(items, ensure_ascii=False),
                    "--reviewer",
                    "sc-ifr",
                    "--producer-path",
                    "C:/pdca/scripts/record-review-outcome.py",
                ],
            ):
                rc = contract_mod.main()
        captured = capsys.readouterr()
        assert rc == 0
        assert '{"ok":true}' in captured.out
        forwarded_payload = mock_forward.call_args.args[1]
        assert forwarded_payload["reviewer"] == "intent-first-review"
