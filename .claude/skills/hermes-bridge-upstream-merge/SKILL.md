---
name: hermes-bridge-upstream-merge
description: hermes-agent（sin1kbk fork、claude-bridge ブランチ）に upstream（NousResearch/hermes-agent）の最新 main を取り込むときに使う。「upstreamの最新mainをclaude-bridgeに取り込んで」「upstreamと同期して」で発火。sandbox 越しの fetch/merge、衝突の意味的解消、マージ由来の退行だけを detached worktree の baseline 差分で切り分ける検証までを扱う。
metadata:
  origin: session-learning
  generated_at: 2026-08-14
  source_session: 1d7b14f5-33a8-4831-88ec-fd01344e2525
---

# upstream/main を claude-bridge へ取り込む

hermes-agent の `claude-bridge`（origin: sin1kbk/hermes-agent）は
upstream（NousResearch/hermes-agent）の fork で、定期的に upstream の main を
merge で取り込む。実例: 2026-08-14 に upstream 1970 commits（merge-base
`a6defd4f1` → `1b1975781`）を取り込み、衝突2ファイル2ハンクを解消してコミット
`5b507a867` を作成。

## 1. fetch とマージ前チェック

**`git fetch upstream` は必ず `dangerouslyDisableSandbox: true` で実行する。**
upstream に `ent/secrets` というブランチが実在し、`refs/remotes/upstream/ent/secrets`
の書き込みが sandbox の `**/secrets` deny パターンに当たって
`failed to store: ... bad object refs/remotes/upstream/ent/secrets` で中断する
（ブランチ名を絞った `git fetch upstream main` でも同じ refspec 処理で再現する
ので、絞り込みでは回避できない）。

```bash
git fetch upstream 2>&1 | tail -5   # dangerouslyDisableSandbox: true
```

ahead/behind と merge-base を先に確認する:

```bash
git rev-list --left-right --count claude-bridge...upstream/main
git log --oneline -1 $(git merge-base claude-bridge upstream/main)
```

衝突を先読みしたいときは `land-worktree-branch-into-main` の 1b節と同じ
`git merge-tree --write-tree --name-only` dry run が使える（今回は未コミットの
作業ツリーを守る必要がないので `git stash create` は不要、ブランチ名を直接渡す）:

```bash
git merge-tree --write-tree --name-only claude-bridge upstream/main > "$TMPDIR/mt.txt"
```

## 2. マージの実行

**`git merge upstream/main --no-edit` も `dangerouslyDisableSandbox: true` が要る。**
このリポジトリ直下の `.envrc` / `.npmrc` / `website/.npmrc` は sandbox の書き込み
deny に当たり、素の sandbox では `Operation not permitted` で止まる。

```bash
git merge upstream/main --no-edit 2>&1 | tail -20   # dangerouslyDisableSandbox: true
```

衝突ファイルの特定:

```bash
grep -n '^<<<<<<<\|^=======$\|^>>>>>>>' <衝突ファイル>
```

## 3. 衝突は「どちらかを採る」でなく両側の意図を読んで合成する

機械的に ours/theirs を選ばない。各衝突ハンクについて、**両側が何を変えたか**を
先に読む:

```bash
# bridge 側がこのファイルに何を足したか（merge-base からの差分）
git diff $(git merge-base HEAD MERGE_HEAD) HEAD -- <file> | grep -n '^@@'
git diff $(git merge-base HEAD MERGE_HEAD) HEAD -- <file> | sed -n '<該当@@の前後>p'

# upstream 側が同じ範囲で何を変えたか
git diff $(git merge-base HEAD MERGE_HEAD) MERGE_HEAD -- <file> | grep -n '^@@'

# その関数/呼び出しの由来コミットを追う（改名・置き換えの経緯を掴む）
git log --oneline -S'<衝突箇所に出てくる固有のシンボル>' HEAD -- <file>
```

実例: `gateway/slash_commands.py` は bridge の「claude-bridge プロバイダでは
コストガードを飛ばす」分岐と、upstream 側で `expensive_model_warning` が
`combined_selection_warning` にリネーム統合された変更が衝突。呼び出し先だけ
upstream の新関数に差し替えて bridge の分岐は残した（`hermes_cli/model_selection_guards.py`
の呼び出し規約を先に読んで確認）。`gateway/run.py` は upstream が追加した
global emergency stop ゲートと bridge が抽出した `_gate_unauthorized_message()`
が近接しており、両方を意図した順序（認可ゲートの後に pause ゲート）で残した。

