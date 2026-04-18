"""
enforce-go-robust-submit.py

UserPromptSubmit hook: /ifr, /rfl の開始と /go-robust の実行、
および脱出口（--no-go-robust フラグ / /skip-go-robust-once コマンド）を
セッション単位で追跡する。

状態は ~/.claude/state/go-robust-enforce/<session_id>.json に保存し、
Stop hook (enforce-go-robust-stop.py) が読み取って要確認未処理時に
次応答を block する。

state schema:
{
  "session_id": "...",
  "last_review_command": {"name": "ifr"|"rfl", "ts": "ISO"} | null,
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

# session_id に使える文字（ファイル名として安全な集合のみ許可）。
# 許可外の文字が入っていればパストラバーサル回避のため state 処理を拒否する。
SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

# 先頭スラッシュコマンドの検出（プロンプト全体の最初の非空行のみを対象にする）。
# MULTILINE 検索だと本文中の引用・例示にも反応して state 遷移が誤発火するため、
# 「この応答でユーザーが実際にコマンドを発行した」ことを示す先頭行に限定する。
FIRST_LINE_CMD_PATTERN = re.compile(
    r"^\s*/(ifr|rfl|intent-first-review|review-fix-loop|go-robust|skip-go-robust-once)\b(.*)$",
    re.IGNORECASE,
)

# 脱出口: レビューコマンドに付けるフラグ（プロンプト全体に出現してよい）
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


def _state_path(session_id: str) -> Path | None:
    """session_id から state ファイルのパスを組み立てる。

    session_id が安全な文字集合に収まらない場合は None を返す（パストラバーサル回避）。
    さらに最終パスが STATE_DIR 配下に収まっていることも resolve() 後に検証する。
    """
    if not SAFE_SESSION_ID_PATTERN.match(session_id):
        sys.stderr.write(
            f"[enforce-go-robust-submit] unsafe session_id rejected: {session_id!r}\n"
        )
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = (STATE_DIR / f"{session_id}.json").resolve()
    try:
        path.relative_to(STATE_DIR.resolve())
    except ValueError:
        sys.stderr.write(
            f"[enforce-go-robust-submit] state path escaped STATE_DIR: {path}\n"
        )
        return None
    return path


def load_state(session_id: str) -> dict:
    path = _state_path(session_id)
    if path is None or not path.exists():
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
    path = _state_path(session_id)
    if path is None:
        return
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


REVIEW_COMMANDS = frozenset(
    {"ifr", "rfl", "intent-first-review", "review-fix-loop"}
)


def _first_command(prompt: str) -> tuple[str, str] | None:
    """プロンプトの最初の非空行がスラッシュコマンドなら (コマンド名, 引数部分) を返す。

    本文中の引用・説明（例: "次回は /ifr を実行したい" といった自然言語）を
    state 遷移トリガーにしないため、先頭行のみを判定対象とする。
    引数部分は --no-go-robust フラグ検査をレビューコマンド行に限定するために返す。
    """
    for line in prompt.splitlines():
        if not line.strip():
            continue
        m = FIRST_LINE_CMD_PATTERN.match(line)
        if m:
            return m.group(1).lower(), m.group(2) or ""
        return None
    return None


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

    parsed = _first_command(prompt)
    cmd, cmd_args = (parsed if parsed is not None else (None, ""))

    # 脱出口: 先頭コマンド /skip-go-robust-once、または
    # 先頭のレビューコマンドに付いた --no-go-robust フラグ。
    # プロンプト全文検索にすると README やコマンド例の引用で誤発火するため、
    # レビューコマンド行の引数部分に限定する。
    is_bypass_flag = cmd in REVIEW_COMMANDS and bool(NO_ENFORCE_FLAG.search(cmd_args))
    if cmd == "skip-go-robust-once" or is_bypass_flag:
        state["bypass_once"] = True
        changed = True

    # /go-robust 実行検出（レビューサイクルのクロージング）
    if cmd == "go-robust":
        state["last_go_robust"] = now
        # 新しいレビューサイクルを開始するまで enforced_count はリセットしない
        # （go-robust が終わればそのサイクルは完了なので）
        changed = True

    # レビューコマンド実行検出
    if cmd in REVIEW_COMMANDS:
        normalized = CMD_NORMALIZE.get(cmd, cmd)
        state["last_review_command"] = {"name": normalized, "ts": now}
        # 新規サイクル開始なので enforcement カウンタをリセット
        state["enforced_count"] = 0
        changed = True

    if changed:
        save_state(session_id, state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
