# 会場カード — 詰まったときの手順

**試合当日、手が止まったときにこの 1 枚だけを見る。** 設計の理由は `CLAUDE.md`、
今どうなっているかは `docs/checks_and_health.md`、経緯は `docs/impl_plan.md`。
ここに書くのは**手順と、その手順を打つ理由の 1 行**だけ。

前提: リポジトリは `~/cbc2026_team3`（`cd` してから打つ）。systemd の unit 名は
`cbc-control` / `cbc-can` / `cbc-can-watchdog`。

---

## 0. まず状況を 1 行で掴む

```bash
systemctl status cbc-control cbc-can cbc-can-watchdog --no-pager
ip -brief link show type can          # 何本 up しているか
```

`cbc-control` が **`failed`** なら → §1。`active` なのに画面が変なら → §3。

---

## 1. 制御プログラムが起動しない / 落ちて上がってこない

### 1-1. 何が起きているか

`main.py` が open するのは**そのロボットが実際に使うバスだけ**（`_robot_bus_names`）。
したがって欠けた 1 本がどれかで結果が変わる。

| 欠けたバス | メインハンド | サブハンド | 既定構成（両方読む）の結果 |
|---|---|---|---|
| `can_m3508` | ✗ | 起動する | 起動しない |
| `can_dm3520` | 起動する | ✗ | 起動しない |
| `can_edulite` / `can_generic` | ✗ | ✗ | 起動しない |

**既定は両ハンドを読むので、どの 1 本が欠けても起動しないことに変わりはない。**
そこへ `cbc-control.service` の再起動制限
（`StartLimitIntervalSec=60` / `StartLimitBurst=3` / `RestartSec=2`）が掛かるため、
**約 6 秒でユニットが `failed` に固定され、以後 `systemctl start` すら通らなくなる**。

> `Job for cbc-control.service failed because start of the service was attempted too often.`

**この状態は「直したのに起動しない」形で現れる。** USB を挿し直しても、
`reset-failed` を打つまで起動しない。

### 1-2. 手順

```bash
# ① 欠けているバスを特定する（試合前点検と同じコマンド）
scripts/setup_can.sh --strict
#    -> 「デバイスが見つかりません」の行に出たバス名が欠けている本数ぶん出る
#    -> 「serial 未採取」なら config/can_buses.yaml が TBD のまま = 別の問題

# ② 該当の CANable を挿し直す（ハブ経由なら PC 直挿しに変える）。
#    バス名は udev が STM32 UID 由来の serial で固定するので、
#    どのポートに挿しても名前は変わらない
scripts/setup_can.sh --strict          # 4/4 になるまで ①② を繰り返す

# ③ failed のラッチを外してから起動する（②だけでは起動しない）
sudo systemctl reset-failed cbc-control
sudo systemctl start cbc-control

# ④ 立ち上がったか
journalctl -u cbc-control -n 50 --no-pager
```

### 1-3. それでも揃わないとき（最後の手段）

**欠けたまま試合に出るなら、動かす範囲を config で絞る。** 開くバスはロボット単位で
決まるので、**そのバスを使わないほうのハンドは config を絞るだけで起動する**。

```bash
sudo systemctl stop cbc-control        # service ではなく手で起動する

# (a) 片方のハンドだけで出る — 欠けたバスを使わないほうを渡す
#     can_m3508 が欠け -> sub_hand は動く / can_dm3520 が欠け -> main_hand は動く
#     can_edulite・can_generic はどちらのハンドも使うので (a) では逃げられない
uv run python main.py --config config/main_hand.yaml
uv run python main.py --config config/sub_hand.yaml

# (b) 机上ベンチの一式で、生きている基板だけを確かめる
uv run python main.py \
  --system config/bench/servo/system.yaml \
  --config config/bench/servo/main_hand.yaml \
  --checklist config/bench/servo/checklist.yaml
#    対象は m3508 / edulite / main_hand / dc / servo / solenoid / dm3520 /
#    y_axis_tuning の 8 セット
#    main_hand だけは CANable 3 本 (can_m3508 + can_edulite + can_generic) を
#    同時に要求する。
#    会場で 1 本欠けているときに選んではならない (起動しない)

# (c) 機体を動かさずに UI と手順だけ確認する
uv run python main.py --dry-run
```

- **(a) は機体が半分しか動かない。** どの軸が死ぬかを操縦者 2 名と Monitor で
  声に出して合わせてから走らせること
- **(c) の `--dry-run` は virtual バスなので EDULITE の励磁 WARNING が必ず 2 件出る。**
  これは正常（`docs/impl_plan.md` 「未解決の課題」）

---

## 2. 会場で UI を直して反映したい

```bash
scripts/deploy.sh --no-install
```

**`--no-install` を付ける。** 素の `deploy.sh` は `uv sync --frozen` と
`pnpm install --frozen-lockfile` を無条件で走らせ、ロックが満たされていなければ
そこでネットワークを取りに行く。会場の回線でそれをやると依存解決で止まる。

