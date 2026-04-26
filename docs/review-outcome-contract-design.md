# Review Outcome Contract 設計

## 目的

`/ifr` `/rfl` と、その Codex 側相当である `sc-ifr` `sc-rfl` `sc-ir` が、
**同じ review outcome contract** を出力できるようにする。

この repo は review semantics の正本であり、
`claude-review-pdca` 側の producer / persistence と接続する前提で
「レビュー結果の意味」をここで固定する。

---

## repo の責務

### `review-fix-pipeline`

担当:
- `/ifr` `/rfl` `/go-robust` の意味論
- safe fix / unresolved / judgment call の判定ルール
- reviewer 名の正規化
- structured review outcome payload の source of truth

### `claude-review-pdca`

担当:
- payload を `review-feedback.db` / `review-patterns.db` に分流
- implementation session での再注入
- PDCA producer / consumer の保存・注入実装

原則:
- **レビューの意味はこの repo**
- **保存と再注入は `claude-review-pdca`**

---

## 設計方針

## 1. Claude/Codex で contract を分けない

Claude Code と Codex は runtime が違うが、review result の意味は同じに保つ。

共通にしたいもの:
- severity
- auto_fixable
- needs_judgment
- status
- category
- file_path
- summary

分けるもの:
- 実行方法
- hook の有無
- shell / PowerShell 差分
- sub-agent / codex exec / Agent ツール差分

つまり:
- **semantic contract は 1 本**
- **runtime adapter は複数本**

## 2. branch を runtime ごとに長期分岐しない

避けたい状態:
- Claude 用 branch
- Codex 用 branch

これを続けると、
- severity の解釈
- auto_fixable の基準
- 要確認の meaning
- payload shape

がズレる。

したがって:
- Claude/Codex で恒久 branch 分岐しない
- 差分は adapter と wrapper に閉じ込める

## 3. reviewer semantics は共通・reviewer 名は正規化

候補:

| 呼び出し | normalized reviewer |
|---|---|
| `/ifr` | `intent-first-review` |
| `sc-ifr` | `intent-first-review` |
| `/rfl` | `review-fix-loop` |
| `sc-rfl` | `review-fix-loop` |
| `sc-ir` | `intent-review-light` |

補足:
- `sc-ir` は軽量 critique なので `intent-first-review` とは分離して観測してよい
- ただし payload schema 自体は同一

---

## 共通 review outcome contract

想定 payload:

```json
{
  "schema_version": 1,
  "session_id": "sess-123",
  "repo_root": "C:/repo",
  "reviewer": "review-fix-loop",
  "runtime": "claude-code",
  "mode": "normal",
  "items": [
    {
      "type": "finding",
      "title": "quoted shell invocation for python helper",
      "summary": "shell-quoted subprocess call is fragile on paths with quotes or shell metacharacters",
      "severity": "warning",
      "category": "robustness",
      "file_path": "hooks/review-feedback-session-check.js",
      "line": 78,
      "status": "fixed",
      "auto_fixable": true,
      "needs_judgment": false,
      "confidence": "high"
    }
  ],
  "verification": {
    "commands": ["pytest -q"],
    "summary": "178 passed, 1 skipped"
  }
}
```

---

## skill ごとの期待値

## `/rfl` / `sc-rfl`

- ループあり
- safe fix を多く含む
- unresolved / fixed / 要確認 を比較的安定して分類できる

期待:
- contract 出力の主要 producer source になる

## `/ifr` / `sc-ifr`

- thorough review
- safe fix と要確認の両方を持つ

期待:
- `/rfl` と同じ contract で出す
- ただし loop 情報は任意

## `sc-ir`

- lightweight
- 全件保存するとノイズ化しやすい

期待:
- contract は同じ
- ただし downstream (`claude-review-pdca`) で stricter 保存ルールを適用

---

## runtime adapter 戦略

## Claude Code adapter

前提:
- slash command
- hook
- Agent
- Git Bash / POSIX shell 断片

責務:
- 既存 `/ifr` `/rfl` の振る舞いを維持
- structured outcome を抽出・出力できるようにする

## Codex adapter

前提:
- skill invocation
- PowerShell
- sub-agent 仕様差分
- hook 非依存

責務:
- review semantics を保ったまま Codex 実行に落とす
- 同じ contract payload を返す

---

## この repo で次に必要なこと

1. review output を free-text のみで終わらせず、
   **machine-readable block** を併記する方針を追加
2. `/ifr` `/rfl` から共通 payload を出せるようにする
3. その payload を `claude-review-pdca` の
   `record-review-outcome.py` に渡せるようにする

---

## branch 方針

推奨:
- この repo では `feat/review-outcome-contract` のような機能 branch を切る
- Claude/Codex の runtime 差分を branch ごとに恒久分離しない

つまり:
- **feature branch は切る**
- **runtime branch は切らない**

---

## 結論

この repo はすでに review logic の正本としてはかなり完成している。
ただし runtime は Claude Code / Git Bash 前提に寄っているため、
Codex 併用を前提にするなら次の更新が必要:

1. 共通 review outcome contract を正式化
2. Claude/Codex の差分を adapter 層に分離
3. `claude-review-pdca` と producer 契約で接続

これにより、
- review logic は 1 本
- runtime adapter は 2 本
- PDCA persistence は別 repo

という責務分離を保ったまま進化できる。