**統合した guard 関数が既存の分岐に暗黙のカバレッジ変化を持ち込んでいないかも
確認する** — 例えば「まとめてスキップ」にした結果、このフォーク固有の
provider/model id が新しく追加されたルールの対象に該当しないかをルールテーブル
自体を読んで裏取りする（`hermes_cli/model_data_policy_guard.py` のような
`(predicate, message)` テーブルを直接読む）。テストが通ることと意味的に等価
であることは別。

解消後、マーカー残りが無いことと構文を確認する:

```bash
rg -n '^<<<<<<< |^>>>>>>> ' --glob '!*.orig' .
git diff --name-only --diff-filter=U
python -m py_compile <解消したファイル>
```

## 4. テストは `scripts/run_tests.sh` 経由、直接 `pytest` は使わない

AGENTS.md がファイル単位のサブプロセス隔離のため直接 pytest 実行を禁じている。
加えてシステム既定の python には pytest が入っていないため、リポジトリの venv
を明示するか `HERMES_PYTHON` を渡す:

```bash
scripts/run_tests.sh tests/gateway/ -q          # dangerouslyDisableSandbox: true
# または個別ファイルを直接:
./venv/bin/python -m pytest <path> -q -p no:cacheprovider
```

## 5. マージ由来の退行だけを baseline 差分で切り分ける

フルスイートは merge 前後どちらでも一定数失敗する（環境依存・flaky・provider
未設定など）。**失敗一覧をそのまま「壊した」と報告しない。** upstream 単体と
マージ前 bridge 単体を detached worktree で用意し、同じサブセットを同条件で
流して失敗ファイル名の集合を取る:

```bash
git worktree add --detach .claude/worktrees/upstream-baseline upstream/main   # dangerouslyDisableSandbox: true
git worktree add --detach .claude/worktrees/bridge-premerge <マージ前のHEAD SHA>

# 3本（merged=作業ツリー / upstream-baseline / bridge-premerge）それぞれで
# 同じテスト対象・同じランナーを実行し、FAILED行だけ抜き出してファイルに保存
grep -E '^FAILED' <ログ> | sed 's/ - .*//' | sort -u > <baseline>.failed
```

「マージ由来」と呼べるのは、**merged では落ちて、upstream-baseline と
bridge-premerge の両方では通っているテストだけ**。実例（今回）: フル
スイート126件中、この条件に合致したものは0件。残りは個別に切り分けた:
- ランナー自身が1回リトライして通った → flaky（`test_turn_lease`）
- 依存パッケージ未導入による不安定（`test_transcription_tools`、faster-whisper 無し）
- **リポジトリ全体を走査するテストが、削除に失敗して残っていた孤児 worktree
  ディレクトリ（下記6）まで拾って誤検出**（`test_no_unreviewed_bare_managed_runtime_lookups`）

使い終えた baseline worktree は必ず片付ける:

```bash
git worktree remove --force .claude/worktrees/upstream-baseline
git worktree remove --force .claude/worktrees/bridge-premerge
```

## 6. リポジトリ内に孤児 worktree を見つけたら

「マージ由来の失敗」を切り分けている途中で、`.claude/worktrees/<name>/...` への
パスを含む失敗が出たら、まずそれが生きた worktree か孤児かを確認する:

```bash
git branch -a --list '*<name>*'                          # ブランチは残っているか
cat .claude/worktrees/<name>/.git 2>/dev/null             # gitdir を指しているか
git -C .claude/worktrees/<name> status -s                 # "fatal: not a git repository" なら
                                                            # .git/worktrees/<name> 側のメタデータが
                                                            # 消えている（削除失敗の残骸）
git merge-base --is-ancestor worktree-<name> HEAD && echo merged
```

merge 済みと分かっても、ディレクトリの削除は破壊的操作なので**このセッションで
勝手に `rm -rf` しない** — 状況（ブランチは取り込み済み・メタデータが壊れて
`git worktree remove` も効かない）を報告し、削除の許可を取ってから実行する。

## 7. コミット

hook の有無を確認してから判断する（このリポジトリはこの時点で `.git/hooks` に
有効なフックが無く `core.hooksPath` も未設定だったが、**それは事前に確認して
から**の話であって `--no-verify` を既定にしない）:

```bash
ls .git/hooks | grep -v sample
git config core.hooksPath
```

push は指示が無ければしない。
