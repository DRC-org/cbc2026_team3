# 画面と部品 — どの画面に何が出るか

**「今どうなっているか」だけ。** 理由は `CLAUDE.md`、経緯は `docs/impl_plan.md` の Phase 4。
配色と操作は `docs/web/design.md`、値の届き方は `docs/web/data_flow.md`。

---

## 設計原則

レイアウトの判断はすべてこの 4 つから出ている。

1. **1 画面 = 1 つの問い。** 答えを画面で最も大きい要素にする
2. **同じ事実を 2 度描かない**
3. **平常時は静か、異常時だけ主張する。** ただし異常時は操縦者の開閉操作を**上書きして**開く
4. **主操作は最大・固定位置。** 入れ替えると押す直前に毎回探し直すことになる

| 画面 | フェーズ | 答える問い |
|---|---|---|
| Monitor | 準備中 | 試合を開始できるか。できないなら何が足りないか |
| Monitor | 試合中・終了 | どちらの機体が止まっていて、何か起きていないか |
| 操縦者 | 準備中 | （半自動）機体は健全か ／（手動）狙った位置へ動かせるか |
| 操縦者 | 試合中・終了 | 今 NEXT を押すのか。押すと何が起きるか |

---

## タブ

**割当も個数も `web/src/lib/tabs.ts` の `TABS` だけが持つ。** 件数を他所へ書き写さない。

| タブ | パス | キー | ページ |
|---|---|---|---|
| Monitor | `/monitor` | `1` | `pages/Dashboard.tsx` |
| Main Hand | `/main-hand` | `2` | `pages/RobotControl.tsx`（`robotKey="main_hand"`） |
| Sub Hand | `/sub-hand` | `3` | `pages/RobotControl.tsx`（`robotKey="sub_hand"`） |

- 表示中のタブは URL パスそのもの。リロードで復帰する
- 遷移時は `location.search` を引き継ぐ（`?ws=` の上書きを落とさない）
- 未知のパスは Monitor へ落とす（試合中に白画面を出さない）
- 旧ハッシュ（`#main-hand`）は `applyLegacyHashRedirect()` がパスへ読み替える。
  **`createBrowserRouter()` より前に呼ぶ**
- タブの注意喚起 LED は 異常 → 許可待ち → 要確認 の順。切断中は灰の「通信断」

---

## フェーズ連動レイアウト

準備中に試合用の操作を並べても押せず、試合中に設定 UI を並べても使わない。

区分は `web/src/lib/phase.ts` の `isSetupPhase()`（`setup` + `ready`）。
**コマンドの可否を決める `isDuringMatch()`（`match` のみ）とは別物。一致させてはならない。**

| | 準備中 | 試合中・終了 |
|---|---|---|
| **Monitor** | `StartGate`（全幅・主役）+ 左 `MatchPrep` / 右 機体状態 | `MatchStrip` + `RobotStatusRow` ×2 + `EventFeed` |
| **操縦者・半自動** | `ModeSwitch` + 機体状態（1 列・展開） | `ModeSwitch` + 左 `ActionPanel`+ステップ / 右 `MatchTimer`+機体状態 |
| **操縦者・手動** | `ModeSwitch` + 左 `ManualPanel` / 右 機体状態 | `ModeSwitch` + 左 `ManualPanel` / 右 `MatchTimer`+機体状態 |

指差喚呼と動作確認は **Monitor の準備面にしか無い**。操縦者 2 名が同じ場所に立つので機体ごとに
置くと二度読み上げになり、動作確認は両ハンドを 1 本のシーケンスで駆動する。

---

## Monitor（`pages/Dashboard.tsx`）

### 準備中 — `grid-cols-[minmax(0,1fr)_minmax(20rem,28rem)] grid-rows-[auto_minmax(0,1fr)]`

```
┌─────────────────────────────────────────┐
│ StartGate                 (col-span-full) │ ← 画面で最も大きい要素
├──────────────────────┬──────────────────┤
│ MatchPrep            │ 機体状態          │
│  コート + court       │  SubsystemStatus  │
│  動作確認 + motor_check│  ×2              │
│  指差喚呼 (group 別)   │  showVerdict=false│
└──────────────────────┴──────────────────┘
```

- **`StartGate`** — **残っている項目名まで**出す。機体異常は「開始できない」ではなく警告として
  併記する（サーバーはハードウェア状態で `match_start` を拒否しないので、ここでボタンを殺すと
  軽微な警告 1 つで試合を始められなくなる）
