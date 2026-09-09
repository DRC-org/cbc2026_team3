# 見た目と操作 — どう見せ、どう操作するか

**「今どうなっているか」だけ。** 理由は `docs/invariants.md` §8、経緯は `docs/history/` にある。
画面の構成は `docs/web/screens.md`、壊れやすい点は `docs/web/pitfalls.md`。

---

## 配色

正は `web/src/index.css` の daisyUI カスタムテーマ `cbc`。**組み込みテーマは持ち込まない**
（`themes: false`）。個別コンポーネントに色の生値を書かない。

ライト基調・**角丸 0・影 0**。画面はほぼ全面がパネルなので、彩度が高いと全体が原色で覆われ
長時間の注視に耐えない。1px の実線 1 本でパネル境界を表す情報密度優先のレイアウトに合わせた。

| 変数 | 値 | 使い所 |
|---|---|---|
| `--color-base-100` | `#ffffff` | パネル面 |
| `--color-base-200` | `#eef1f4` | 地 |
| `--color-base-300` | `#d5dbe2` | 罫線・表の縞・入力欄 |
| `--color-base-content` | `#181b1f` | 文字 |

状態色 4 つ。**「白抜き文字を載せて AA (4.5:1)」と「`badge-soft` の淡色地に載せて AA」の
両方**を満たす値を実測して選んである（ライト地では彩度の高い amber や green はそのままでは
文字として読めない）。

| トーン | 値 | 意味 |
|---|---|---|
| `success` | `#2f7a4f` | 正常・実行可 |
| `warning` | `#96620a` | 要確認・許可待ち・手動操縦中 |
| `error` | `#b3372c` | 異常・赤コート |
| `info` | `#25667f` | 実行中・青コート・情報 |

`primary` / `secondary` / `accent` / `neutral` に独自の意味は持たせない。取りこぼした
daisyUI 既定クラスが原色で浮かないよう既存の状態色へ寄せてあるだけ。

### 例外の 2 色（`@theme`）

| 変数 | 値 | 理由 |
|---|---|---|
| `--color-estop` | `#c62d1f` | 緊急停止。他の何よりも先に目に入る必要がある |
| `--color-next` | `#d98c00` | NEXT。周辺視野で見つかる必要がある。`warning` は文字として読める明度まで落としてあり**面塗りには暗すぎる** |

### 状態はチップで示す

`ui/StatusBadge.tsx` に一本化。地・枠・文字の 3 点で示すので、文字色の明度を犠牲にせず
目立たせられる。**着色テキストを新しく書かない。**

クラスの正は `web/src/lib/tone.ts`。**daisyUI のクラスは対で書く**（`badge badge-soft
badge-success` のように揃った 1 本の文字列で持つ。理由は `docs/web/pitfalls.md`）:
`TONE_BADGE_CLASS` / `TONE_STATUS_CLASS`（正方形 LED）/ `TONE_BORDER_L_CLASS`（アクセントバー）/
`TONE_ALERT_CLASS`（トースト）/ `TONE_TEXT_CLASS` / `TONE_PROGRESS_CLASS`。

フェーズとコートの対応は `web/src/lib/phase.ts`。**読めなかった配信（`MALFORMED`）も語彙の
1 つとして持つ** —— 索引が `undefined` になるとチップが無地・無文字で消える。

ヘッダーの地はフェーズ色で塗らない。**左端のバーとチップだけ**で示す（全面を塗ると画面で
最も明るい面になる）。

### ON のトグルは `success` の面塗り 1 種類

`SuctionPadPanel`（次の吸着で使う弁の宣言）と `OnOffPadGroup`（弁の今すぐ開閉）はどちらも
`border-success bg-success text-success-content` で ON を示し、OFF は既定のボタンのまま置く。
2 つの面が同じ弁を別の色で描くと、同じ画面の 2 箇所で ON の意味が食い違う。

**`OnOffPadGroup` の丸（`rounded-full`）は角丸 0 の唯一の例外。** 6 個以上が横に並ぶ群で、
狙う対象を形で見つけられることを優先している。**未指令（`target` が `null`）は OFF の色では
なく破線の輪郭**で描く —— 塗り分けだけだと「まだ押していない」と「OFF を送った」が同じ絵に
なる。

---

## 文字とサイズ

`html { font-size: clamp(13px, min(1.05vw, 2.1vh), 20px) }` で**幅と高さの両方に追従**させる
（20px 固定では 1366×768 級でパネルが画面外へ溢れた）。Tailwind の余白は rem 基準なので、
ここを起点にすると余白も追従する。

**この追従から外れる書き方をしない** —— `card-xs` / `table-xs` はサイズ修飾子が `font-size` も
固定する（`docs/web/pitfalls.md`）。アイコンも `size="1em"`。

