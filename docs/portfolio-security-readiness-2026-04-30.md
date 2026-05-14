# review-fix-pipeline 公開可否・セキュリティ確認メモ

作成日: 2026-04-30
対象 repo: `Tenormusica2024/review-fix-pipeline`

## 結論

条件付きでポートフォリオ掲載可能です。

この repo は、AIレビューと修正を分離するworkflow、Claude Code skill の正本管理、Codex/PDCA連携の review outcome contract という説明価値があります。一方で、実際の review outcome payload や過去レビュー結果には、未公開repo名・ローカルパス・作業ログ・秘密情報が混ざる可能性があるため、公開対象は抽象化した設計説明に限定する必要があります。

## 公開してよい内容

- AI自己レビュー偏りを避けるため、レビュー担当と修正担当を分離する設計
- `/ifr`、`/rfl`、`/go-robust` の役割
- intent-first review の考え方
- safe fix / unresolved / judgment call の分類方針
- Claude Code と Codex で同じ review outcome contract を使う設計
- `review-fix-pipeline` と `claude-review-pdca` の責務分離
- `public-portfolio-summary-ja.md` に書いた抽象説明・30秒説明・タイトル案

## 公開しない方がよい内容

- 実際のレビュー対象repoや未公開コードの詳細
- raw review output / raw finding / raw review outcome payload
- ローカルの `review-feedback.db` やPDCA連携DBの中身
- ユーザー固有のローカルパスを含む実データ
- API key、token、cookie、credential、env値
- クライアント名・業務名が入るレビュー例
- 秘密情報が混ざった可能性のある検証ログ全文

## llmwiki 候補理由への判断

`portfolio_review_candidates.json` では、この repo は以下の理由で候補化されていました。

- `baseline-stale`
- `readiness=needs-polish-before-pin`
- `security=medium`

今回の作業では、ポートフォリオ向けに「何のプロジェクトか」「何が普通のlint/CIと違うか」「claude-review-pdcaとどう分担するか」を説明できるドキュメントを追加しました。これにより `readiness=needs-polish-before-pin` の主要因である差別化説明不足は軽減されています。

`security=medium` は、候補データ上では `README.md` の credential 関連記述検知です。現時点では、credentialという語やcredentialを扱う設計説明の検知であり、即時に実シークレット露出を意味するものではありません。ただし、公開時は実review payloadやログを出さない運用が必須です。

## 実施した安全確認

- 作業前の `git status --short` は clean
- 追加は docs の公開説明・安全境界メモのみに限定
- 既存実装、skill本文、hook、DB処理には触れていない
- 実レビュー結果・実payload・個別repoの指摘は本文に含めていない

## 残る注意点

ポートフォリオへ出す場合は、`public-portfolio-summary-ja.md` の短い説明をベースにしてください。READMEやreview outcome contractの例をそのまま公開素材へ転用する場合は、repo名・file path・payload例が公開してよいダミーであることを確認してください。

## 次の一手

- この repo を公開候補として扱う場合は、ユーザー本人が掲載文面を確認する
- 実例を見せたい場合は、架空repo・架空file pathの toy example を別途作る
- llmwiki 側では、このレビュー結果を反映した baseline 更新を repo 単位で行う