- **`MatchPrep`** — 各操作の直下に、その操作を確認する指差喚呼を置く。配置の正は
  `config/checklist.yaml` の `group`、対応表は `web/src/lib/checklistGroups.ts`。
  全体の進捗は上端に 1 箇所だけ
- **右の機体状態** — `showVerdict={false}`。判定チップは `StartGate` が出すので二重にしない
  （false のときパネルは常に開く）

### 試合中・終了 — `grid-cols-2 grid-rows-[auto_minmax(0,1fr)_minmax(0,0.42fr)]`

```
┌─────────────────────────────────────────┐
│ MatchStrip                (col-span-full) │
├──────────────────────┬──────────────────┤
│ RobotStatusRow       │ RobotStatusRow    │
├──────────────────────┴──────────────────┤
│ EventFeed                 (col-span-full) │
└─────────────────────────────────────────┘
```

- **`MatchStrip` は必須。** `match_finish` は MATCH フェーズ限定なので、隠すと試合を終われない。
  残すのは導線だけ（フェーズとコートはヘッダーが常時出している）
- **`RobotStatusRow`** — 上から 進行状態 → 進捗 → 現在ステップ。数値は下の `SubsystemStatus` へ
  送るが `defaultOpen` で展開する（Monitor は操縦しない役で、異常の切り分けが仕事）
- **`EventFeed`** — トーストは数秒で消えるので、履歴をここに残す

---

## 操縦者画面（`pages/RobotControl.tsx`）

最上段は常に **`ModeSwitch`**。フェーズにもモードにも依らず同じ位置・**同じ高さ**
（変えると下の `ActionPanel` が上下にずれる）。シーケンス名と総ステップ数もこの帯が持つ。
総ステップ数を渡すのは準備中だけ（試合中は `ActionPanel` が `1/22` で出す）。

配信が届くまでは「データ未受信 — 接続待機中...」の 1 パネルだけ。

### 準備中

```
半自動: grid-cols-1              手動: grid-cols-[minmax(0,1fr)_minmax(19rem,26rem)]
┌──────────────┐                ┌─────────────┬──────────┐
│ ModeSwitch   │                │ ModeSwitch              │
├──────────────┤                ├─────────────┼──────────┤
│ 機体状態(展開) │                │ ManualPanel │ 機体状態  │
└──────────────┘                └─────────────┴──────────┘
```

半自動の準備中はこの画面に操作が無い（指差喚呼と動作確認は Monitor 側）ので 1 列に広げる。

### 試合中・終了 — `grid-cols-[minmax(0,1fr)_minmax(17rem,21rem)]`

```
┌─────────────────────────────┬──────────────┐
│ ModeSwitch                                 │
├─────────────────────────────┼──────────────┤
│ ActionPanel                 │ MatchTimer   │ ← shrink-0
│  状態 → 現在ステップ(3em) →   │              │
│  NEXT で走る範囲 → [STOP|主操作]├──────────────┤
├─────────────────────────────│ 機体状態      │ ← 縮む側
│ ステップ一覧 (flex-1・内部scroll)│  平常時 1 行  │
└─────────────────────────────┴──────────────┘
```

手動中は**左カラムごと `ManualPanel` へ明け渡す**。同じ列に 2 つの操作面が並ぶと、
どちらの指令が機体へ届くのか画面から読めなくなる。

**`ActionPanel` の主操作** — 右の大きい面が常に「今押すべきボタン」、左は常に STOP:

| `sequenceKind` | 右の面 | 状態表示 |
|---|---|---|
| `idle` | `START`（`Play`・緑） | 待機中 — START で開始 |
| `idle` かつ `step_index > 0` | `先頭から再開`（`Play`・橙） | 停止中 — START は先頭から走り直します |
| `waiting_trigger` | **`NEXT`**（`ArrowRight`・面塗り） | 許可待ち — NEXT を押してください |
| `running` | `RUNNING`（スピナー・無効） | 実行中 |
| `complete` | `DONE`（`Check`・無効） | 全ステップ完了 |
| `no_sequence` | `シーケンス未取得`（無効） | シーケンス未取得 |
| 試合中でない / 切断中 | 理由を書いた無効ボタン | — |

判定は `web/src/lib/sequenceStatus.ts` の `sequenceKind()` だけが持つ（`step_index` からの
推測をしない）。**START と NEXT はアイコンを分ける** —— 同じ位置に同じ大きさで交互に出るため。

