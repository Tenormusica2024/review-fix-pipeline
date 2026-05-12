# Eval / Guardrail Design Playbook for AI Code Review Workflows

この文書は、`review-fix-pipeline` と `claude-review-pdca` を使って、
「問題が起きてから eval を足す」のではなく、**設計段階で eval / ガードレールをどう入れるか**を説明するための整理である。

対象読者は、Claude Code / Codex / AI coding agent を業務導入する企業、または面接で AI エージェント運用設計を評価する人を想定する。

---

## 1. 一言で言うと

この2 repo の設計思想は、以下で説明できる。

> AI がコードを書くこと自体ではなく、AI が出した変更を **別文脈で評価し、危険な修正を止め、学習した失敗を次回の実装前に再注入する** ための仕組みです。  
> 設計段階で「何を自動修正してよいか」「何を人間判断に戻すか」「何を再発防止ルールに昇格するか」を分けています。

企業向けには、次の言い方が一番通りやすい。

> AI コーディングを導入すると、実装速度は上がりますが、同じ文脈で自己レビューさせると見落としや過剰修正が残ります。  
> そこで、レビュー、修正、再レビュー、学習再注入を分け、各段階に eval とガードレールを置く設計にしています。

---

## 2. なぜこのプロジェクトを軸にするか

`Agent Workflow Reliability Dashboard` は説明用 UI としては有効だが、日本企業向けの実績説明では、やや抽象的・派手に見えやすい。

一方で `review-fix-pipeline` / `claude-review-pdca` は、企業が実際に気にする次の論点に直結する。

| 企業側の関心 | この設計で説明できること |
|---|---|
| AI 生成コードの品質評価 | `ifr` / `rfl` による intent-first review と fresh-context re-review |
| Claude Code / Codex の業務導入 | runtime は違っても review outcome contract を共通化する設計 |
| eval pipeline | review result を structured outcome として保存し、次回実装に戻す |
| HITL | false positive / judgment call / rule promotion を人間承認に残す |
| ガードレール | safe fix / critical verification / false-positive early exit / repo scope isolation |
| Hooks / 権限設計 | `claude-review-pdca` の PreToolUse / PostToolUse / SessionEnd injection |
| LLMOps / 継続改善 | findings DB と learned patterns DB による PDCA loop |

---

## 3. 設計段階で入れる eval / ガードレールの型

### 型0: まず「AIに任せる範囲」を分ける

設計時に最初に決めることは、モデル名やツールではなく、作業を4種類に分けること。

| 区分 | 例 | 自動化方針 |
|---|---|---|
| deterministic check | lint, type check, import error, unit test | 自動実行してよい |
| safe fix | 明確なtypo、壊れた参照、局所的な堅牢化 | 自動修正候補 |
| judgment call | API設計、UX、仕様変更、互換性判断 | 人間確認または go-robust 原則で限定処理 |
| forbidden / high-risk | 秘密情報、破壊的変更、本番操作、広範囲rewrite | block / approval 必須 |

この分類を先に作ることで、「AIにどこまで任せるか」を説明できる。

---

### 型1: Review Outcome Contract を eval surface にする

`review-fix-pipeline` 側では、レビュー結果を単なる自然文ではなく、意味を持つ項目に分ける。

主要フィールド:

- `severity`
- `category`
- `auto_fixable`
- `needs_judgment`
- `status`
- `file_path`
- `summary`
- `reviewer`
- `runtime`

設計上の意味:

> eval 対象を「レビュー文の良し悪し」ではなく、**どの指摘が、どの深刻度で、どの処理経路に進むべきか**に置く。

企業向け説明:

> レビュー結果を markdown の感想で終わらせず、後段の自動修正・人間確認・再注入に使える contract にしています。これにより、Claude Code と Codex のように runtime が違っても、品質評価の意味を揃えられます。

---

### 型2: Reviewer / Fixer を別文脈に分ける

`review-fix-pipeline` の中核は、reviewer と fixer を同じコンテキストにしないこと。

```text
変更差分
  -> fresh reviewer が intent-first review
  -> main context が safe fix のみ適用
  -> 別の fresh reviewer が再レビュー
  -> clean になるまで bounded loop
```

設計上のガードレール:

- 自分で書いたコードを同じ文脈で甘くレビューしない
- reviewer の blind spot を fixer に持ち込まない
- loop は最大回数を持つ
- false positive が増えたら止める