フォントは `@fontsource-variable/*` で**自己ホスト**（`main.tsx` が読む）。Google Fonts の
`<link>` は解決に失敗しても無言で素の sans-serif に落ちるので、会場のネットワーク次第で
当日まで気付けない。`--font-sans` = Inter + Noto Sans JP、`--font-mono` = JetBrains Mono。

### ラベルは英大文字と日本語で役割を分ける

**英大文字を使うのは、試合中に頻度高く押す主操作とその枠を占める状態表示だけ**:
`START` / `NEXT` / `STOP` / `RUNNING` / `DONE` / `RESET` / `EMG STOP`。
それ以外はすべて日本語（「先頭から再開」「シーケンス未取得」「試合開始前」）。

周辺視野では文字を読まず形で拾うので、**大文字であること自体が「これは今押すボタンだ」の印**
になる。装飾として増やすとその印が薄まる。

**英字でも大文字でなければこの印にはならない**ので、タブ名（`Monitor` / `Main Hand` /
`Sub Hand`）・接続表示（`Connected` / `Disconnected`）・緊急停止の解除（`Reset`。オーバーレイと
`EStopBanner` の両方）は今までどおりでよい（前 2 つは操作ではなく名前、最後は緊急停止を
解除する唯一の口）。

---

## アイコン

`lucide-react`。既定値（`size="1em"` / `strokeWidth={1.75}` / `shrink-0` / `aria-hidden`）は
`ui/Icon.tsx` に閉じ込め、各所で個別指定しない。

**同じ記号に 2 つの意味を持たせない。** アイコンは読まずに形で引くので、1 つの記号が 2 つの
状態を指すと、区別が付いた気になったまま取り違える。

| 記号 | 意味 | 出る場所 |
|---|---|---|
| `OctagonX` | 緊急停止 | `AppHeader` / `EStopOverlay` / `EStopBanner` |
| `Hand` | **許可待ち / ここで停止** 専用 | `ActionPanel` / `SequenceStepList` / `RobotStatusRow` |
| `SlidersHorizontal` / `Workflow` | 手動 / 半自動 | `ModeSwitch` |
| `Play` | START（先頭から） | `ActionPanel` / `StartGate` |
| `ArrowRight` | NEXT（続けて走る） | `TriggerButton` / `ActionPanel` |
| `Square` | 停止（STOP・中断） | `ActionPanel` / `MatchStrip` / `MotorCheckPanel` |
| `Check` | 完了 | `TriggerButton` / `SequenceStepList` / `MatchPrep` / `MotorCheckPanel` / `HomingPanel` |
| `Ban` | 操作不可 | `TriggerButton` / `ActionPanel` |
| `RotateCcw` | やり直し・解除 | `MatchControl` / `MatchPrep` / `EStopOverlay` / `EStopBanner` |
| `EyeOff` | 開発用の非表示（`--dev-tools` 時だけ現れる） | `EStopOverlay` |
| `Zap` | 開発用の一括チェック（同上） | `MatchPrep` |
| `Activity` | 動作確認の起動 | `MotorCheckButton` |
| `Crosshair` | 零点合わせの起動 | `HomingButtons` |
| `Ruler` | 作動点測定の起動 | `SwitchMeasurePanel` |
| `Send` | 絶対値入力の送信 | `AbsoluteEntry` |
| `Minus` / `Plus` · `ChevronsLeft` / `ChevronsRight` | ジョグ · 可動端へ | `ContinuousControls` |
| `ChevronRight` / `Pause` / `Circle` | ステップ一覧の 現在 / 許可待ち / 未到達 | `SequenceStepList` |
| `TriangleAlert` | 警告 | 各所 |
| `OctagonAlert` | `error` トースト | `Toaster` |
| `CircleAlert` / `CircleHelp` / `Info` | 開始できない理由 / 押せない理由 / 補足 | `StartGate` / `MotorCheckButton` / `MatchPrep` |
| `ShieldAlert` / `ShieldQuestion` / `PackageX` / `ListX` | 安全機構の異常 / 版番号未確認 / ワーク落下の恐れ / タスク失敗 | `SubsystemStatus` |
| `ListMinus` | 除外ステップ | `MotorCheckPanel` |
| `X` | 軸の失敗 / 通知を閉じる | `HomingPanel` / `Toaster` |
| `ChevronDown` / `ChevronRight` | 開閉 | `SubsystemStatus` / `MotorCheckPanel` |

`Hand` はかつて手動操縦にも使っており、**試合中に手のアイコンを見てもトリガー待ちか手動操縦か
判別できなかった**。`Play` と `ArrowRight` も同じ理由で分ける（同じ位置に同じ大きさで交互に
出るので、記号まで同じだと色と文字でしか見分けられない）。

