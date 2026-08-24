---
name: hermes-bridge-land-and-deploy
description: hermes-agent の claude_bridge 変更を worktree ブランチから claude-bridge へ着地・push した後、この機体（s.kuboki機）で稼働中の gateway に反映して再起動するときに使う。「mergeしてpushして、この機体に反映して」「gatewayに反映して」で発火。
metadata:
  origin: session-learning
  generated_at: 2026-08-19
  source_session: d5085f99-6f14-4062-a97b-31ba6abdbdd6
---

# claude-bridge の変更をこの機体の稼働中 gateway へ反映する

## 背景

worktree でコミットした claude_bridge の変更を claude-bridge ブランチへ着地・push しても、
それだけではこの機体で動いている gateway プロセスには反映されない。push だけして反映を
省略すると、後から「実装したのに効いていない」という報告を受けて
[[hermes-bridge-busy-gate-diagnosis]] のような原因調査を余計に走らせることになる。

## 1. どの checkout が実際に動いているかを確認する（推測しない）

`~/.hermes/hermes-agent` のような「それらしい名前のディレクトリ」を実行系だと思い込まない
——実例: `~/.hermes/hermes-agent` は NousResearch/hermes-agent 本家の `main` を指す
別クローンで、gateway の実行には使われていなかった。実行系の確定は起動スクリプトか
実際のモジュール解決で行う:

```bash
grep real_hermes ~/.hermes/scripts/hermes-launch
# real_hermes="${HERMES_REAL_BIN:-$HOME/dev/sin1kbk/hermes-agent/venv/bin/hermes}"

/Users/s.kuboki/dev/sin1kbk/hermes-agent/venv/bin/python -c \
  "import gateway.claude_bridge as m; print(m.__file__)"
```

editable install なので、返るパスが手元の dev checkout（`~/dev/sin1kbk/hermes-agent` の
メイン worktree）を指せば、そこを更新すれば gateway に反映されると確定できる。

## 2. push 済みのブランチを、実行系のメイン worktree へ fast-forward する

worktree からの push は次の形で行う（fast-forward 可能なことは事前に
`git merge-base --is-ancestor claude-bridge HEAD` で確認しておく）:

```bash
git push origin HEAD:refs/heads/claude-bridge
```

push 出力に `failed to store: -25308` という行が混ざっても、続けて
`<old-sha>..<new-sha> HEAD -> claude-bridge` が出ていれば push 自体は成功している
（sandbox が拒否するローカル ref 由来の無害な警告 —
[[hermes-bridge-upstream-merge]] の `ent/secrets` と同系統）。

実行系のメイン worktree 側（`~/dev/sin1kbk/hermes-agent`）で追随する:

```bash
git status --short   # .envrc/.npmrc 等の "Operation not permitted" 警告は無視してよい
git fetch origin claude-bridge 2>&1 | tail -3
git merge-base --is-ancestor HEAD origin/claude-bridge && echo ff-safe
git merge --ff-only origin/claude-bridge   # dangerouslyDisableSandbox: true
```

`git fetch` / `git merge --ff-only` はこのリポジトリ直下の `.envrc` / `.npmrc` /
`website/.npmrc` の sandbox 書き込み拒否に当たるため `dangerouslyDisableSandbox: true`
が要る（[[hermes-bridge-upstream-merge]] と同じ制約）。

## 3. gateway を再起動して新しいコードを読ませる

```bash
launchctl kickstart -k user/501/ai.hermes-launch.gateway   # dangerouslyDisableSandbox: true
sleep 3
launchctl print user/501/ai.hermes-launch.gateway 2>&1 | head -6   # dangerouslyDisableSandbox: true
```

`launchctl kickstart` は sandbox越しだと `Could not kickstart service ...: 1: Operation not
permitted` で失敗する（launchctl の IPC がブロックされるため）。`dangerouslyDisableSandbox:
true` を付けて撃ち直す。kickstart 自体が exit 0 でも起動が成功したとは限らないので、
`launchctl print` の出力に `state = running` があることを確認してから完了と報告する。

## 4. worktree の後片付け

着地・反映まで終わった worktree は着地元セッションの手で消してよい:

```bash
git worktree remove .claude/worktrees/<name> --force
git worktree prune
git branch -d worktree-<name>
```

## 適用範囲

着地先が `claude-bridge` 以外のブランチでも、「push だけでは稼働中の gateway に
届かない・再起動が要る」という構造は同じ。1節の「実行系を推測せず確認する」は
claude_bridge 以外のこのリポジトリの変更を反映するときにも使える。gateway が
新しいコードを読み込んでいるか（mtime/pyc/ps 突合）の確認は
[[hermes-bridge-busy-gate-diagnosis]] の手順1と同じ。着地そのもの（worktree
コミット → ブランチへの merge/push の一般手順）は [[land-worktree-branch-into-main]]
を参照。
