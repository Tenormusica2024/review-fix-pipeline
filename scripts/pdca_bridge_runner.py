#!/usr/bin/env python3
"""
PDCA bridge 実行を 1 コマンドに寄せる wrapper。

目的:
- markdown / findings / items のどれを持っていても同じ入口から流せる
- cross-repo forward 時に --repo-root 明示を促し、repo scope 誤記録を減らす
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_MAP = {
    "output": PROJECT_ROOT / "scripts" / "review_output_bridge.py",
    "findings": PROJECT_ROOT / "scripts" / "review_feedback_bridge.py",
    "items": PROJECT_ROOT / "scripts" / "review_outcome_contract.py",
}


def build_command(args: argparse.Namespace) -> list[str]:
    script = SCRIPT_MAP[args.kind]
    cmd = [sys.executable, str(script)]

    if args.kind == "output":
        if args.input_text is not None:
            cmd.extend(["--input-text", args.input_text])
        else:
            cmd.extend(["--input-file", args.input_file])
        cmd.extend(["--auto-fix-status", args.auto_fix_status])
    elif args.kind == "findings":
        if args.findings_json is not None:
            cmd.extend(["--findings-json", args.findings_json])
        else:
            cmd.extend(["--findings-file", args.findings_file])
        cmd.extend(["--status", args.status, "--confidence", args.confidence])
        if args.needs_judgment:
            cmd.append("--needs-judgment")
    else:
        if args.items_json is not None:
            cmd.extend(["--items-json", args.items_json])
        else:
            cmd.extend(["--items-file", args.items_file])

    cmd.extend(["--reviewer", args.reviewer])

    if args.session_id:
        cmd.extend(["--session-id", args.session_id])
    if args.repo_root:
        cmd.extend(["--repo-root", args.repo_root])
    if args.cwd:
        cmd.extend(["--cwd", args.cwd])
    if args.runtime:
        cmd.extend(["--runtime", args.runtime])
    if args.mode:
        cmd.extend(["--mode", args.mode])
    for value in args.target_file or []:
        cmd.extend(["--target-file", value])
    for value in args.verification_command or []:
        cmd.extend(["--verification-command", value])
    if args.verification_summary:
        cmd.extend(["--verification-summary", args.verification_summary])
    if args.producer_path:
        cmd.extend(["--producer-path", args.producer_path])
    if args.pdca_root:
        cmd.extend(["--pdca-root", args.pdca_root])
    if args.forward_to_pdca:
        cmd.append("--forward-to-pdca")
    if args.classify_patterns:
        cmd.append("--classify-patterns")

    return cmd


def validate_args(args: argparse.Namespace) -> None:
    if args.forward_to_pdca and not args.repo_root and not args.allow_bridge_repo_root:
        raise ValueError(
            "--forward-to-pdca 使用時は --repo-root を明示してください。"
            " review-fix-pipeline 自身への誤記録を避けるためです。"
            " review-fix-pipeline 自身を対象にするなら --allow-bridge-repo-root を付けてください。"
        )


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified PDCA bridge runner")
    parser.add_argument("--kind", choices=sorted(SCRIPT_MAP), required=True, help="output/findings/items")
    parser.add_argument("--reviewer", required=True, help="reviewer / skill name")
    parser.add_argument("--session-id", help="session id")
    parser.add_argument("--repo-root", help="actual target repo root")
    parser.add_argument("--cwd", default=".", help="cwd for downstream repo detection")
    parser.add_argument("--runtime", default="unknown", help="claude-code / codex / unknown")
    parser.add_argument("--mode", default="normal", help="normal / review-only")
    parser.add_argument("--target-file", action="append", default=[], help="target file")
    parser.add_argument("--verification-command", action="append", default=[], help="verification command")
    parser.add_argument("--verification-summary", default="", help="verification summary")
    parser.add_argument("--producer-path", help="optional explicit PDCA producer path")
    parser.add_argument("--pdca-root", help="path to claude-review-pdca root")
    parser.add_argument("--forward-to-pdca", action="store_true", help="forward to downstream PDCA producer")
    parser.add_argument("--classify-patterns", action="store_true", help="pass --classify-patterns downstream")
    parser.add_argument(
        "--allow-bridge-repo-root",
        action="store_true",
        help="allow review-fix-pipeline 自身を target repo として forward する",
    )

    parser.add_argument("--input-text", help="review markdown text (kind=output)")
    parser.add_argument("--input-file", help="review markdown file (kind=output)")
    parser.add_argument(
        "--auto-fix-status",
        choices=["pending", "fixed"],
        default="pending",
        help="status for legacy output auto-fixable items (kind=output)",
    )

    parser.add_argument("--findings-json", help="findings JSON array (kind=findings)")
    parser.add_argument("--findings-file", help="findings JSON file (kind=findings)")
    parser.add_argument("--status", choices=["pending", "fixed", "judgment-required"], default="fixed", help="status for findings items (kind=findings)")
    parser.add_argument("--confidence", choices=["low", "medium", "high"], default="high", help="confidence for findings items (kind=findings)")
    parser.add_argument("--needs-judgment", action="store_true", help="mark all findings as judgment items (kind=findings)")

    parser.add_argument("--items-json", help="items JSON array (kind=items)")
    parser.add_argument("--items-file", help="items JSON file (kind=items)")
    return parser


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()

    try:
        validate_args(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    cmd = build_command(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.stdout:
        print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