企業向け説明:

> AI に実装とレビューを同じ会話で続けさせると、意図を知っているため見落としやすくなります。そこで reviewer と fixer を意図的に分け、再レビューも新しい文脈で行うようにしています。

---

### 型3: `critical` は実測確認してから直す

`critical` finding は強い言葉なので、そのまま修正すると危険。

設計ルール:

- `critical` はまず最小再現で確認する
- Python なら `python -c`、Node なら `node -e` などで実測する
- 実測できないものは、原則として設計判断に戻す

企業向け説明:

> 重大指摘ほど、AIの過剰反応で壊すリスクがあります。critical はそのまま修正せず、最小再現で本当に起きることを確認してから修正する設計にしています。

---

### 型4: safe fix と judgment call を分ける

`auto_fixable: true` は、AI が勝手に直してよいという意味ではない。  
正確には、**設計判断なしに局所修正できる**という意味。

| 項目 | 自動化可否 | 理由 |
|---|---|---|
| import漏れ | 可 | 意図を変えない |
| 明確なNameError | 可 | 実測確認しやすい |
| API仕様変更 | 不可 | 利用者影響がある |
| セキュリティ方針変更 | 不可 | 権限/責任判断が必要 |
| UI文言の好み | 原則不可 | ユーザー意図依存 |

企業向け説明:

> 自動修正の条件を「AIが自信ありと言ったか」ではなく、「設計判断を含まない局所修正か」で分けています。

---

### 型5: false-positive early exit を置く

AIレビューを回し続けると、後半は実バグではなくノイズを拾い始める。

設計ルール:

- loopごとに false positive の比率を見る
- 一定以上なら loop を止める
- 「5回全部回す」より「品質が落ち始めたら止める」

企業向け説明:

> eval は多ければよいわけではありません。モデルが実問題を拾い切った後は、ノイズを修正して逆に品質を下げることがあるため、false positive rate を停止条件にしています。

---

### 型6: PDCA DB に保存し、次回実装前に再注入する

`claude-review-pdca` は、review 結果を保存して終わりにしない。

```text
Plan  : 編集前に、対象ファイルに関係する過去 findings を注入
Do    : AI が実装
Check : review-fix-pipeline がレビュー/修正/再レビュー
Act   : 新しい findings / learned patterns を DB に戻す
```

設計上のガードレール:

- 全 findings を流さない
- 対象ファイルに関係するものだけ注入
- repo_root で別プロジェクトを混ぜない
- stale / low-severity / dismissed を除外
- false positive の dismissed は人間承認のみ

企業向け説明:

> レビュー結果を一回限りの指摘で終わらせず、次回同じファイルを編集する前に関連 findings だけを戻します。これにより、AI が同じ種類の失敗を繰り返す確率を下げています。

---

### 型7: rule promotion は HITL に残す

すべての finding を `CLAUDE.md` や `CODEX.md` に書くと、ルールが肥大化して逆に使えなくなる。

設計ルール:

- DB pattern と repo rule を分ける
- durable / reusable / specific なものだけ rule candidate にする
- rule document への書き込みは human approval 必須
- global rule には勝手に昇格しない

企業向け説明:

> AIの失敗をすべてルール化すると運用できません。まずDBに保存し、繰り返し起きる・再利用価値がある・既存ルールと重複しないものだけ、人間承認でルールに昇格します。

---

## 4. 企業別に刺さる説明ポイント

| 企業/求人シグナル | 重点説明 | このrepoで見せる箇所 |
|---|---|---|
| デジライズ: Claude Code / CLAUDE.md / Skills / MCP | Claude Code導入時のレビュー・修正・ルール化の型 | `ifr` / `rfl` / `go-robust`, Skills, README, rule promotion |
| Randstad FDE: MCP / multi-agent / eval pipeline / observability | FDEが顧客環境に入れる品質管理ループ | review outcome contract, PDCA bridge, DB reinjection |
| GenerativeX: Agent SDK / MCP / evaluation / SRE / OpenTelemetry | agent workflow を評価・観測・改善する設計 | structured outcome, loop state, false-positive stop, persistence |
| エクサウィザーズ: Claude Code / Hooks / 権限設計 / AI生成コード品質評価 | AI駆動開発の安全なハーネス | fresh reviewer/fixer split, hooks, critical verification |
| Sansan: Remote MCP / 認証 / セキュリティ / Rate limit | 現状はやや間接的。直接狙うなら security/rate-limit scenario の追加が必要 | permission/rule境界の説明は可能だが主戦場ではない |
| SYSLEA: HITL / AI品質保証 / LLMOps / Claude Code / Codex | 最も相性が良い。HITLつきAI品質保証として説明 | dismissed HITL, file-specific reinjection, Codex/manual mode |

