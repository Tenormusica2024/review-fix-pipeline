"""
enforce-go-robust-stop.py

Stop hook: /ifr, /rfl が出した「要確認」を
/go-robust が処理する前にユーザーに返すのを防ぐ。

検知ロジック（structured state 主導・marker scoring は補助）:
1. UserPromptSubmit hook (enforce-go-robust-submit.py) が記録した
   session state を読む
2. last_review_command 以降に last_go_robust が走っていなければ未処理
3. かつ last_assistant_message に要確認マーカー（## 要確認 + severity + auto_fixable: false など）
   が含まれていれば、pending とみなす
4. pending なら decision:"block" で /go-robust 実行を強制

ループ防止:
- stop_hook_active (built-in) → pass（無限継続防止）
- bypass_once (ユーザーが /skip-go-robust-once または --no-go-robust) → 1回だけ pass
- enforced_count >= MAX_ENFORCE → pass（安全弁、同一サイクル内2回まで）
- /go-robust 自身の出力は検知対象外（last_review_command が /go-robust ではないため自動的に除外）
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# pythonw guard
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


STATE_DIR = Path.home() / ".claude" / "state" / "go-robust-enforce"

# session_id に使える文字（ファイル名として安全な集合のみ許可）。
# submit hook (_state_path) と同一ルールに揃えることで、Stop hook 側だけ
# 緩い文字集合を許すことによるパストラバーサルを防ぐ。
SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

# 同一レビューサイクル内での強制回数上限（安全弁）
MAX_ENFORCE = 2

# 要確認セクションの検出マーカー（IFR Step 4 フォーマット準拠）
# 公開 README に英語例 (## Requires confirmation) を載せている以上、日本語と英語の両方を受理する。
# どちらか片方だけだと README 例に従ったレビュー出力を Stop hook が検知できず /go-robust が発火しない。
MARKER_HEADER = re.compile(
    r"^\s*##\s*(?:要確認|Requires\s+confirmation)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
MARKER_SEVERITY = re.compile(
    r"^\s*severity\s*:\s*(critical|warning|info)",
    re.MULTILINE | re.IGNORECASE,
)
MARKER_AUTOFIX_FALSE = re.compile(
    r"^\s*auto_fixable\s*:\s*false", re.MULTILINE | re.IGNORECASE
)
# U+2500 BOX DRAWINGS LIGHT HORIZONTAL の連続（5文字以上）
MARKER_DIVIDER = re.compile(r"─{5,}")


def _state_path(session_id: str) -> Path | None:
    """session_id から state ファイルのパスを組み立てる。

    submit hook 側と同じ検証ロジック: 安全な文字集合に収まらない session_id は拒否し、
    resolve() 後に STATE_DIR 配下であることも確認してパストラバーサルを防ぐ。
    """
    if not SAFE_SESSION_ID_PATTERN.match(session_id):
        sys.stderr.write(
            f"[enforce-go-robust-stop] unsafe session_id rejected: {session_id!r}\n"
        )
        return None
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = (STATE_DIR / f"{session_id}.json").resolve()
    try:
        path.relative_to(STATE_DIR.resolve())
    except ValueError:
        sys.stderr.write(
            f"[enforce-go-robust-stop] state path escaped STATE_DIR: {path}\n"
        )
        return None
    return path


def load_state(session_id: str) -> dict | None:
    path = _state_path(session_id)
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        sys.stderr.write(
            f"[enforce-go-robust-stop] state file corrupted: {path}\n"
        )
        return None


def save_state(session_id: str, state: dict) -> None:
    path = _state_path(session_id)
    if path is None:
        return
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def has_pending_markers(text: str) -> bool:
    """
    last_assistant_message に『未処理の要確認』が含まれるかを判定する。

    判定基準:
    - `## 要確認` ヘッダー必須
    - 加えて以下のどちらか:
      - `severity:` 行 + `auto_fixable: false` 行 の両方
      - `─────` 区切り（IFR Step 4 フォーマットの区切り線）
    """
    if not text:
        return False

    has_header = bool(MARKER_HEADER.search(text))
    if not has_header:
        return False

    has_severity = bool(MARKER_SEVERITY.search(text))
    has_autofix_false = bool(MARKER_AUTOFIX_FALSE.search(text))
    has_divider = bool(MARKER_DIVIDER.search(text))

    return (has_severity and has_autofix_false) or has_divider


def build_block_reason(review_cmd: str) -> str:
    return (
        f"/{review_cmd} が出力した「要確認」がまだ処理されていません。\n"
        "ユーザーに返す前に /go-robust を実行し、"
        "5原則（堅牢性優先・安全側・検知可能・必要十分・AIエージェント可読性）で"
        "判断可能な項目をすべて処理してください。\n\n"
        "今回だけスキップしたい場合は次の入力で /skip-go-robust-once を実行するか、"
        "次回のレビューコマンドに --no-go-robust フラグを付けてください。"
    )


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0

    # 無限ループ防止（Claude Code built-in）
    if data.get("stop_hook_active"):
        return 0

    session_id = data.get("session_id") or ""
    if not session_id:
        return 0

    state = load_state(session_id)
    if state is None:
        # state なし = レビューコマンド未実行、何もしない
        return 0

    last_review = state.get("last_review_command")
    if not last_review:
        return 0

    last_review_ts = last_review.get("ts", "")
    last_go_robust = state.get("last_go_robust") or ""

    # レビュー後に /go-robust が既に走っていれば通す
    if last_go_robust and last_go_robust >= last_review_ts:
        return 0

    # 脱出口: bypass_once (one-shot) → 消費して通す
    if state.get("bypass_once"):
        state["bypass_once"] = False
        save_state(session_id, state)
        return 0

    # 安全弁: 同一サイクルで MAX_ENFORCE 回ブロック済みなら諦める。
    # 無限ループ防止のため pass するが、サイレントに諦めると気づきにくいので
    # stderr に警告を出して運用者が検知できるようにする。
    enforced_count = state.get("enforced_count", 0)
    if enforced_count >= MAX_ENFORCE:
        sys.stderr.write(
            f"[enforce-go-robust-stop] MAX_ENFORCE ({MAX_ENFORCE}) reached for session "
            f"{session_id!r}; giving up enforcement for this cycle\n"
        )
        return 0

    # 要確認マーカー検出
    last_msg = data.get("last_assistant_message") or ""
    if not has_pending_markers(last_msg):
        return 0

    # ブロック発動
    state["enforced_count"] = enforced_count + 1
    save_state(session_id, state)

    cmd_name = last_review.get("name", "review")
    output = {
        "decision": "block",
        "reason": build_block_reason(cmd_name),
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
