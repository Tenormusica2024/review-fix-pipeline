"""
enforce-go-robust-submit.py

UserPromptSubmit hook: /ifr, /rfl, /brutal-review の開始と /go-robust の実行、
および脱出口（--no-go-robust フラグ / /skip-go-robust-once コマンド）を
セッション単位で追跡する。

状態は ~/.claude/state/go-robust-enforce/<session_id>.json に保存し、
Stop hook (enforce-go-robust-stop.py) が読み取って要確認未処理時に
次応答を block する。

state schema:
{
  "session_id": "...",
  "last_review_command": {"name": "ifr"|"rfl"|"brutal-review", "ts": "ISO"} | null,
  "last_go_robust": "ISO" | null,
  "bypass_once": bool,
  "enforced_count": int  // 同一サイクル内で block した回数
}
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# pythonw guard
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


STATE_DIR = Path.home() / ".claude" / "state" / "go-robust-enforce"

# レビューコマンド検出（行頭マッチ、大小無視）
# /ifr, /rfl, /brutal-review および正式名 /intent-first-review, /review-fix-loop
REVIEW_CMD_PATTERN = re.compile(
    r"^\s*/(ifr|rfl|brutal-review|intent-first-review|review-fix-loop)\b",
    re.IGNORECASE | re.MULTILINE,
)

# /go-robust 実行検出
GO_ROBUST_PATTERN = re.compile(r"^\s*/go-robust\b", re.IGNORECASE | re.MULTILINE)

# 脱出口: one-shot バイパスコマンド
SKIP_ONCE_PATTERN = re.compile(
    r"^\s*/skip-go-robust-once\b", re.IGNORECASE | re.MULTILINE
)

# 脱出口: レビューコマンドに付けるフラグ
NO_ENFORCE_FLAG = re.compile(r"--no-go-robust\b", re.IGNORECASE)

# コマンド名正規化（正式名 → 短縮名）
CMD_NORMALIZE = {
    "intent-first-review": "ifr",
    "review-fix-loop": "rfl",
}


def _default_state(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "last_review_command": None,
        "last_go_robust": None,
        "bypass_once": False,
        "enforced_count": 0,
    }


def load_state(session_id: str) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{session_id}.json"
    if not path.exists():
        return _default_state(session_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # state壊れてたら作り直す（サイレント失敗を避けるためstderrに記録）
        sys.stderr.write(
            f"[enforce-go-robust-submit] state file corrupted, reset: {path}\n"
        )
        return _default_state(session_id)


def save_state(session_id: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{session_id}.json"
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        # 入力がJSONでなければ何もしない
        return 0

    prompt = data.get("prompt") or ""
    session_id = data.get("session_id") or ""
    if not session_id or not prompt:
        return 0

    state = load_state(session_id)
    now = datetime.now(timezone.utc).isoformat()
    changed = False

    # 脱出口: /skip-go-robust-once または --no-go-robust
    if SKIP_ONCE_PATTERN.search(prompt) or NO_ENFORCE_FLAG.search(prompt):
        state["bypass_once"] = True
        changed = True

    # /go-robust 実行検出（レビューサイクルのクロージング）
    if GO_ROBUST_PATTERN.search(prompt):
        state["last_go_robust"] = now
        # 新しいレビューサイクルを開始するまで enforced_count はリセットしない
        # （go-robust が終わればそのサイクルは完了なので）
        changed = True

    # レビューコマンド実行検出
    m = REVIEW_CMD_PATTERN.search(prompt)
    if m:
        cmd = m.group(1).lower()
        cmd = CMD_NORMALIZE.get(cmd, cmd)
        state["last_review_command"] = {"name": cmd, "ts": now}
        # 新規サイクル開始なので enforcement カウンタをリセット
        state["enforced_count"] = 0
        changed = True

    if changed:
        save_state(session_id, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
