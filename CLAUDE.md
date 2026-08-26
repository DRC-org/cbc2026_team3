# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

キャチロボバトルコンテスト 2026 出場ロボットの中央制御プログラム。
固定型ロボット（メインハンド + サブハンド）を半自動シーケンス制御で動作させる。
同一 PC 上で両ロボットを制御し、Web UI（localhost:8080）から操縦者が操作する。
自作モータドライバのファームウェア（PlatformIO）も同じリポジトリに同居している。

## 設計文書

| 文書 | 役割 |
|---|---|
| `docs/impl_plan.md` | 実装計画・設計判断の記録。**すべての実装判断はこれに従う。設計変更・追加作業を行ったら必ず更新すること** |
| `docs/motor_driver_can_protocol.md` | 自作モータドライバ CAN プロトコルの**単一情報源**。PC 側 `lib/drivers/generic.py` と `firmware/` の双方がこれに従う |

## コマンド

### Python バックエンド（uv 管理）

```bash
uv run python main.py             # サーバー起動（localhost:8080）
uv run python main.py --dry-run   # CAN バスなしで起動（virtual バス。配線確認に使える）
uv run pytest                     # 全テスト実行
uv run pytest tests/drivers/      # ドライバテストのみ
uv run pytest -x                  # 最初の失敗で停止
uv run pytest -k "m3508"          # 特定テストのみ
uv run ruff check .               # リント
uv run ruff format .              # フォーマット
```

### Web UI（web/ ディレクトリ）

```bash
cd web && pnpm install            # 依存インストール
cd web && pnpm dev                # 開発サーバー起動
cd web && pnpm build              # プロダクションビルド
cd web && pnpm test               # vitest（watch）
cd web && pnpm test:run           # vitest（1 回だけ実行）
cd web && pnpm check              # lint + format + 型検査 + テスト
```

dev サーバーは全インターフェースに bind し、Host ヘッダは `drc` と `*.ts.net` を許可する
（`vite.config.ts`）。Tailscale 経由なら `http://drc:5173`、制御プログラム直結なら
`http://drc:8080` で開く。別名のホストを使うなら `VITE_ALLOWED_HOSTS` に足す。
WS 接続先は UI のステータスバー（接続表示）から変更でき、`?ws=drc:8080` でも一時上書きできる。
タブは URL パス（`/monitor` `/main-hand` `/sub-hand` `/pid-tuning`）。旧ハッシュ形式の
ブックマーク（`#main-hand` 等）は起動時にパスへ読み替える。

### ファームウェア（PlatformIO）

`-d` にプロジェクトディレクトリを渡せばリポジトリ直下から実行できる。

```bash
pio test -e native -d firmware/dc_motor   # 実機不要。firmware/test/ の全ケース
pio test -e native -d firmware/servo      # 実機不要。上とまったく同じ全ケース
pio run -e uno_r4_minima -d firmware/dc_motor
pio run -e uno_r4_minima -d firmware/servo -t upload
```

テストは `firmware/test/` にあり、両プロジェクトが `test_dir` で共有するので、
**native テストはどちらか一方で足りる**（どちらから回しても同じ全ケースが走る）。
一方**実機ビルド（`pio run`）は従来どおり両方必要**。共有しているのは `firmware/lib/MotorCan/`
までで、`main.cpp` と `config.h` は別物のため。

### CAN セットアップ