ボタンの上に「**NEXT で走る範囲**」を 1 行で予告する。NEXT を押すと機体は次の許可待ちまで
複数ステップを一気に走るので、どこまで動いて止まるかを押す前に確定させる。

**ステップ一覧**（`SequenceStepList`）は現在位置へ自動スクロール、行を押すと `sequence_jump`。
可否と案内文は `stepJumpBlockedReason` が同時に決める（駆動中は塞ぐ／トリガー待ちは塞がない）。

---

## 外枠（`layouts/RootLayout.tsx`）

```
ConnectionBanner   切断中だけ・全幅
AppHeader          [タブ帯] … [接続][時計][フェーズ][コート] │ EMG STOP
RouteErrorBoundary > Outlet    画面本体
Toaster / WsSettings / EStopOverlay   重なる層
```

- **常設帯はヘッダー 1 本。フッターは持たない。** タブを別の帯にすると縦を 2 段消費し、
  1366×768 級ではその 1 段が操作領域を削る。折り返しもしない。縮んでよいのはタブ帯だけ
- ヘッダーの並び順は EMG STOP 誤爆防止から決まる（`docs/web/design.md`）
- **`RouteErrorBoundary` は `<Outlet />` だけを囲う。** ヘッダー・接続バナー・緊急停止
  オーバーレイは境界の外（1 画面が落ちても止める手段を残す）。`key` はパス（落ちた境界は
  タブを切り替えても解けない）
- **`AppShell` の `memo` は飾りではない**（`docs/web/data_flow.md`）
- ページ全体はスクロールさせない。スクロールするのは `Panel` の本文だけ

---

## 部品カタログ

`web/src/components/` は**誰が描くか**で 6 つ。**直下には何も置かない。**
barrel（`index.ts`）は作らず常に実ファイルまで指す（oxlint の `import/no-cycle` を効かせる）。

### `shell/` — 全画面へ出す外枠

| 部品 | 役割 |
|---|---|
| `AppHeader` | 常設帯 |
| `TabBar` | タブ 3 枚と注意喚起 LED |
| `Clock` | 現在時刻。**独立した部品であること自体が本体**（毎秒 `setState` するので、展開するとタブ帯ごと毎秒描き直される） |
| `ConnectionBanner` | 切断中の全幅バナー。ヘッダー右端の小さな表示では気付けない |
| `EStopOverlay` | 緊急停止中の全画面モーダル。**`onClose` を渡さない**ので Esc・背景クリックで閉じない。解除は `Reset` のみ。停止理由を必ず出す |
| `WsSettings` | 接続先の変更ダイアログ |
| `Toaster` | 通知の唯一のスタック（操作拒否 + ヘルス異常）。右下・最大 3 件 |
| `RouteErrorBoundary` | 画面本体の描画例外の境界 |

### `monitor/` — Dashboard 専用

| 部品 | 役割 |
|---|---|
| `StartGate` | 準備中の主役。開始可否と足りない項目名 |
| `MatchPrep` | コート・動作確認・指差喚呼を group ごとに並べる |
| `ChecklistItems` | 指差喚呼の 1 行。**どの群でも同じ見た目で描く唯一の場所**（配置は決めない） |
| `MatchControl` | `useResetConfirm`（リセット確認）と `MatchStrip` |
| `RobotStatusRow` | 試合中の 1 機ぶん |
| `EventFeed` | ヘルス変化の履歴 |

### `operator/` — RobotControl 専用

| 部品 | 役割 |
|---|---|
| `ModeSwitch` | 操作モードの帯。手動中は帯そのものを警告色にする |
| `ActionPanel` | 試合中の主役 |
| `TriggerButton` | 主操作の右の面（NEXT / RUNNING / DONE / 無効） |
| `SequenceStepList` | ステップ一覧。現在位置へ自動スクロール |
| `MatchTimer` | 試合残り時間。`shrink-0` |
| `ManualPanel` | 手動の操作面。軸の並びも可動範囲も配信をそのまま描く |
| `ManualAxisRow` | 手動の 1 軸。**行の単位は論理軸であってモータではない** |
| `ContinuousControls` | ジョグと長押しリピート |
| `AbsoluteEntry` | 絶対値入力。欄には現在の目標値を入れておく |
| `RangeBar` | 可動範囲の表示。**ドラッグできる入力にしてはならない**（掴んだ瞬間に機体が飛ぶ） |