---

## 静と動

**平常時は静か、異常時に自分から主張する。** `SubsystemStatus` は平常時 1 行に畳み、異常時は
操縦者の開閉操作を**上書きして**開く。同じ部品でも役割で既定を変える（呼び分けの表は
`docs/web/screens.md`）。

**点滅（`.alert-blink`）は異常時にだけ画面へ出る要素に限る** —— `EStopOverlay` と
`ConnectionBanner`、そのオーバーレイを隠しているあいだだけ出る `EStopBanner` だけ。平常時に
点滅している要素が 1 つでもあると「異常時に自分から主張する」が成立しない。
ヘッダーの EMG STOP は赤地・大面積・固定位置なので外しても見つけにくくならない。
付ける先も**先頭の記号まで**。`prefers-reduced-motion: reduce` では緊急停止も例外にしない。

**機体が動いているあいだ画面を覆わない。** モーダルにしてよいのは、押す前に出して押した瞬間に
閉じる確認だけ。動作確認の進捗（`MotorCheckPanel`）がモーダルで自動的に開いていたとき、
**両ハンドの全アクチュエータが駆動されているあいだずっとヘッダーの EMG STOP が押せなかった**
（クリックは背景に吸われ、`ModalProvider` の `openCount` はホットキーも封じる）。
止める手段だけが画面から消える壊れ方で、操縦者には「押したらパネルが閉じた」としか見えない。

---

## キーボード

`web/src/hooks/useHotkeys.ts`。キーは `KeyboardEvent.key`（スペースは `" "`）、修飾キー併用は
`Shift+Home` の形。

| キー | 操作 | 効く条件 | 持ち主 |
|---|---|---|---|
| `1` `2` `3` | タブ切替 | 常時 | `RootLayout`（`TABS` から組む） |
| `Space` | START / NEXT | 試合中 **かつ 半自動** | `RobotControl` |
| `↑` `↓` | 手動の軸選択 | 手動・連続軸が 2 本以上 | `ManualPanel` |
| `←` `→` | ジョグ（長押しで加速） | 手動・**選択中の行だけ** | `ContinuousControls` |
| `[` `]` | ジョグのステップ量 | 同上 | `ContinuousControls` |
| `Shift+Home` / `Shift+End` | 可動端へ | 同上 | `ContinuousControls` |

- **`Space` は手動モード中は無効。** 誤爆すると手動で機構を動かしている最中にシーケンスが
  走り出す。これは UI 側の即応性のためで、唯一の防御ではない（サーバーにも
  `CommandSpec.blocked_during_manual` がある）
- **端への移動だけ修飾キーを併用する。** 1 打で軸が可動端まで走る唯一の操作で、しかも
  `Home` / `End` はジョグの `←` `→` と同じクラスタにある（ノート PC では `Fn+←/→` が
  そのまま `Home/End` になる機種が多い）
- ジョグのキー割当は**選択中の行だけが張る**。全行が張ると、どの軸へ飛ぶかが登録順という
  画面から読めない事情で決まる

**発火しない条件**（`isHotkeyBlocked`）: 修飾キー併用・キーリピート・入力欄・モーダル表示中。
判定は `ModalContext` の表示中モーダル数で行い、CSS クラスの DOM 検索に依存しない。
**押しっぱなしのジョグ（`useHoldKey`）もこの判定を共有する。**

**凡例は「そのキーが効く場所」にしか置かない。** 数字キーはタブ自身が、`Space` は
START / NEXT ボタン自身が `<Kbd>` として持つ。離れた場所に一覧を作ると ①同じ事実を 2 度描く
②割当を変えたときに一覧だけが古くなる。

### 長押しリピート

ポインタは `useHoldRepeat`、キーは `useHoldKey`。加速の engine は共通（`useRepeatController`）。
`HOLD_DELAY_MS` 400ms → `HOLD_INTERVAL_MS` 150ms 間隔 → `HOLD_ACCEL_EVERY` 6 回ごとに倍。

**止める側を多重に張る。** 止める経路は `pointerup` だけではない —— ボタンの外へドラッグして
離す・ブラウザがポインタ操作を取り消す・タブが切り替わってフォーカスを失う・**部品が
アンマウントされる**。1 つでも拾い損ねると**指を離したのに機体が動き続ける**。

**`setPointerCapture` を使ってはならない** —— `pointerleave` が飛ばなくなり、ボタンの外へ
逃がして止める経路が消える。最後の砦は可動範囲のクランプで、連続発火は必ず端で止まる。
**伸びた量は呼び出し側が画面に出すこと**（読めないまま動く距離だけ変わると次の 1 押しを
予測できない）。

