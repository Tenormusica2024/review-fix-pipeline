"""
review_feedback_bridge.py のテスト。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch


def _load_module():
    target = Path(__file__).resolve().parent.parent / "scripts" / "review_feedback_bridge.py"
    spec = importlib.util.spec_from_file_location("review_feedback_bridge_module", target)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


bridge_mod = _load_module()


class TestReviewFeedbackBridge:
    def test_build_items_maps_findings_to_fixed_items(self):
        findings = [
            {
                "summary": "quoted shell invocation",
                "severity": "warning",
                "category": "robustness",
                "file_path": "hooks/review-feedback-session-check.js",
                "line": 42,
            }
        ]
        items = bridge_mod.build_items(findings, status="fixed", confidence="high", needs_judgment=False)
        assert len(items) == 1
        assert items[0]["status"] == "fixed"
        assert items[0]["auto_fixable"] is True
        assert items[0]["file_path"] == "hooks/review-feedback-session-check.js"

    def test_build_items_can_mark_judgment_required(self):
        findings = [{"summary": "need product choice", "severity": "warning"}]
        items = bridge_mod.build_items(findings, status="judgment-required", confidence="medium", needs_judgment=True)
        assert items[0]["type"] == "judgment_call"
        assert items[0]["needs_judgment"] is True
        assert items[0]["auto_fixable"] is False

    def test_main_prints_payload_json(self, capsys):
        findings = [
            {
                "summary": "quoted shell invocation",
                "severity": "warning",
                "file_path": "src/app.py",
            }
        ]
        with patch(
            "sys.argv",
            [
                "review_feedback_bridge.py",
                "--findings-json",
                json.dumps(findings, ensure_ascii=False),
                "--reviewer",
                "review-fix-loop",
                "--repo-root",
                "C:/repo",
            ],
        ):
            rc = bridge_mod.main()
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)
        assert payload["items"][0]["status"] == "fixed"
        assert payload["items"][0]["file_path"] == "src/app.py"

    def test_main_forwards_to_pdca(self, capsys):
        ok = subprocess.CompletedProcess(args=["python"], returncode=0, stdout='{"ok":true}', stderr="")
        findings = [{"summary": "quoted shell invocation", "severity": "warning"}]
        with patch.object(bridge_mod, "forward_to_producer", return_value=ok) as mock_forward:
            with patch.object(bridge_mod, "resolve_producer_path", return_value="C:/pdca/scripts/record-review-outcome.py"):
                with patch(
                    "sys.argv",
                    [
                        "review_feedback_bridge.py",
                        "--findings-json",
                        json.dumps(findings, ensure_ascii=False),
                        "--reviewer",
                        "review-fix-loop",
                        "--forward-to-pdca",
                        "--classify-patterns",
                    ],
                ):
                    rc = bridge_mod.main()
        captured = capsys.readouterr()
        assert rc == 0
        assert '{"ok":true}' in captured.out
        assert mock_forward.call_args.kwargs["classify_patterns"] is True
