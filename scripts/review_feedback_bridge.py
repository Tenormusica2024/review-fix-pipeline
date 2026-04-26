#!/usr/bin/env python3
"""
review-feedback.py に渡す findings JSON を、そのまま共通 outcome contract /
PDCA producer へ接続する bridge。

主用途:
- /rfl Step 5 の既存 `review-feedback.py record --findings ...` と同じ JSON を再利用
- markdown parse を経由せずに PDCA へ保存する
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_outcome_contract import (  # type: ignore
    build_payload,
    detect_repo_root,
    forward_to_producer,
    normalize_path,
    resolve_producer_path,
    warn_if_repo_root_points_to_bridge_repo,
)


def load_findings(findings_json: str | None, findings_file: str | None) -> list[dict]:
    if findings_json:
        try:
            findings = json.loads(findings_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"--findings-json のJSONが不正: {e}") from e
    else:
        if not findings_file:
            raise ValueError("findings input is required")
        path = Path(findings_file)
        if not path.exists():
            raise ValueError(f"findings file が見つかりません: {findings_file}")
        try:
            findings = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"findings file のJSONが不正: {e}") from e

    if not isinstance(findings, list):
        raise ValueError("findings は配列である必要があります")
    if not all(isinstance(item, dict) for item in findings):
        raise ValueError("findings の各要素は object である必要があります")
    return findings


def normalize_finding_item(
    finding: dict,
    *,
    status: str,
    confidence: str,
    needs_judgment: bool,
) -> dict:
    severity = str(finding.get("severity") or "info").strip().lower()
    if severity not in {"critical", "high", "warning", "info", "nitpick"}:
        severity = "info"
    return {
        "type": "judgment_call" if needs_judgment else "finding",
        "title": str(finding.get("title") or finding.get("summary") or "").strip(),
        "summary": str(finding.get("summary") or "").strip(),
        "severity": severity,
        "category": str(finding.get("category") or "").strip(),
        "file_path": normalize_path(finding.get("file_path")),
        "line": finding.get("line"),
        "status": status,
        "auto_fixable": status == "fixed" and not needs_judgment,
        "needs_judgment": needs_judgment,
        "confidence": confidence,
    }


def build_items(
    findings: list[dict],
    *,
    status: str,
    confidence: str,
    needs_judgment: bool,
) -> list[dict]:
    return [
        normalize_finding_item(
            finding,
            status=status,
            confidence=confidence,
            needs_judgment=needs_judgment,
        )
        for finding in findings
        if str(finding.get("summary") or "").strip()
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge review-feedback findings JSON to shared outcome contract / PDCA producer")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--findings-json", help="findings JSON array")
    source.add_argument("--findings-file", help="findings JSON file")
    parser.add_argument("--reviewer", required=True, help="reviewer / skill name")
    parser.add_argument("--session-id", help="session id")
    parser.add_argument("--repo-root", help="repo root")
    parser.add_argument("--cwd", default=".", help="repo root detection fallback cwd")
    parser.add_argument("--runtime", default="unknown", help="claude-code / codex / unknown")
    parser.add_argument("--mode", default="normal", help="normal / review-only")
    parser.add_argument("--target-file", action="append", default=[], help="target file")
    parser.add_argument("--verification-command", action="append", default=[], help="verification command")
    parser.add_argument("--verification-summary", default="", help="verification summary")
    parser.add_argument("--producer-path", help="optional explicit PDCA producer path")
    parser.add_argument("--pdca-root", help="path to claude-review-pdca root")
    parser.add_argument("--forward-to-pdca", action="store_true", help="forward to downstream PDCA producer")
    parser.add_argument("--classify-patterns", action="store_true", help="pass --classify-patterns to downstream PDCA producer")
    parser.add_argument("--status", choices=["pending", "fixed", "judgment-required"], default="fixed", help="status assigned to findings")
    parser.add_argument("--confidence", choices=["low", "medium", "high"], default="high", help="confidence assigned to findings")
    parser.add_argument("--needs-judgment", action="store_true", help="mark all findings as judgment items")
    args = parser.parse_args()

    try:
        findings = load_findings(args.findings_json, args.findings_file)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    repo_root = normalize_path(args.repo_root) or detect_repo_root(args.cwd)
    warn_if_repo_root_points_to_bridge_repo(
        repo_root,
        forward_to_pdca=bool(args.producer_path or args.forward_to_pdca),
        explicit_repo_root=bool(args.repo_root),
    )
    items = build_items(
        findings,
        status=args.status,
        confidence=args.confidence,
        needs_judgment=args.needs_judgment,
    )
    payload = build_payload(
        reviewer=args.reviewer,
        items=items,
        session_id=args.session_id,
        repo_root=repo_root,
        runtime=args.runtime,
        mode=args.mode,
        target_files=args.target_file,
        verification_commands=args.verification_command,
        verification_summary=args.verification_summary,
    )

    producer_path = None
    if args.producer_path or args.forward_to_pdca:
        producer_path = resolve_producer_path(args.producer_path, pdca_root=args.pdca_root)
        if not producer_path:
            print(
                "Error: downstream PDCA producer が見つかりません。"
                " --producer-path / --pdca-root / PDCA_PRODUCER_PATH / CLAUDE_REVIEW_PDCA_ROOT を確認してください。",
                file=sys.stderr,
            )
            return 1

    if producer_path:
        result = forward_to_producer(
            producer_path,
            payload,
            cwd=args.cwd,
            classify_patterns=args.classify_patterns,
        )
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.stdout:
            print(result.stdout, end="")
        return result.returncode

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
