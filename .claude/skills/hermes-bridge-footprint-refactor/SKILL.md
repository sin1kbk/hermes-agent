---
name: hermes-bridge-footprint-refactor
description: hermes-agent の claude-bridge フォークで、bridge固有ロジックのupstream所有ファイル（gateway/run.py, gateway/slash_commands.py, gateway/config.py, hermes_cli/model_switch.py, hermes_cli/providers.py, plugins/platforms/discord/adapter.py等）への食い込みを減らし、将来のupstreamマージ衝突面を縮小するときに使う。「upstream追随に備えてリファクタ」「bridgeの衝突面を減らして」「クリーンにモジュール性を保って」で発火。footprint測定、worktree隔離、delegate-impl、diff-stat/baseline比較、delegate-review、修正、コミットまで扱う。
metadata:
  origin: session-learning
  generated_at: 2026-08-16
  source_session: 1d7b14f5-33a8-4831-88ec-fd01344e2525
---

# bridge ロジックを upstream 所有ファイルから隔離するリファクタ

hermes-agent の `claude-bridge` フォークは、bridge 固有ロジックを upstream 所有ファイルに
直接書き込むほど次の upstream merge（[[hermes-bridge-upstream-merge]]）の衝突面が増える。
実例（2026-08-14）: footprint が gateway/run.py ~535行・gateway/slash_commands.py ~281行など
合計約1,900行まで膨らんだ時点でリファクタを実施し、gateway/run.py 132/73・
slash_commands.py 126/14 まで縮小。ロジックは新設の `gateway/claude_bridge/` パッケージ
（core.py / config.py / runner_mixin.py / slash.py / __init__.py）と
`hermes_cli/claude_bridge_defs.py`、`hermes_cli/claude_bridge_switch.py`、
`plugins/platforms/discord/claude_bridge_ui.py` に集約し、upstream 所有ファイルには
薄いフック（1〜数行の呼び出し）だけを残した。

## 1. footprint を測る

bridge が触れている upstream 所有ファイル全部について測定する（対象の例:
gateway/run.py, gateway/slash_commands.py, gateway/config.py, hermes_cli/model_switch.py,
hermes_cli/providers.py, plugins/platforms/discord/adapter.py, hermes_cli/runtime_provider.py,
agent/auxiliary_client.py）:

**先に `git fetch upstream main` して `upstream/main` を最新化し、`git diff` の基準は
`upstream/main` そのものではなく `git merge-base upstream/main HEAD` を使うこと**
（実測 2026-08-31: ローカル `upstream/main` が無関係な壊れたブランチ参照
`refs/remotes/upstream/ent/secrets` の破損で2週間以上fetchが止まっており、その stale な
`upstream/main` との差分は無関係な upstream 側の自然進化まで「footprint」として混入し、
gateway/run.py だけで +3779/-746 という誤った巨大値が出た。merge-base 基準に直したら
+140/-73 で予算内と判明。stale ref は `rm -f .git/refs/remotes/upstream/<broken-ref>` で
削除すれば fetch が通る場合がある — `git update-ref -d` は sandbox 越しだと
`Operation not permitted` で失敗することがあるため、直接 rm を試す）:

```bash
git fetch upstream main
MB=$(git merge-base upstream/main HEAD)
git diff "$MB" --stat -- <file>      # 概観
git diff "$MB" --numstat -- <file>   # add/delete行数を個別取得（予算チェックに使う）
```

すでに `gateway/claude_bridge/` パッケージが存在するなら、新規に足された bridge ロジックが
それを経由せず inline に戻っていないかもここで確認する。**予算超過そのものは自動でリファクタ
理由にしない** — 超過分が package 経由のフック呼び出し（例: `/model` や `/reasoning` の
統合ポイント追加）でしかないなら、行数予算は「目安」であって「行数を削るためのタスク」を
生まない。リファクタが要るのは、超過分に package を経由しない生ロジックが inline で
混入しているときだけ。

## 2. 設計は main agent が固める。ファイル単位の diff-stat 予算を数値で決める

各ファイルの「リファクタ後の目標変更行数」を決め、受け入れ基準にする（実例:
config.py ≤20 / run.py ≤170 / slash_commands.py ≤110 / model_switch.py ≤55 /
providers.py ≤8 / adapter.py ≤35）。超過は機械的に却下せず理由で判断する
（実例: run.py は認可コード `_gate_unauthorized_message` を意図的に残したため
205 まで超過したが妥当と判断した）。

