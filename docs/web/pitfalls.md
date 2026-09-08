# 踏みやすい罠 — 何で壊れ、何が守っているか

**ここに並ぶのは全部、実際に踏んだもの。** UI を触る前にこの 1 枚を読む。

`web/` はテストが本体より多い（本体 8,545 行 / テスト 10,366 行・52 ファイル）。大半は
「見た目の確認」ではなく、**過去に壊れた不変条件を固定するため**にある。

---

## 一覧

| 罠 | 症状 | 守っているテスト |
|---|---|---|
| daisyUI のクラスを片方だけ書く | DOM には居るのに**何も見えない** | `lib/daisyPairs.test.tsx` / `ui/Modal.test.tsx` |
| クラス名を実行時に組み立てる | CSS ごと出力されず色が付かない | `lib/daisyPairs.test.tsx` |
| grid の `self-start` を flex-col へ書き写す | パネルが 157px に潰れ、隣は 424px に膨らんで画面外へ | `pages/RobotControl.test.tsx` |
| `shrink-0` を付け忘れる | 異常時に展開した瞬間、試合時間の数字に文字が重なる | `pages/RobotControl.test.tsx` |
| 受信境界で `?? []` | ラッチしているのに画面は平常 | `lib/protocol.test.ts` / `lib/healthVerdict.test.ts` |
| 検査なしで `.length` を呼ぶ | **全画面が白くなる** | `lib/healthVerdict.test.ts` |
| 判定を 2 箇所に書く | 同じ瞬間にパネルが「完了」、隣が「未実行」 | `lib/motorCheckStatus` 系 |
| context 分割だけで memo を付けない | 外枠が毎秒 40 回描き直される | `layouts/RootLayout.test.tsx` / `context/RobotContext.test.tsx` |
| `send` の戻り値を捨てる | 押したのに機体は動かず、トーストも出ない | `pages/RobotControl.test.tsx` |
| 契約を手で書き写す | ヘルス異常が 100% 捨てられたまま両側のテストが緑 | `test/wsContract.test.ts` |
| リダイレクトをルーター生成より後に置く | 旧ブックマークが全部 Monitor に落ちる | `App.test.tsx` |
| 実測値があるモータに指令値を出す | 停止中の POS に指令値が出る | `diagnostics/MotorStatus.test.tsx` |

---

## Tailwind / daisyUI

### クラス名を実行時に組み立ててはならない

Tailwind は**ソース中に現れた文字列ぶんしか CSS を出力しない**。

```ts
TONE_BORDER_CLASS[t].replace("border-", "border-l-")   // ✗ 走査から漏れ、CSS ごと消える
TONE_BORDER_L_CLASS[t]                                  // ○ lib/tone.ts にリテラルで持つ
```

### クラスは「対」で書く

daisyUI のコンポーネントは「親クラス + 修飾子」が揃って初めて成立する。片方だけだと
**可視化ルールごと CSS から消える**。

```tsx
<div className="modal-box">                                      {/* ✗ 既定の opacity:0 だけが残る */}
<div className="modal modal-open"><div className="modal-box">    {/* ○ */}
```

`.modal-box` は既定が `opacity:0; scale:.95` で、可視化するルールは `.modal-open` 側にしかない。
**これで、DOM には居るのに何も見えないモーダルを出荷しかけた。**

同種の罠は `badge` / `status` / `alert` / `progress` 全般にある。対は `web/src/lib/tone.ts` に
**揃った 1 本の文字列**として持ち、`lib/daisyPairs.test.tsx` が全トーンぶん機械的に検査する。
**トースト側にローカル定義を持たせない。**

### 既定を上書きしたい箇所は明示のユーティリティを書く

ビルド後のレイヤ順は `... < utilities < daisyui` に見えるが、**実測ではユーティリティが勝つ**。
一方でユーティリティを書いていない属性は daisyUI の既定がそのまま残る。

特に `:disabled` は既定が「地 base-content 10% / 文字 20%」で、`⊘ 準備中` `RUNNING` `✓ DONE` の
ように**状態表示を兼ねる無効ボタン**が読めなくなる（`ui/Button.tsx` の `DISABLED_CLASS` で
上書き済み）。配色を変えたら実機描画で確認する。

### サイズ修飾子は font-size まで固定する

`card-xs` や `table-xs` は padding だけでなく本文の `font-size` も指定するので、ルートの
`clamp()` スケーリングから**その部分だけが外れる**。`card-body` は使わない（`ui/Panel.tsx` は
枠にだけ `card card-border` を使う）。`table-xs` はセル側に `text-[0.85em]` を当てて打ち消す。

---

## レイアウト

### grid の子は既定で縦に伸びる。`shrink-0` では止まらない

内容ぶんの高さに留めるには **`self-start`**。落とすと、中身が数行しかないカードが全高の白い箱になる。

### ただしこれは grid の話。flex-col の子へ書き写してはならない

`align-self` が効くのは**クロス軸**で、grid では縦だが **flex-col では横**。flex-col の子に
`self-start` を付けると幅が `stretch`（列の幅）から `fit-content` へ落ち、内容が幅を決める。

**flex-col の子は主軸（縦）にはそもそも伸びないので、高さを留める目的の `self-start` は
1 つも要らない。**

試合中の操縦者画面の右カラム（`flex min-h-0 flex-col`・幅 358px）で実際に取り違えた:

| 部品 | 何が起きたか |
|---|---|
| `MatchTimer` | 中身が `0:00` の 1 行しかないので **157px** まで縮み、font-size 3.4em の数字が枠からはみ出した |
| `SubsystemStatus` | min-content が列より広く **424px** へ膨らみ、右端がビューポートを **58px** 越えて TMP 列とヘルスチップが読めなくなった |

**症状が縮む側と溢れる側の両方に出る**ので、片方だけ見ても原因にたどり着きにくい。

### 縦が足りないときに縮む側は明示する

flex の既定（`flex-shrink: 1`）は列の全パネルを一律に縮める。

- 削れる余地の無いパネル（`MatchTimer`）には **`shrink-0`**
- **内部スクロールを持つパネル**（`SubsystemStatus` のモータ一覧）に溢れを引き受けさせる

落とすと、機体状態が異常時に自分から展開した瞬間に試合時間の caption が数字に重なる ——
**試合中に最も参照する値が、最も要るときに消える。**

### ページ全体はスクロールさせない

`Page` が `overflow-hidden` + `min-h-0` を持つ。スクロールしてよいのは `Panel` の本文だけ
（`.scroll`）。`Page` が `display` を持たないのは、呼び出し側の `grid` と衝突させないため
（どちらが勝つかが Tailwind の出力順という不安定な要因に依存する）。

---

## 受信境界

### `?? []` のような黙った既定値を置かない

埋めると「ラッチしているのに画面は平常」へ化け、**埋めたこと自体が画面から読めなくなる**。
読めなかった配信は `MALFORMED` として異常側へ倒す（`docs/web/data_flow.md`）。

これで `safety` の 1 欄が落ちただけで**全画面が白くなった** —— `describeSafetyIssues` が無検査で
`.length` を呼び、レンダー本体なので React ツリーごとアンマウントした。

### 判定を 2 箇所に書かない

`MotorCheckPanel` と `MotorCheckSummary` に完了判定が別々に書かれ、実際に食い違っていた ——
パネル側は「実行中でなく、エラーも無く、ステップ表が届いている」を完了と読むので、
**一度も実行していない状態が「完了」**になり全ステップに緑の ✓ が付いた。
`config/checklist.yaml` の「アクチュエータ動作確認 完了」は、その誤表示のままチェックが付く
経路だった。**ステップ数 0 は「未読込」であって完了ではない。**

### サーバーの判定より楽観的にならない

`health.overall` はヘルス計算そのものが失敗したときに `down` へ倒れる。内訳だけを見て
「異常なし」を出すと、サーバーのフェイルセーフが画面上で消える。同じ形の誤りを `StartGate` でも
一度やった。

---

## 状態と再描画

### context の分割だけでは効かない

`AppShell` の `memo` へテレメトリ由来の props を渡した瞬間、外枠が毎秒 40 回描き直される状態へ
戻る（仕組みは `docs/web/data_flow.md`）。守っているのは `context/RobotContext.test.tsx` の
「20Hz × 2 台ぶん (毎秒 40 通) の配信でも低頻度側は再描画されない」と
`layouts/RootLayout.test.tsx` の「state 配信で外枠を再描画しない」。

### `send` の戻り値を捨てない

切断中の `send` は `false` を返して黙る。**送信は必ず `sendOrReport` を通す。**

### 旧ハッシュ URL の読み替えはルーター生成より前

`createBrowserRouter` は**生成時点の location を読む**。`App.tsx` の 2 行の間に処理を挟んだり
入れ替えたりしない。崩れると `#main-hand` 等の旧ブックマークが全て Monitor に落ちる
（各操縦者は担当タブの URL をブックマークして試合に臨む）。

---

## テストの作法

**テストを足した／張り替えたら、本番コードにわざと 1 行の不具合を入れ、狙ったテストが落ちる
ことを確認してから元に戻す。** 落ちなければ、そのテストは「実装が正しいこと」ではなく
「実装が存在すること」しか見ていない。行数もケース数も、噛んでいることの証拠にならない。

多重防護の層は **1 枚ずつ単独で確かめる**。統合経路のテストでは 1 枚壊しても他が拾ってしまう。

契約フィクスチャ（`ws-contract.json`）は手で書かない。整形もしない（`.prettierignore` で
oxfmt の対象外）。両側が自分のサンプルを持つと、契約が食い違ったまま**両方のテストが緑になる**。

ヘルパは `test/robotContext.tsx`（Provider でくるむ）/ `test/mockWebSocket.ts` /
`test/motorState.ts`。テスト本体は**対象ソースの隣**に `*.test.ts(x)`。

---

## 検証

```bash
cd web && pnpm check        # lint + format + 型検査 + テスト（これが合否）
cd web && pnpm test:run     # vitest を 1 回だけ
cd web && pnpm dev          # 実機描画（配色・レイアウトは目で見る）

UPDATE_WS_CONTRACT=1 uv run pytest tests/test_ws_contract.py   # 配信を変えたら再生成
```

**レイアウトを触ると `RootLayout.test.tsx` と `RobotContext.test.tsx` が落ちることがある** ——
props 経路の変更が再描画回数に出るため。落ちたら「テストを実装に合わせる」前に、外枠が毎秒
40 回描き直される形になっていないか確かめる。

配色とレイアウトはテストだけでは足りない。過去に見つかった不具合はどれも実描画でしか出なかった。