**会場入りの前日までに、ネットワークのある場所で素の `scripts/deploy.sh` を
一度回してキャッシュを温めておくこと。** `--no-install` は `web/node_modules` が
無ければその場で落ちる（vite のエラーではなく、理由の読めるメッセージで落とす）。

ビルドだけしてサービスに触りたくないときは `--no-restart` を足す。

---

## 3. 動いているのに様子がおかしい

### 3-1. journal に `[ WD ]` が出た → **吸着ワークの落下を疑う**

`cbc-can-watchdog.service` は bus-off から復旧するために `ip link` の **down/up** を
打つ。その 1 秒弱のあいだ CAN が止まるので、**自作モタドラ 3 枚のウォッチドッグ
（`command_timeout_ms` 既定 500ms）が満了しうる**。電磁弁基板は満了で全 ch を OFF に
倒す設計なので、**吸着で保持していたワークはその場で落ちる**。

```bash
journalctl -u cbc-can-watchdog -f      # [ WD ] ... down/up で復旧を試みます
```

- 見たら**まずワークの有無を目で確かめる**。UI には「落ちた」と出る手段が無い
  （電磁弁基板は弁が開いたかを観測できないので、到達も落下も報告しない）
- 復旧自体は正しい動作なので止めない。止めると bus-off から戻る手段が無くなる
- 誤発火はしにくい（backlog が残っている **かつ** TX が 3 周期進まない、の AND）。
  出続けるなら本当にそのバスが詰まっている

### 3-2. UI は「接続中」なのにモータが赤い（STALE）

**赤いのが「バス丸ごと」か「自作モタドラ 1 枚ぶん」かで見る場所が違う。**

**(a) そのバスのモータが全部赤い** — 受信が止まっている。`state.health` でバスが
`DOWN` なら受信ループが `rx_down` を立てている。`scripts/setup_can.sh` を打ち直せば
同じ socket のまま復帰する（受信ループは down/up を跨いで生き残る）。

**(b) 自作モタドラ 1 枚のチャンネルだけが揃って赤い** — **基板の LED を見る。**
赤の速い点滅なら **CAN 不通かデバイス ID 未設定**（DIP の回しすぎ）である。

| 見えるもの | 意味 | 手当て |
|---|---|---|
| LED 赤・速い点滅 | CAN 不通 / デバイス ID 未設定 | CAN の配線 → DIP の基板番号（**0〜7**。8 以上は全 ch 未設定）の順に見る |
| LED 正常なのに STALE | 配線・電源・ファーム焼き忘れ | `config/<robot>.yaml` の `can_id` が実在するスロットかを確かめる |

**PC 側から「デバイス ID 未設定」は分からない。** 未設定のチャンネルは `FEEDBACK` を
1 通も送らないので（仕様書 §2.2）、症状は配線不良と完全に同じ「全 ch STALE」になる。
**この切り分けは LED でしかできない。**

`0x40`台 = サーボ基板 / `0x80`台 = DC 基板 / `0xC0`台 = 電磁弁基板なので、
赤いモータの `can_id` を見ればどの基板を見に行けばよいかが分かる。

### 3-3. 動作確認が「センサが応答していません」で止まる

原点センサ（`origin_sensor`、サーボ基板 #0 SV4 = `0x44`）が FEEDBACK を返していない。
配線・基板の電源・ファームの焼き込みを順に見る。**センサの配線不良と、そもそも
存在しないスロットを登録している場合とで症状が同じ**なので、`config/main_hand.yaml`
の `sensors:` の `can_id` が実在するスロットかを先に確かめる
（サーボ基板 1 枚は SV0〜SV4 の 5 本しか無い）。

### 3-4. 緊急停止が解除できない

- 物理スイッチ（DC 基板の `REF`）は**ラッチ**なので、離しても自動では戻らない。
  解除は UI の Reset（CAN の `E_STOP` 解除フレーム）だけ
- **押したままだと毎周期再ラッチされる。** まずスイッチが戻っているかを見る
- EDULITE が過電流等の fault を保持していると、解除しても指令が効かない。
  UI のヘルスが `FAULT` ならモータの電源を入れ直す

---

## 4. 試合直前の点検（指差喚呼と対で打つコマンド）

```bash
scripts/setup_can.sh --strict          # 指差喚呼 can_bus_strict
```

`4/4 バス起動 (未採取 0 / 欠け 0 / 失敗 0)` が出ること。
1 本でも欠けていたら §1 へ。

そのあと Monitor でヘルスが READY・無励磁 0 台であること（指差喚呼 `health_ready`）、
最後に**実際に非常停止を押して止まること**（指差喚呼 `estop_functional`）を確かめる。
物理停止は DC 基板経由なので、**配線 1 本で機能ごと失われても画面は平常のまま**である。