---

## 確認の取り方

**3 通り。基準は「何を失うか」と「時間の余裕があるか」。コマンド名が同じことを理由に
揃えてはならない。**

| 取り方 | 使う操作 | 理由 |
|---|---|---|
| **二度押し**（`useArmedPress`） | 試合開始（`StartGate`）/ 試合終了（`MatchStrip`） | 計時の開始点と、残り時間との勝負の最後。ダイアログは押したボタンから離れた位置に出るので往復が挟まる。この 2 つだけその数百 ms を払わない |
| **モーダル** | 動作確認の起動 / 零点合わせ / 作動点測定 / 準備中の RESET / 先頭から再開 / ステップジャンプ | 押した瞬間に機体が動く、または取り返しの付かないものを捨てる。**準備中なので時間の余裕がある** |
| **確認なし** | 通常停止（STOP）/ 試合後の「セッティングへ戻る」 | 前者は安全側の動作で止めるまでの時間を延ばさない。後者は試合後の唯一の進み先で、失うのは消化済みのチェックリストだけ |

### 二度押しの内訳

誤爆を防ぐのは 2 つの時間で、**どちらが欠けても確認にならない**:

| 定数 | 値 | 無いとどうなるか |
|---|---|---|
| `ARM_GUARD_MS` | 400ms | ダブルクリック 1 回がそのまま二度押しとして成立する |
| `ARM_TIMEOUT_MS` | 4000ms | 武装したまま忘れられたボタンが「次に触れた 1 回で試合が始まる」状態で残る |

- 不感時間中の 2 回目は**捨てるだけで武装は解かない**（解くと連打が永久に 1 回目へ戻る）。
  自動解除までの残り時間も引き直さない
- 表示上 `guard` と `armed` を区別しない（400ms だけ見た目が変わると「押せたり押せなかったり」に見える）
- **武装は押した瞬間の状況に紐づく。** 切断・指差喚呼の解除・フェーズ遷移で `disarm()` する
- ダイアログ本文が持っていた情報（コート・機体が動く条件・「緊急停止ではない」）は
  **武装中の表示へ移すこと。** 落とすと二度押しは単なる連打になる

**モーダルの中身は「押すと何が起きるか」を書く場所**であって飾りではない。
`MotorCheckButton` が「**両機**の可動範囲に人・物がないこと」と書くのは、動くのが両機だから。
文面が古いままだと操縦者は片方の機体しか見ずに開始する。

---

## EMG STOP の誤爆を防ぐ配置

**誤爆の向きは「隣のボタンを押そうとして EMG STOP を踏む」。** EMG STOP は赤地・大面積で常に
同じ位置にあるので、狙って外すのは隣の小さいボタンの側。試合中に踏むと機体は止まりシーケンスは
降りる。

1. **押せるものを EMG STOP から遠い側へ、押せないものを近い側へ寄せる**
2. **EMG STOP 手前の余白は緩衝。詰めてはならない**（`AppHeader` の `ml-6`）
3. ヘッダー直下の帯の操作ボタンは**帯の先頭**に固定する（`ModeSwitch`）。文言がモードで変わる
   ものを左に置くと、ボタンの横位置がモードごとに動く（2 択セグメントの並びと文言は変えない）
4. 画面全幅の要素の右端には押せるものを置かない

```
[タブ帯] ………… [接続][時計] [フェーズ][コート]  │ (緩衝) │ EMG STOP
 ↑押せる・最も遠い  ↑押せる  ↑押せない                    ↑最大・固定位置
```

タブ帯が最左なのは、画面の隅がポインタで最も当てやすく、同時に EMG STOP から最も遠いため。
フェーズとコートを隣に置くのは対の情報だから（両端へ離すと試合設定を 2 回に分けて読むことになる）。

**適用範囲は「ヘッダー直下の最上段」と「画面全幅の要素の右端」に限る。** `ActionPanel` の
「左が STOP・右が主操作」は EMG STOP から縦に遠いので従来どおり。

---

## 通知

`components/shell/Toaster.tsx` に一本化（操作拒否 + ヘルス異常）。右下に最大 3 件
（`MAX_TOASTS`）、古いものから押し出される。トーンは `warning` と `error` の 2 つ。
コンテナは `pointer-events-none` で下の操作を透かす。

**理由文は省略せず折り返す。** 次の一手が書かれているので切ると読めなくなる。カードは幅
`22rem` 固定で縦に伸びる（`docs/web/pitfalls.md` の「daisyUI の `.alert` は grid」）。

**トーストは履歴ではない** —— 数秒で消えるので、Monitor の試合中は `EventFeed` が残す。