`ManualAxisRow` の見た目は 3 通り。**分岐の根拠は配信された `manual` と `command_mode` だけで、
軸名は一切見ていない**（機構が変わっても UI は無変更）:

| 軸の性格 | 出すもの |
|---|---|
| `manual` を持つ連続軸 | ジョグ + 絶対値入力 + 可動範囲バー + プリセット |
| 離散状態アクチュエータ | プリセットのみ |
| `duty` 軸 | プリセットのみ。現在値は `—` |

### `motorcheck/` — 動作確認

| 部品 | 役割 |
|---|---|
| `MotorCheckButton` | 起動ボタン。**両ハンドで 1 つ**なので robot を取らない。確認はダイアログ |
| `MotorCheckPanel` | 進捗パネル。**モーダルにしてはならない**（駆動中に EMG STOP を覆う）。インライン展開 |
| `MotorCheckSummary` | 区分見出しに状態を 1 語。進捗と理由はパネルが出す |

### `diagnostics/` — 診断ツリー

| 部品 | 役割 |
|---|---|
| `SubsystemStatus` | 診断の累進的開示。平常時 1 行 |
| `HealthIndicator` | CAN バスの健全性。表示は 1 通りだけ |
| `SensorSummary` | センサ入力（原点スイッチ）。**モータ一覧には混ぜない** |
| `MotorSummary` / `MotorStatus` | モータ一覧と 1 基の数値行（見出しは一覧に 1 行だけ） |

**`SubsystemStatus` の開閉**は 3 つの入力で決まる:

| 入力 | 意味 |
|---|---|
| `manualOpen` | 操縦者が押した開閉。`defaultOpen` が**変わった周期だけ**追従（手で畳んだ状態は保たれる） |
| `forcedOpen` | 判定が `error` / `warning`、またはワーク落下の恐れ。**操縦者の操作を上書きして開く** |
| `showVerdict` | `false` なら畳む機能ごと持たない |

`defaultOpen` は初期値ではなく「**今このパネルを開いておくべきか**」の宣言。grid の同じ位置に
留まり再マウントされないので、`useState` の初期値として受けるだけだと試合中に手動へ入っても
畳まれたままになる。

| 呼び出し元 | `defaultOpen` | `showVerdict` | `onReenergize` |
|---|---|---|---|
| Dashboard 準備中 | — | `false` | — |
| `RobotStatusRow` | `true` | 既定 | — |
| RobotControl 準備中 | `true` | 既定 | あり |
| RobotControl 試合中 | 手動中のみ | 既定 | あり |

開いたときの並びは 判定理由 → ワーク落下 → 版番号未確認 → タスク失敗 → 安全機構 → CAN →
**センサ** → モータ。センサが先なのは、モータ一覧が残り高さまで伸びてスクロールするため
（後ろだと指差喚呼で見たい 1 行が隠れる）。

### `ui/` — 自前プリミティブ

**レイアウト骨格は CSS ではなくここが持つ。**

| 部品 | 提供するもの |
|---|---|
| `Page` | ページの外枠。**`display` を持たない**（呼び出し側が `flex flex-col` か `grid` を指定。両方あるとどちらが勝つかが Tailwind の出力順に依存する）。`overflow-hidden` + `min-h-0` |
| `Panel` | 枠を持つ唯一の単位。**入れ子にしない**。`legend` / `actions` / `accentTone` / `bodyClassName`。`card-body` は使わない |
| `Section` | パネル内の区切り。罫線 1 本 + 小見出し |
| `Button` | 7 トーン。`next` だけ面を塗る。無効時の文字色を daisyUI 既定から戻す |
| `StatusBadge` | **状態表示の唯一の形** |
| `Kbd` | キーヒント |
| `Icon` | lucide の既定値。**`absoluteStrokeWidth` は使わない**（`size` が文字列だと線が消える） |
| `Modal` | `<div>` 版の daisyUI modal。**`<dialog>` を使わない**（Esc で必ず閉じる）。`onClose` を渡さなければ構造的に閉じられない |

---

## 足すときの規則

- **モータ名・軸名・ステップ名を UI へ書かない。** 配信をそのまま描くので、機構が変わっても無変更
- 部品は 6 バケツのどれかへ。直下には置かない
- 新しい状態表示を足す前に、その事実が既にどこかに描かれていないか確認する
- 同じ記号に 2 つの意味を持たせない（`docs/web/design.md`）
- 判定を画面側で組み立て直さない（`docs/web/data_flow.md`）
