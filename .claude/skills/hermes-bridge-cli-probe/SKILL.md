---
name: hermes-bridge-cli-probe
description: hermes-agent の gateway/claude_bridge で `claude -p` の stream-json 実行時挙動（ターン進行中の二重 stdin 投入、permission-mode の即時反映など）に依存する機能を実装・変更する前に、その挙動を推測せず実プロセスへの実測プローブで確認する手順。「claude -p が〜を受け付けるか確認したい」「bridge の実装が CLI の未文書化挙動に依存している」「ステアリング/割り込みが効くか試したい」で発火
metadata:
  origin: session-learning
  generated_at: 2026-08-19
  source_session: d5085f99-6f14-4062-a97b-31ba6abdbdd6
---

# claude -p の実行時挙動を実測してから bridge を実装する

`gateway/claude_bridge` は `claude -p --input-format stream-json --output-format
stream-json` を per-channel の常駐子プロセスとして起動し、stdin に NDJSON の
`user` イベントを書き込んで stdout の `result` イベントを待つ。この CLI 側の
挙動（ターン進行中に2通目の `user` イベントを書いたらどう扱われるか、
permission-mode の変更がいつ効くか等）はドキュメント化されていない。ここを
ドキュメントや訓練知識からの推測で実装すると、間違った前提の上にロックや
キューの設計を組んでしまう。**必ず実プロセスに対する実測プローブを先に行う。**

## 手順

1. スクラッチ領域（セッションの scratchpad ディレクトリ等、repo 外）で
   NDJSON イベントを `printf` + `sleep` で時間差投入するパイプラインを組む:

   ```bash
   { printf '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"<最初の指示>"}]}}\n'
     sleep <T1>
     printf '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"<割り込みで送る指示>"}]}}\n'
     sleep <T2>
   } | claude -p --input-format stream-json --output-format stream-json --verbose \
       --allowedTools "<必要な最小限>" --model <model> --settings '{"env":{}}' \
       > probe_out.jsonl 2> probe_err.log
   echo "exit=$?"; wc -l probe_out.jsonl
   ```

   - 最初の指示は「数秒かかる決定的なタスク＋固定文字列で返答」にする（例:
     `sleep 15` を実行してから厳密に `DONE1` とだけ返す）。実行時間を作れて
     結果を文字列比較だけで判定できる。
   - 割り込みの指示は、最初のタスクの `sleep` より短い `T1` の後に送る
     （＝進行中のターンに刺さる位置）。割り込みが反映されたかを見分けられる
     マーカー語（例: `EXTRA` を返答に含めさせる）を必ず仕込む。
   - パイプの寿命（末尾の `sleep <T2>`）は CLI が実際に終了して結果を吐き
     切るまでの十分な長さにする。短すぎると `result` イベントが出る前に
     パイプが閉じて何も確認できない。
   - `--model` は結果が見えれば十分なので安価なものでよい。

2. `probe_out.jsonl` を1行ずつ python3 でパースし、`type` で分岐して人間に
   読める形に要約する:

   ```bash
   python3 -c "
   import json
   for i, line in enumerate(open('probe_out.jsonl')):
       e = json.loads(line)
       t = e.get('type')
       if t == 'assistant':
           for c in e['message'].get('content', []):
               if c.get('type') == 'text':
                   print(i, 'assistant text:', repr(c['text'][:200]))
               elif c.get('type') == 'tool_use':
                   print(i, 'tool_use:', c.get('name'), json.dumps(c.get('input'))[:120])
       elif t == 'user':
           print(i, 'user content/tool_result')
       elif t == 'result':
           print(i, 'RESULT is_error=', e.get('is_error'), 'num_turns=', e.get('num_turns'), 'result=', repr(str(e.get('result'))[:200]))
       else:
           print(i, t, str(e.get('subtype',''))[:40])
   "
   ```

3. 判定基準:
   - マーカー語が最終 `result` のテキストに含まれ、かつ `result` イベントが
     1件しか出ていなければ「CLI が2通目を進行中のターンに畳み込んだ
     （steering された）」と確定できる。
   - `result` が複数出る、あるいはマーカー語が出ないなら、2通目は無視される
     か次ターンとして独立に扱われている——ロック/キュー越しに送る設計のままで
     正しい。
   - 確定した挙動を根拠に実装する。実装後のテスト用フェイク（`proc.stdout`
     を差し替える `MagicMock`/`asyncio.Queue` パターン、
     `tests/gateway/test_claude_bridge_*.py` 系に既存）にも、プローブで
     確認した通りの `result` イベント形（`is_error` の有無、`session_id` の
     扱い等）をそのまま反映させる——プローブ結果と食い違うテストは実挙動を
     検証していない。

## 適用範囲

`hermes-bridge-slash-command` の手順1（「依存する CLI 機能を先に実測する」）
と同じ原則の、ターン進行中の stdin 投入という別カテゴリへの適用。スラッシュ
コマンド追加以外——タイムアウト、キャンセル、permission-mode 切り替え
タイミングなど——`claude -p` の実行時挙動に実装が依存する変更全般に使う。
