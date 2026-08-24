---
name: hermes-bridge-busy-gate-diagnosis
description: hermes-agent の gateway/claude_bridge でターン進行中のsteering/interrupt系の変更を実装・テストして本番へ反映したのに、Discord等で効いていないように見えるときに使う。原因が bridge 側の実装ではなく手前の共通「セッションビジー時ガード」（gateway/run.py::_handle_active_session_busy_message、gateway/platforms/base.py::handle_message）にある可能性を、プロセス鮮度確認→ログ突合→コード追跡の順で切り分ける手順。「実装したのに効いていない」「ログから原因調査して」「busy_input_modeの設定通りに動いていない」で発火。
metadata:
  origin: session-learning
  generated_at: 2026-08-19
  source_session: d5085f99-6f14-4062-a97b-31ba6abdbdd6
---

# gateway busy-session ガードが bridge の変更を握りつぶしていないか切り分ける

## 背景

hermes-agent のメッセージルーティングは2層構造になっている。各プラットフォーム
adapter の `handle_message`（`gateway/platforms/base.py`）は、セッションが
ビジーな間、後続メッセージを先に `self._busy_session_handler`（=
`gateway/run.py::_handle_active_session_busy_message`）へ渡す。ここを通過
しないと `gateway/claude_bridge` など個別プロバイダの `handle_message` には
**そもそも到達しない**。この上位ガードは `_effective_busy_input_mode()` が
返す `busy_input_mode`（config: `agent-config/home/hermes/config.yaml` の
`busy_input_mode` キー）に従って分岐し、`running_agent = _busy_state.turn.agent`
に対して `hasattr(running_agent, "redirect")` / `hasattr(running_agent, "steer")`
をチェックする。**プロバイダが `turn.agent` に何も登録していなければ、
config値が `interrupt` でも `steer` でも常に既存の queue へフォールバックする**
——実装のバグではなく、この契約（`turn.agent` に `redirect`/`steer` を生やして
登録する）を満たしていないだけなので、プロバイダ側の差分だけをいくら見直しても
原因は見つからない。

## 手順

1. **プロセスが新しいコードを読んでいるかを機械的に確認する**（コード追跡に
   入る前に、まずここを潰しておく）:
   ```bash
   stat -f "%Sm" -t "%Y-%m-%d %H:%M:%S" <変更したファイル>
   ps -p <gatewayのPID> -o lstart=
   find <__pycache__ dir> -name "<module>*" -exec stat -f "%Sm %N" {} \;
   ```
   ソースの mtime・.pyc のコンパイル時刻がいずれもプロセス起動時刻より前で
   あれば、新しいコードは読み込まれている。ここが原因ではないと確定させて
   から次に進む。

2. **自分が仕込んだログ文言が一度も出ていないことを確認する**。実装に
   仕込んだ固有のログ（例: `logger.info("claude_bridge: steered the active
   turn for %s", key)`）を全期間 grep する:
   ```bash
   rg -n "<仕込んだログ文言>" ~/.hermes/logs/gateway.log
   ```
   0件なら、変更したコードパス自体が一度も呼ばれていない証拠。
   `~/.hermes/logs/errors.log` も grep して例外で落ちていないか確認するが、
   無関係な例外（Discord再接続・レート制限等）しか無ければここは深追いしない。

3. **実際のメッセージ数とターン完了数を突き合わせる**。対象チャンネル/
   セッションのキーはプロバイダの完了ログ（例: `gateway.claude_bridge:
   claude_bridge: key=<platform>:<chat_id>:<thread_id> session=<uuid>
   cost=... is_error=...`）から取れる。そのキーで複数ログを串刺しにする:
   ```bash
   rg -n "<channel/thread ID>" ~/.hermes/logs/gateway.log ~/.hermes/logs/agent.log
   ```
   送信メッセージ数（例: Discord adapter の "Flushing text batch" ログ）に
   対し完了ターン数（"claude_bridge: key=..." 等の完了ログ）が明らかに
   少なければ、複数メッセージが1ターンにまとめられている＝steer ではなく
   queue で捌かれている証拠になる。

4. **呼ばれていないなら、`handle_message` の手前を遡ってゲートを特定する**。
   `gateway/platforms/base.py::handle_message` → `self._busy_session_handler`
   （= `gateway/run.py::_handle_active_session_busy_message`）→
   `_effective_busy_input_mode(source)` → `running_agent =
   _busy_state.turn.agent` に対する `hasattr` チェック、の順に読み、対象
   プロバイダのターンが `turn.agent` に何を登録しているか（例: bridge は
   `ClaudeBridgeRunnerMixin._claude_bridge_handler` が
   `self.claude_bridge.handle_message(event)` を直接 await するだけで
   `turn.agent` に何も登録しない）を確認する。ここまで来て初めて「実装
   自体は正しいが、呼ばれる経路がない」という結論が技術的に裏付けられる。

## 対応方針を決めるときの注意

このガードは upstream 所有ファイル（`gateway/run.py` 等）にある。footprint
最小化の方針（[[hermes-bridge-footprint-refactor]]）を優先するなら、`run.py`
側には「対象プロバイダのターン中かどうかを判定して委譲する薄いフックを
1つ足すだけ」に留め、steer/queue 等の実際の挙動分岐はプロバイダ側の config
（例: `busy_mode`）に持たせる設計にすると upstream 差分を最小に保てる。
native 側の `busy_input_mode` の意味論（例: `interrupt` = kill して新規ターン）
と、プロバイダ側で実装可能な意味論がズレる場合は、その解釈をどちらに寄せる
か・config キーをどう追加するかを実装前に AskUserQuestion でユーザーに
提示して決める——ここは技術的に一意に決まらない製品判断であり、切り分け
だけでは答えが出ない。

## 適用範囲

bridge 以外の新しいプロバイダやフックが gateway の busy-session まわりの
挙動（interrupt/steer/redirect）に依存する変更を入れるときも同じ切り分け
手順が使える。[[hermes-bridge-cli-probe]]（`claude -p` CLI 側の未文書化
挙動を実測する手順）とは対象レイヤーが異なる——こちらは gateway 内部の
メッセージルーティング層の診断。