---

## 5. 面接での説明テンプレート

### 30秒版

```text
AIコーディングでは、速く実装できる一方で、同じ文脈で自己レビューすると見落としや過剰修正が起きます。
そこで私は、レビュー、修正、再レビュー、学習再注入を分ける小さな品質管理パイプラインを作りました。
レビュー結果は自然文で終わらせず、severity、auto_fixable、judgment call、file_path などの contract にして、次回実装前に関連 findings だけを戻す設計にしています。
```

### 90秒版

```text
設計段階では、まずAIに任せる範囲を deterministic check、safe fix、judgment call、high-risk action に分けています。
safe fix は自動修正できますが、仕様判断やセキュリティ方針は人間確認に戻します。
critical finding はそのまま直さず、最小再現で実測してから修正します。
また、レビューを回し続けると false positive が増えるので、false-positive rate を停止条件にしています。

さらに、レビュー結果をDBに保存し、次回同じファイルを編集するときに関連 findings だけを再注入します。
これにより、AIが同じ失敗を繰り返しにくくなります。
ただし、false positive の却下や repo rule への昇格は、人間承認を必須にしています。
```

### 「後付けevalでは？」と聞かれた時

```text
最初は問題が起きた後に eval を足す形が多かったです。
ただ、その経験から、設計段階で先に分類すべきポイントが見えてきました。
今は、どこを deterministic check にするか、どこを LLM judge にするか、どこを HITL に戻すか、どの finding を次回実装前に再注入するかを先に設計するようにしています。
この review-fix-pipeline / claude-review-pdca は、その設計思想を実装に落としたものです。
```

---

## 6. 今後足すとさらに企業向けに強くなるもの

現状でも説明材料として使えるが、企業向け証拠としてさらに強くするなら以下を追加する。

### P0: simple enterprise diagram

派手な dashboard ではなく、以下だけを1枚で示す。

```text
Edit -> Inject past findings -> Implement -> Review -> Safe fix -> Fresh re-review -> Store findings -> Rule promotion
```

### P1: sample review outcome JSON

架空の小さなサンプル repo に対して、次を含む JSON を置く。

- `auto_fixable: true`
- `needs_judgment: true`
- `severity: critical` + `verified_by_repro: true`
- `resolution: fixed`
- `pdca_reinjection_candidate: true`

### P2: eval metrics page

README か docs に、以下のような小さな表を追加する。

| metric | meaning |
|---|---|
| true finding rate | review finding のうち実修正された割合 |
| false-positive stop count | loop停止に使った回数 |
| reinjection hit count | 次回実装時に役立った finding 数 |
| HITL retained count | 人間判断に残した件数 |

### P3: security / permission scenario

Sansan / Remote MCP / 認証 / rate limit 方向を狙うなら、別途 scenario を足す。

例:

- external API tool call guard
- rate limit breach prevention
- credential leak check
- tenant boundary check

これは現状の主戦場ではないため、まずは code quality eval / HITL / PDCA を主軸にする。

---

## 7. このプロジェクトで言わない方がいいこと

避ける表現:

- 「AIが自律的に全部直します」
- 「レビューをたくさん回すので安全です」
- 「Claude Codeを使い倒しています」
- 「後から問題が起きたのでevalを足しました」

代わりに言う表現:

- 「自動修正できるものと人間判断に戻すものを分けています」
- 「レビュー結果を構造化して後段の処理に使います」
- 「critical は実測確認してから直します」
- 「false positive が増えたら止めます」
- 「過去 findings を対象ファイルに限定して再注入します」

---

## 8. 最短の結論

企業向けにこの2 repoを説明するなら、軸はこれでよい。

> `review-fix-pipeline` は、AI生成コードを別文脈で評価し、安全に修正するための eval / fix loop。  
> `claude-review-pdca` は、その結果を保存し、次回実装前に必要な findings だけを戻す PDCA / LLMOps layer。  
> 2つを合わせて、AIコーディングを「速い個人技」ではなく「設計可能な品質管理プロセス」にする。
