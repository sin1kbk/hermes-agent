# CLAUDE.md — claude-bridge フォーク固有ルール

このファイルは fork 専有（upstream の NousResearch/hermes-agent には存在しない）。
`AGENTS.md` はルート直下に存在するが upstream 所有ファイルで、upstream 側が今も
編集し続けている。fork 固有の運用ルールは upstream 所有ファイルではなく必ずここに書く。

## Claude bridge ロジックの隔離ルール

`claude-bridge` フォークの bridge 固有ロジックは、upstream 所有ファイル
（gateway/run.py, gateway/slash_commands.py, gateway/config.py,
hermes_cli/model_switch.py, hermes_cli/providers.py,
plugins/platforms/discord/adapter.py, hermes_cli/runtime_provider.py,
agent/auxiliary_client.py 等）に直接書かない。実体は fork 専有モジュールに置く:

- `gateway/claude_bridge/`（core.py / config.py / runner_mixin.py / slash.py）
- `hermes_cli/claude_bridge_defs.py`, `hermes_cli/claude_bridge_switch.py`
- `plugins/platforms/discord/claude_bridge_ui.py`

upstream 所有ファイルに残してよいのは、上記モジュールを呼ぶ薄いフック
（1〜数行の呼び出し）だけ。新しい bridge 機能を追加するとき、まず「この関数は
`gateway/claude_bridge/` の既存モジュールに置けるか」を最初に考える —
upstream 所有ファイルに書くのは、その呼び出し口だけで済まないときの最終手段。

### 判定基準は行数ではなく「package を経由しているか」

各ファイルにはおおよその diff-stat 予算がある（`git merge-base upstream/main HEAD`
基準、2026-08-31実測）:

| ファイル | 予算 (+/-) |
|---|---|
| gateway/run.py | ≤170 |
| gateway/slash_commands.py | ≤110〜120目安 |
| gateway/config.py | ≤20 |
| hermes_cli/model_switch.py | ≤55 |
| hermes_cli/providers.py | ≤8 |
| plugins/platforms/discord/adapter.py | ≤35 |

予算はあくまで目安。**超過そのものはリファクタ理由にならない** — 超過分が
`gateway/claude_bridge/` 等を呼ぶフックの追加（例: `/model` や `/reasoning` の
統合ポイントが増えた）でしかないなら、リファクタは不要。リファクタが要るのは、
超過分に package を経由しない生ロジックが inline で混入しているときだけ。

### 再測定するとき

`upstream/main` は事前に `git fetch upstream main` で最新化し、
`git diff upstream/main` ではなく `git diff $(git merge-base upstream/main HEAD)`
を基準にする。`upstream/main` の fetch が壊れたブランチ参照で止まっていると
（実例: 2週間 stale な状態で無関係な upstream 側の変更まで footprint に
混入し、実際には健全なファイルが数千行超過に見えた）測定値が信用できない。

### 大きく育ってきたら

上記モジュールを経由しない inline ロジックが実際に積み上がってきたら、
`.claude/skills/hermes-bridge-footprint-refactor/SKILL.md` の手順
（footprint測定 → worktree隔離 → delegate-impl → diff-stat/baseline比較 →
delegate-review → commit）に従う。