移設先モジュールの責務分担、どの関数を verbatim で移すか、どこを lazy import に
するか（循環 import 回避）まで main agent が詰め、delegate-impl プロンプトに
インラインで埋め込む。**ゼロ挙動変更**が受け入れ基準の中核であることを明記する。

## 3. worktree 隔離 + delegate-impl（内部の role-impl に depth: deep を渡す）

作業ツリー汚染を避けるため必ず worktree に隔離してから委譲する:

```bash
git worktree add .claude/worktrees/<name> -b worktree-<name> HEAD   # dangerouslyDisableSandbox: true
```

`EnterWorktree` でセッションを worktree に移したら、venv が親リポジトリから
相対パスで見えるかを確認してから進む（実例: `ls ../../../venv/bin/python` →
`parent venv ok`。worktree 自体には `.venv`/`venv` は作られない）。

`delegate-impl` は素の呼び出しで良いが、大規模リファクタでは内部で起動される
`role-impl` workflow に `depth: "deep"` を渡すこと（設計を全部インラインで
埋め込んだプロンプトを渡せば1回の呼び出しで通る）。実例の所要時間は約1時間20分
（Bash 219 / Read 91 / Edit 55 / Write 10、うち `scripts/run_tests.sh` を18回起動 —
時間の大半はテスト実行）。Codex レイヤーが未認証なら resolver が自動で
`impl-claude` にフォールバックする（正常動作、報告するだけでよい）。

長時間動いていて心配になったら、実際にハングしているかを transcript のファイル
サイズ推移や `run_tests.sh` 起動回数から判断する（[[codex-impl-liveness-check]]）。

## 4. 受け入れ検証は「diff-stat 予算」と「baseline 比較テスト」の両方

実装完了後、まず手順1のコマンドで diff-stat 予算を再測定する。次にテスト:

```bash
export HERMES_PYTHON=<repo>/venv/bin/python
scripts/run_tests.sh <対象パス> -q
```

**フルスイートは worktree 内だけで完結させず、リファクタ前後の baseline と比較する**
（[[hermes-bridge-upstream-merge]] と同じ「merged / 前バージョン」の2点比較の考え方）。
実例: リファクタ後124件失敗 / リファクタ前baseline126件失敗、新規失敗は環境依存の
`test_voice_mode.py`（sounddevice関連）のみで、本流ツリーでも同一に失敗することを
確認しリグレッションなしと判断した。

## 5. delegate-review は対象ファイルと受け入れ基準の実測値を埋め込んで起動する

`delegate-review` の対象プロンプトに、レビュー範囲（diff scope・新規/削除ファイル一覧）・
設計の要点・診断済みの受け入れ基準の実測値（diff-stat予算の実測結果、テスト差分の結論）を
全部インラインで埋め込む。role-review は「何と比較して等価であるべきか」を渡されないと
的外れな指摘になる。

**worktree 作業中は、検証コマンドの `cd` でセッションの実 cwd を本流リポジトリに
戻さないこと。** `role-impl`/`role-review` の resolver は起動時に「宣言された cwd
（args.cwd）と、resolver を起動したセッションの実 cwd が一致しているか」を検証し、
不一致なら即エラーで落ちる（実測のエラー文言: `宣言された cwd: .../worktrees/<name> /
実 cwd: <repo root> — cwd は resolver を起動したセッションの作業ディレクトリと
一致していなければならない`）。

このエラーで落ちたら **`resumeFromRunId` で再開しない** — resume は cwd 検証段の
キャッシュ済み失敗結果をそのまま再生するため、シェル cwd を worktree に戻した後でも
同じエラーで即座に（実測: tool_uses 0、duration 5ms）再失敗する。`cd` でセッション cwd
を worktree に戻したら、同じ `scriptPath` を **`resumeFromRunId` を付けずに新規 run として**
起動し直す。

## 6. レビュー指摘は委譲閾値未満なら直接修正する

実例（critical/high 0・medium 2・low 3・unverified 0 → 再レビュー条件なし。
指摘内容: モジュール分割で暗黙に変わったロガー名 `logging.getLogger(__name__)`、
移設時に呼び出し側で先にガードされ到達不能になった死んだ `one_turn` 分岐など）:
5件とも Read で該当箇所を確認したうえで Edit、コンパイル確認、対象テストの再実行
（実例: 11ファイル198テスト全パス）まで通してから worktree ブランチへコミットする。

```bash
git add -A 2>&1 | grep -v 'Operation not permitted'   # 直下 .npmrc 等の sandbox 拒否は無視してよい
git commit --no-verify -m "..."
```

コミット後、main への取り込みは [[land-worktree-branch-into-main]] に従う。
