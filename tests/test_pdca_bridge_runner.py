from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import patch


def _load_module():
    target = Path(__file__).resolve().parent.parent / "scripts" / "pdca_bridge_runner.py"
    spec = importlib.util.spec_from_file_location("pdca_bridge_runner_module", target)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


runner_mod = _load_module()


class TestPDCABridgeRunner:
    def test_validate_requires_repo_root_for_forward(self):
        parser = runner_mod.make_parser()
        args = parser.parse_args(
            [
                "--kind",
                "output",
                "--input-text",
                "x",
                "--reviewer",
                "sc-ifr",
                "--forward-to-pdca",
            ]
        )
        try:
            runner_mod.validate_args(args)
            assert False, "expected ValueError"
        except ValueError as e:
            assert "--repo-root" in str(e)

    def test_build_command_for_output(self):
        parser = runner_mod.make_parser()
        args = parser.parse_args(
            [
                "--kind",
                "output",
                "--input-text",
                "## Auto-fixable",
                "--reviewer",
                "sc-ifr",
                "--repo-root",
                "C:/target",
                "--forward-to-pdca",
            ]
        )
        cmd = runner_mod.build_command(args)
        assert str(Path("scripts/review_output_bridge.py")).replace("\\", "/") in cmd[1].replace("\\", "/")
        assert "--input-text" in cmd
        assert "--repo-root" in cmd
        assert "--forward-to-pdca" in cmd

    def test_build_command_for_findings(self):
        parser = runner_mod.make_parser()
        args = parser.parse_args(
            [
                "--kind",
                "findings",
                "--findings-json",
                "[]",
                "--reviewer",
                "sc-rfl",
                "--repo-root",
                "C:/target",
                "--status",
                "fixed",
                "--confidence",
                "high",
            ]
        )
        cmd = runner_mod.build_command(args)
        assert str(Path("scripts/review_feedback_bridge.py")).replace("\\", "/") in cmd[1].replace("\\", "/")
        assert "--findings-json" in cmd
        assert "--status" in cmd

    def test_main_runs_downstream_script(self, capsys):
        ok = subprocess.CompletedProcess(args=["python"], returncode=0, stdout='{"ok":true}', stderr="")
        with patch.object(runner_mod.subprocess, "run", return_value=ok) as mock_run:
            with patch(
                "sys.argv",
                [
                    "pdca_bridge_runner.py",
                    "--kind",
                    "items",
                    "--items-json",
                    "[]",
                    "--reviewer",
                    "sc-gr",
                    "--repo-root",
                    "C:/target",
                    "--forward-to-pdca",
                ],
            ):
                rc = runner_mod.main()
        captured = capsys.readouterr()
        assert rc == 0
        assert '{"ok":true}' in captured.out
        invoked = mock_run.call_args.args[0]
        assert "review_outcome_contract.py" in invoked[1].replace("\\", "/")