```bash
sudo scripts/install.sh           # udev ルール配置 + systemd 有効化（初回のみ）
scripts/setup_can.sh              # 手動 up。見つかったバスだけ立ち上げる（開発用）
scripts/setup_can.sh --strict     # 試合前点検。3 本揃わなければ異常終了

# vcan（CAN 統合テスト用）
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

## アーキテクチャ

asyncio 単一プロセスで CAN 通信・シーケンス制御・Web サーバーを統合実行する。

- `lib/` — 共通ライブラリ（両ロボットで共有）
  - `axis_sync.py` — 左右直結ペアの単位換算とずれ判定（`MotorSpec` / `SyncGroup`）。最下位層で、上位を import しない
  - `can_manager.py` — SocketCAN 複数バス管理。受信ループがモータの `matches_feedback` でフレームを振り分ける
  - `commands.py` — WS コマンドの語彙（名前・許可フェーズ・緊急停止時の可否・ハンドラ）の単一情報源
  - `config_schema.py` — yaml の検証付き読み込み。しきい値の既定値もここだけが持つ
  - `drivers/` — モータドライバ群（M3508 / EDULITE 05 / 自作モタドラ）。`base.py` の基底クラスを継承
  - `control/periodic.py` — 周期タスクの土台（`PeriodicTask` / `PausablePeriodicTask` / `LogThrottle`）。
    3 つの周期タスクはこれを継承し、起動・停止・周期の作法を共有する
  - `control/feedback.py` — フィードバック鮮度の判定（`FeedbackFreshness`）。未受信は異常にしない
  - `control/sync_guard.py` — 左右直結ペアの局所保護（`SyncGuard`）。判定とラッチだけを持つ
  - `control/position_loop.py` — M3508 の PC 側位置制御 PID ループ（200Hz）
  - `control/sync_monitor.py` — 左右ペア軸のずれを常時監視（50Hz）。超過で全体緊急停止
  - `sequence/positions.py` — 位置定数 yaml の読み込み・単位換算・論理軸の解決
  - `sequence/motors.py` — `MotorHandle`（1 モータ）と `AxisHandle`（1 論理軸 = 1〜N モータ）
  - `sequence/engine.py` — `@step` デコレータベースのシーケンスエンジン。`require_trigger=True` で操縦者の許可待ち
  - `motor_check.py` — セッティングタイムのアクチュエータ動作確認
  - `server.py` — aiohttp で HTTP 静的配信 + WebSocket (`/ws`) を統合
- `robots/` — ロボット固有のシーケンス定義（main_hand.py / sub_hand.py）
- `config/` — YAML 設定（後述）
- `firmware/` — 自作モータドライバのファームウェア（PlatformIO / Arduino UNO R4）
- `web/` — Vite + React + TypeScript + Tailwind v4 / daisyUI 5 の操作 UI
  - 画面切替は React Router（library mode / `createBrowserRouter`）。ルートは `src/routes.tsx`、
    共通の外枠と WebSocket 接続は `src/layouts/RootLayout.tsx`（タブ帯は `AppHeader` の中）
  - 配色は `src/index.css` の daisyUI カスタムテーマ `cbc`（ライト基調）に集約。組み込みテーマは使わない
  - `src/components/` は**誰が描くか**で分ける。`shell/`（RootLayout が全画面へ出す外枠）/
    `monitor/`（Dashboard 専用）/ `operator/`（RobotControl 専用）/ `motorcheck/`（セッティング
    タイムの動作確認）/ `diagnostics/`（`SubsystemStatus` を頂点とする診断ツリー）/ `ui/` の 6 つで、
    直下には何も置かない。barrel（`index.ts`）は作らず常に実ファイルまで指す（oxlint の
    `import/no-cycle` を効かせたまま依存グラフを読めるようにするため）
  - `src/components/ui/` — 自前プリミティブ（`Page` / `Panel` / `Section` / `Button` /
    `StatusBadge` / `Kbd` / `Icon` / `Modal`）。レイアウト骨格は CSS ではなくここが持つ
  - 画面の主役は `ActionPanel`（操縦者・試合中）/ `StartGate`（Monitor・準備中）/
    `Checklist`（操縦者・準備中）。診断は `SubsystemStatus` が平常時 1 行へ畳む
  - アイコンは `lucide-react`。既定値は `ui/Icon.tsx` に閉じ込め、各所で個別指定しない
  - フォントは `@fontsource-variable/*` で自己ホスト（会場のネットワークに依存させない）
  - `src/test/` — vitest 共通ヘルパ（WebSocket スタブ、RobotProvider ラッパ）。テスト本体は対象ソースの隣に `*.test.ts(x)`

### 設定ファイルの分担

| ファイル | 持つもの |
|---|---|
| `config/system.yaml` | PC 上に 1 つしか存在しない設定。バス別名・`health`・`motor_check` |
| `config/can_buses.yaml` | CAN バス定義の単一情報源。udev ルールとセットアップスクリプトの双方がここから生成・参照する |
| `config/<robot>.yaml` | そのロボットのモータ構成（ドライバ種別・バス別名・CAN ID・PID・動作確認の個別上書き） |
| `config/<robot>_positions.yaml` | 論理軸の単位換算と機構位置の定数 |
| `config/checklist.yaml` | セッティングタイムの指差喚呼チェックリスト |

読み込みと検証は `lib/config_schema.py` に一本化してある。`health` / `motor_check` /
`can_buses` は PC 上に 1 組しか存在し得ないため `config/<robot>.yaml` には書けず、
書いてあったら移動先を示して**起動を拒否**する。ロボットごとの yaml に書けてしまうと
読み込み側は片方の値しか採用できず、もう片方が「書けるのに効かない設定」になるため。

**`robots/*.py` に数値を書いてはならない。** シーケンスは `move_to({"軸名": "位置名"})` の形で書き、
単位換算・許容差・待ち時間はすべて位置定数 yaml が持つ。機構が変わったら yaml の数値だけを差し替える。

### 知っておくべき設計上の制約

**CAN バス名は udev で個体固定する。** `can0`/`can1`/`can2` は USB 列挙順で入れ替わり、
番号が入れ替わると C620 に EDULITE 用のコマンドが飛んでモータを壊す。
バス名は `can_m3508` / `can_edulite` / `can_generic` で、STM32 UID 由来の serial に紐付ける。

**CAN ID はバス単位でロボット横断に一意。** メインハンドとサブハンドは物理的に同じ
`can_edulite` / `can_generic` を共有する。重複すると受信ループが最初にマッチした 1 台で
打ち切るため、もう一方は永久にフィードバックを得られない。`tests/test_robot_sequences.py` で検証している。

**M3508 は電流指令しか受け付けない。** 位置制御は PC 側の PID ループが担う。
C620 の電流指令フレーム（`0x200`）は 1 通に 4 モータ分のスロットを持つため、
**同一バス上の M3508 は必ず 1 つの `M3508PositionLoop` が束ねる**（個別送信すると他モータのスロットを 0 で潰す）。

**左右ペア軸は 3 重に保護している。** `y_axis`（M3508 ×2）と `rotate`（EDULITE ×2）は機構的に直結し、
位置がずれるとその場で壊れる。位置定数 yaml の `motors:` で 1 論理軸に複数モータを束ね、
逆回転は `scale` の符号で表す。保護は ①同一フレームでの同時指令 ②偏差監視（シーケンス停止 /
ループ内 200Hz で電流 0 / `SyncMonitor` 50Hz で全体緊急停止）③フィードバック途絶をペア単位で判定
（片方だけ止めると残った側が押し続ける）。
**判定と単位換算は `lib/axis_sync.py` に一本化してあり、3 層とも `SyncGroup.violation()` を呼ぶ。**
層ごとに違うのは頻度と超過後の扱い（debounce・ラッチ・効果）だけで、境界そのものがずれてはならない。
逆換算を層ごとに書き写すと、符号を 1 箇所落としただけで「ずれていないのに止まる」か
「ずれているのに止まらない」のどちらかになり、しかも片方の層だけが壊れるので気付けない。
`lib/axis_sync.py` は最下位層なので上位モジュールを import してはならず、層ごとの差とその理由は
同モジュールの docstring に集約してある。

**ペア軸に片側だけ効く操作を作らない。** PID ゲイン差し替え（`set_pid_gain`）も原点確定
（`set_origin_here`）もグループ全員へ展開する。左右を別々の時刻に原点確定すると、その間に
片方が動いた分だけ消えないオフセットが残り、正常な動作でも即座に偏差超過で止まる。
「1 台だけに効かせてよいか」の判断は `M3508PositionLoop._paired_with()` に 1 つだけ置く。

**周期タスクは `lib/control/periodic.py` を継承し、作法を揃える。** 兄弟クラスで作法が違うと
片方の知識でもう片方を触ったときに「止めたつもりが止まっていない」が起きる。二重 `start()` は
`RuntimeError`（黙って無視すると起動したつもりのタスクを試合中に発見できない）、`stop()` は
`cancel()` せず停止イベントで降ろす（`_on_run_exit()` で 0 電流フレームを送るタスクがあり、
キャンセルすると止め損なう）。**周期は次回起床時刻を絶対時刻で管理する。** 後置 `sleep` だと
実周期が `interval + 処理時間` になり、「50Hz × 2 サンプル = 40ms なら機構破損に間に合う」と
置いている偏差監視の時間予算が負荷に比例して伸びる。周期の実測をテストで固定してある。

**離散状態アクチュエータは新ドライバを作らず位置定数で表す。** グリッパの開/閉、壁の初期/閉/開は
`positions` の名前付き状態として書く。`move_to` は位置名でしか値を引けないため、
定義した状態以外を送れないことが構造的に保証される。

**運用は半自動シーケンス制御のみ。操作モードという軸は存在しない。** 機体が動くのは
操縦者がタブで `sequence_start` / `trigger` を押したときだけで、`match_start` はフェーズを
進めるだけ。`require_trigger` のステップは常にトリガー待ちで止まる（「全自動なら素通り」
のような例外は無い）。試合開始のゲートは `main_hand` / `sub_hand` 2 名の指差喚呼。

**試合を開始できるかを決めるのはサーバーの `can_start_match` だけ。** UI は理由を説明する
だけで、`checklists` から開始可否を導出し直してはならない。一度これを `StartGate` でやって、
サーバーが「開始できる」と配信しているのに画面がボタンを殺す状態を作った（配信ロールが
1 つ増えただけで試合が始められなくなる）。`lib/healthVerdict.ts` と同じ、判定を 1 箇所に
置く原則がここにも要る。

**フェーズによる可否のサーバー写しは `lib/phase.ts` の `isDuringMatch()` だけ。**
`lib/match_state.py` の `PHASES_DURING_MATCH` と 1:1 で対応し、`PHASES_OUTSIDE_MATCH` は
その補集合なのでこの 1 つで両側に答えられる。可否を決めるのはサーバー（`lib/commands.py`）で、
UI は送る前に理由を説明するだけ。画面ごとに `phase === "match"` と書き散らすと、
フェーズが増えたときに片方の画面だけが古い条件のまま残る。レイアウトの出し分けに使う
`isSetupPhase()` とは別物で、一致させてはならない（あちらは `finished` を試合中と同じ
情報密度へ寄せるための区分）。塞ぐのは送信であって編集ではない — 試合中の `/pid-tuning` は
送信ボタンだけを無効化する。値を用意しておけるほうが実務に合う。

**WS コマンドの語彙は `lib/commands.py` の `CommandSpec` が単一情報源。** 名前・許可フェーズ・
拒否理由・緊急停止中の可否・ハンドラ名・拒否通知経路を同じ 1 行に置く。`CommandSpec` は
どのフィールドにも既定値を持たないので、コマンドを足す人はゲート方針を必ず宣言することになる。
語彙が「許可フェーズ表」「拒否文」「if-elif」へ分かれていると、どのゲート表にも載らない
コマンドが生まれ、「意図してゲート対象外にした」のか「書き忘れた」のかコードから読めなくなる。
全フェーズで通すなら `PHASES_ANY` を明示的に書く（＝素通りさせると宣言する）。
`lib/server.py` に語彙を戻してはならない。

**しきい値の既定値は `lib/config_schema.py` にしか置かない。** `health` の 4 値は
`HealthThresholds` として必ず 1 組で運び、バラの引数に分解しない。分解すると 4 本のうち
3 本だけ配線した経路が作れてしまい、残る 1 本だけが既定値のまま黙って効く（「途絶は config
どおりに見ているのに温度警告だけ既定の 65℃」が成立し、ログにも UI にも現れない）。
同じ理由で、同じ概念に別名を付けない（動作確認の鮮度判定も `feedback_timeout_ms`）。

**ファームと PC 側はプロトコルの対。** `docs/motor_driver_can_protocol.md` を単一情報源とし、
片方だけを変更してはならない。`firmware/lib/MotorCan/` が `Arduino.h` を include しないのは、
native 環境（`pio test -e native`）でプロトコル層と安全機構をテストできるようにするため。

**基板は DC 用・サーボ用とも UNO R4 Minima で、CAN ペリフェラルは `D4`(TX)/`D5`(RX) に固定。**
このピンを他用途へ割り当てると CAN が上がらず、PC から止められない基板ができあがる。
各 `main.cpp` の `static_assert` が `config.h` のピン衝突をビルド時に検出する。

**テレメトリ配信は 1 クライアントの不調で止めてはならない。** `_broadcast_state` は全
クライアントへ直列に送るため、詰まった 1 台を無期限に待つと他の全員（Monitor 含む）の
値が凍る。しかも WebSocket は開いたままなので UI は「接続中」を出し続け、操縦者は
凍った値を最新だと思って見続ける。送信には `_WS_SEND_TIMEOUT_S` を必ず通し
(`_send_or_drop`)、切り離しの `close()` は別タスクへ逃がす（`close()` も相手の応答を待つので、
配信ループ上で await すると同じ場所で詰まる）。`_broadcast_loop` の例外ガードも外さないこと。

**WS メッセージの契約は 1 箇所で定義し、サーバーと UI が同じものを見る。** 両者が
それぞれ自分のサンプルを持つと、契約が食い違ったまま両方のテストが緑になる。一度これで
`health_change` に `robot` が載っていないのに UI が `typeof msg.robot === "string"` を受信条件に
していて、ヘルス異常が 100% 捨てられたまま出荷しかけた（TS 側はサンプルを自分で捏造し、
Python 側は `target` と `to` しか見ていなかった）。`tests/test_ws_contract.py` が実物の
`RobotServer` に配信させたメッセージを `web/src/test/ws-contract.json` へ焼き付け、
`web/src/test/wsContract.test.ts` が同じファイルを `useRobotSocket` の**受信経路へ流し込む**
（型が合っていても受信条件が弾けば画面には何も出ないので、型アサーションでは足りない）。
契約ファイルは**手書き禁止**。サーバー側を変えたら
`UPDATE_WS_CONTRACT=1 uv run pytest tests/test_ws_contract.py` で再生成し、web/ 側の型と
受信条件も必ず追従させること。

**WS のワイヤ型と受信条件は `web/src/lib/protocol.ts` にしかない。** 型は
`parseServerMessage()` と同じファイルに置く。型だけを別に持つと「型は合っているのに
受信条件が弾く」— 画面には何も出ないのに型検査は通る — 状態が作れてしまう。
`lib/` は最下層なので `hooks/` を import してはならない（接続を張らずに受信条件と状態遷移を
検証できる性質は、依存の向きが片方向であることで成立している）。状態遷移は純関数の
`lib/robotReducer.ts`、接続の面倒だけが `hooks/useWebSocket.ts` にある。

**Web UI はモータ名をハードコードしていない。** モータ状態は `Record<string, MotorState>` として
そのまま流れるので、モータの増減で UI 側の変更は要らない。

**context は購読頻度で 3 つに分ける。** `useRobotStates()`（毎秒 40 回変わるテレメトリ）/
`useRobotStatus()`（フェーズ・接続・ヘルス）/ `useRobotCommands()`（送信関数）から
**必要なものだけ**を読む。1 つに束ねると、モータ温度が 0.1℃ 動いただけでチェックリストも
タブもトーストも描き直される。**ただし分割だけでは効かない。** 親が再描画すると React は
memo の無い子を素通しで描き直すので、外枠は `memo` した `AppShell`（`layouts/RootLayout.tsx`）に
括り出し、そこへテレメトリ由来の props を渡さないこと。`RootLayout.test.tsx` と
`RobotContext.test.tsx` が再描画回数で守っている。

**旧ハッシュ URL の読み替えはルーター生成より前に行う。** 各操縦者は担当タブの URL をブックマークして
試合に臨む。`web/src/App.tsx` は `applyLegacyHashRedirect()` → `createBrowserRouter()` の順で
評価されることに依存しており（`createBrowserRouter` は生成時点の location を読む）、
順序が崩れると `#main-hand` 等の旧ブックマークが全て Monitor に落ちる。`src/App.test.tsx` が検証している。

**モーダルは `<dialog>` を使わない。** `<dialog>` + `showModal()` は Esc で必ず閉じるため、
解除経路を Reset ボタンのみに限定する緊急停止オーバーレイの安全設計と両立しない。
`components/ui/Modal.tsx` は `onClose` を渡さなければ閉じられない構造になっている。

**daisyUI のクラスは「対」で書く。片方だけだと可視化ルールごと消える。** Tailwind は
ソース中に現れたクラスぶんしか CSS を出力しない。`modal-box` を書いて `modal modal-open` を
書かないと、`.modal-box` の既定 `opacity:0` だけが残り、`.modal.modal-open>.modal-box{opacity:1}` は
出力されず、**DOM には居るのに何も見えないモーダル**になる（実際に一度これで出荷しかけた）。
同種の罠は「親クラス + 状態クラス」で成立する daisyUI コンポーネント全般にある。

**daisyUI の既定を上書きしたい箇所は必ず明示のユーティリティを書く。** ビルド後の
レイヤ順は `... < utilities < daisyui` に見えるが、実測ではユーティリティが勝つ。
一方でユーティリティを書いていない属性は daisyUI の既定がそのまま残る。
特に `:disabled` は既定が「地 base-content 10% / 文字 20%」で、`⊘ 準備中` `RUNNING` `✓ DONE` の
ように*状態表示を兼ねる*無効ボタンが読めなくなる（`components/ui/Button.tsx` の `DISABLED_CLASS` で
上書き済み）。配色を変えたときは実機描画で確認すること。

**サイズ修飾子が font-size まで固定するコンポーネントに注意。** `card-xs` や `table-xs` は
padding だけでなく本文の `font-size` も直接指定する。ルートの `clamp()` による全体スケーリングから
その部分だけが外れるため、`card-body` は使わず（`Panel.tsx` は枠にだけ `card card-border` を使う）、
`table-xs` はセル側に `text-[0.85em]` を当てて打ち消す。

**各画面は答える問いを 1 つに絞り、同じ事実を 2 度描かない。** 以前は現在ステップが
`SEQUENCE` / `CURRENT STEP` / `STEP 一覧` の 3 箇所に出ており、操縦者は 3 回読んで
ようやく 1 つの事実にたどり着いていた。新しい表示を足すときは、まずその事実が
既にどこかに描かれていないかを確認すること。

**平常時に静かで、異常時に自分から主張する。** 試合中の操縦者は機体を見ており、
画面へ視線を戻すのは一瞬しかない。そこに 8 モータ × 4 値の数字が常時出ていると
「異常があるか」が数字の海に沈む。`SubsystemStatus` は平常時 1 行に畳み、
異常時は操縦者の開閉操作を**上書きして**開く（畳んだまま見逃させない）。
同じ部品でも役割で既定を変える — 操縦者の試合中は畳み、Monitor と準備フェーズは開く。

**主操作は状態によって位置を動かさない。** `ActionPanel` は右の大きい面が常に
「今押すべきボタン」（START / NEXT / RUNNING / DONE）で、左は常に STOP。
状態で入れ替えると押す直前に毎回探し直すことになる。

**grid の子は既定で縦に伸びる。`shrink-0` では止まらない。** 内容ぶんの高さに留めるには
`self-start` が要る。落とすと、平常時に中身が数行しかないカードが全高の白い箱になる。

**クラス名を実行時に組み立ててはならない。** `TONE_BORDER_CLASS[t].replace("border-", "border-l-")`
のような書き方は Tailwind の走査から漏れ、CSS ごと出力されない（`lib/tone.ts` に
`TONE_BORDER_L_CLASS` としてリテラルで持つ）。

**機体の健全性判定は `lib/healthVerdict.ts` に一本化する。** 2 箇所に書くと
「Monitor は READY と言うのに操縦者の画面は異常と言う」状態が生まれる。

**状態は色付きテキストではなくチップで示す。** ライト地では警告色を AA (4.5:1) まで暗くすると
もはや警告色に見えない。`components/ui/StatusBadge.tsx` に一本化してあるので、
新しい状態表示を足すときも着色テキストを書かずにこれを使う。

## テスト方針

TDD でプロトコル層とシーケンスエンジンを開発する。テストを先に書き（RED）、実装して通す（GREEN）。
詳細は `docs/impl_plan.md` の「テスト戦略」セクションを参照。

## 言語

日本語でコミュニケーションすること。コード中のコメントも日本語で可。
