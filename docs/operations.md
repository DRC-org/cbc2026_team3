# コマンドと運用

日常のコマンドと、systemd / CAN まわりの運用手順。**試合当日に手が止まったときは
[`venue_recovery.md`](venue_recovery.md)（会場カード）を先に見ること。** ここに書いた設計の
理由は [`invariants.md`](invariants.md) にある。

## Python バックエンド（uv 管理）

```bash
uv run python main.py             # サーバー起動（localhost:8080）
uv run python main.py --dry-run   # CAN バスなしで起動（virtual バス。配線確認に使える）
uv run python main.py --dev-tools # 開発用表示を解禁（CBC_DEV_TOOLS=1 でも可）
uv run python main.py --log-level debug # debug|info|warning|error（既定 info）

uv run pytest                     # 全テスト
uv run pytest tests/drivers/      # ドライバテストのみ
uv run pytest -x                  # 最初の失敗で停止
uv run pytest -k "m3508"          # 特定テストのみ
uv run ruff check .               # リント
uv run ruff format .              # フォーマット
```

構成の差し替えは `--system` / `--config`（机上ベンチ用の一式は
`config/bench/<対象>/`）。

## Web UI（`web/`）

```bash
cd web && pnpm install            # 依存インストール
cd web && pnpm dev                # 開発サーバー
cd web && pnpm build              # プロダクションビルド（出力は dist/）
cd web && pnpm test:run           # vitest を 1 回だけ実行
cd web && pnpm check              # lint + format + 型検査 + テスト + ビルド
```

dev サーバーは全インターフェースに bind し、Host ヘッダは `drc` と `*.ts.net` を許可する
（`vite.config.ts`）。Tailscale 経由なら `http://drc:5173`、制御プログラム直結なら
`http://drc:8080`。別名のホストを使うなら `VITE_ALLOWED_HOSTS` に足す。

WS 接続先はヘッダーの接続表示（と切断バナー）から変更でき、`?ws=drc:8080` でも一時上書き
できる。タブは URL パス（`/monitor` `/main-hand` `/sub-hand`）で、旧ハッシュ形式の
ブックマーク（`#main-hand` 等）は起動時にパスへ読み替える。

## ファームウェア

**ビルド・書き込み・ツールチェーンの要件は [`../firmware/README.md`](../firmware/README.md) が
持つ。** 3 枚とも MCU もビルド系も違うので、そちらを見ること。

覚えておく点だけ: **native テスト（`pio test -e native`）は 2 プロジェクトが `test_dir` を
共有するのでどちらか一方で足りる**が、**実機ビルドは 3 env とも必要**（dc_motor / servo の
`nano` / servo の `uno_r4_minima`）。**DC 基板だけはプロジェクトが 2 つあり**
（`dc_motor` = 内蔵 CAN / `dc_motor_slcan` = USB CDC + SLCAN。現物へ焼くのは後者で、前者は
切り戻し用）、どちらを焼くかと書き込みコマンドも `firmware/README.md` が持つ。電磁弁だけ
PlatformIO ではなく CMake で、
`arm-none-eabi-gcc` は 11 以降が要る（古い版は**コンパイルは通ってリンクだけが落ちる**）。

## CAN セットアップ

```bash
sudo scripts/install.sh           # udev ルール配置 + systemd 有効化（初回のみ）
scripts/setup_can.sh              # 手動 up。見つかったバスだけ立ち上げる（開発用）
scripts/setup_can.sh --strict     # 試合前点検。定義済みの全バス（現行 5 本）が揃わなければ異常終了
scripts/setup_can.sh --only can_dc # そのバスだけ扱う（cbc-slcand@ が内部で使う）
```

**`--strict` は試合直前の点検で人が打つ**（[`venue_recovery.md`](venue_recovery.md) §4）。
`cbc-can.service` は `--strict` を付けずに呼ぶので **CAN が 0 本でも success で終わり**、
`cbc-control.service` の `Wants=cbc-can.service` は揃っていることを保証しない。
**「揃っているか」に答えるのは人である。**

`--strict` を付けない理由は、片ハンドだけの練習・机上ベンチ・`--dry-run` がどれもバスの
揃っていない構成なので、付けると毎回この unit が `failed` で残り、`Requires=cbc-can.service` の
ままの `cbc-can-watchdog.service` が上がらなくなるため。`Requires=` ではなく `Wants=` なのは、
udev の CAN 再起動で制御プログラムを道連れにしないため。

vcan を使うテストは無い（`--dry-run` は python-can の `virtual` インタフェースで vcan ではない）。

### `can_dc` だけは `slcand` 経由（DC 基板は USB CDC 直結）

DC 基板は CAN トランシーバ故障のため USB CDC + SLCAN で PC へ繋ぐ。仕様は
[`motor_driver_can_protocol.md`](motor_driver_can_protocol.md) §1.1、固定の仕組みと
故障モードは [`invariants.md`](invariants.md) §1・§3。`setup_can.sh` はこのバスだけ扱いが違う。

- **`slcand` が要る** → `sudo apt install can-utils`。無ければ `cbc-slcand@can_dc` がその場で言う
- udev が固定するのは **tty**（`/dev/can_dc_tty`）で、netdev `can_dc` は `slcand` が作る
- **`slcand` を起こすのは `cbc-slcand@can_dc.service` だけ**（`scripts/slcand.sh`）。
  `setup_can.sh` は slcand を起動しない —— このバスについては「`slcand` が作った netdev を
  up する」ところまでしか持たない
- **プロセスが死んだら systemd が 10 秒後に起こし直す**（`Restart=always` / `RestartSec=10`）。
  起こし直した直後に `setup_can.sh --only can_dc` を自分で呼んで netdev を up まで戻すので、
  人の操作は要らない
- tty が無い間 `scripts/slcand.sh` は**黙って待つ**（基板を挿していない開発機で journal を
  埋めないため）。挿し込めばそのまま `slcand` が起動する。`StartLimitIntervalSec=0` なので
  何回落ちても諦めない（`failed` で止まると会場で人手の復旧が要るため）
- 取り残された `slcand` の掃除（tty が消えたのに netdev が残る形）も `scripts/slcand.sh` が
  起動前にやる

**netdev が戻っても `cbc-control` は繋ぎ直さない。** netdev が unregister されるとカーネルが
bind 済みの raw socket を切り離し、同名の netdev が再登録されても**再 bind されない**
（`ip link down/up` とは別物）。`slcand` が死んだあと通信を戻すには
**`sudo systemctl restart cbc-control`** が要る。これは gs_usb の 4 本を USB ごと抜いたときも
同じで、今回の自動復旧が面倒を見るのは netdev までである。

#### VID/PID とシリアル番号の採取

`config/can_buses.yaml` の `can_dc` は `TBD` のままなので、**実機から採取して埋めるまで
udev ルールが生成されず、`--strict` は「未採取」で落ちる**（`setup_can.sh` も採取方法を案内する）。
`cbc-slcand@can_dc` も **未採取の間は enable されない**（`install.sh` は「採取済みか」の判定を
`can_config.py` に任せている）ので、何も起きないし何も失敗しない。採取して埋めたら
`sudo scripts/install.sh` を打ち直すと enable される。

```bash
lsusb                              # 基板を抜き差しして増減する行を見る
ls -l /dev/serial/by-id/           # iSerial を出す個体ならここに実体が並ぶ
udevadm info -a -n /dev/ttyACM0 | grep -m3 -E 'ATTRS\{idVendor\}|ATTRS\{idProduct\}|ATTRS\{serial\}'
```

採った 3 つを `config/can_buses.yaml` の `can_dc` の `vendor_id` / `product_id` / `serial` へ
書き、`sudo scripts/install.sh` で udev ルールを配置し直してから挿し直す。

**落とし穴: UNO R4 Minima は iSerial を出さない個体がある。** その場合 `ATTRS{serial}` が
一致せず、**ルールは何も言わずに効かない**（症状は「`/dev/can_dc_tty` が生えない」だけで、
エラーはどこにも出ない）。`ls -l /dev/serial/by-id/` に出ない、`udevadm` に `ATTRS{serial}` が
無い、で先に切り分けること。

代替は物理ポート位置（`KERNELS==`）で縛る方法だが、**これは「個体固定」ではなく「挿し口固定」**
で、基板を別のポートへ挿した瞬間に効かなくなる —— [`invariants.md`](invariants.md) §1 の趣旨から
外れるので、外れることを承知のうえで選ぶこと。

### 同じ CAN バスは 1 プロセスしか掴めない

`main.py` は CAN を開く前に、使うバスごとに `/run/lock/cbc-can-<インタフェース名>.lock` を
`flock` して持ち主を主張する（`lib/bus_lock.py`。そこが開けなければ `/tmp` へ落とし、それも駄目なら
ERROR を 1 行出して主張なしで起動を続ける —— 主張できないことで機体が動かせなくならないように）。別のプロセスが掴んでいれば起動を拒否し、
相手の PID とコマンドラインを 1 行で出す。**ロボット単位ではなくバス単位**で競う ——
`--config config/sub_hand.yaml` と `--config` 省略（全機構成）はロボットの集合が違っても
`can_generic` を共有する。

```
CAN バス 'can_generic' は別のプロセスが掴んでいます (PID 55571: python -u main.py --dev-tools --port 8081)。…
```

- 拒否されたら文面の相手を止める（`kill <PID>` / `systemctl stop cbc-control`）。
- 相手が異常終了・電源断で消えていれば主張はカーネルが外している。**ロックファイルは消さない**
  （消す必要も無い。理由は [`invariants.md`](invariants.md) §5）。
- 起動ログに `持ち主を主張できません` / `で主張します` が出たら、別ユーザー（root の systemd 起動）が
  残したファイルが開けていない。検出はそのユーザー同士でしか効かないので、二重起動していないかは
  `ps aux | grep main.py` で自分で見る。
- `--dry-run` は CAN を開かないので掴まない。机上ベンチ（`config/bench/**`）は実バスを開くので掴む。
- `scripts/tune_y_axis.py` など直接 `can.Bus` を開くスクリプトはこの主張を通らない。制御プログラムを
  止めてから使う。

## サービス運用（systemd）

```bash
sudo scripts/install.sh           # 4 unit を配置（cbc-control だけ enable しない）
scripts/deploy.sh                 # git pull + 依存導入 + Web UI ビルド + 全サービス再起動
scripts/deploy.sh --no-pull       # pull だけ飛ばす
scripts/deploy.sh --no-install    # 会場用。pull と依存導入を飛ばしてビルドと再起動だけ
sudo systemctl start cbc-control  # 制御プログラム + Web UI 起動（8080）
journalctl -u cbc-control -f      # ログ追跡
journalctl -u cbc-can-watchdog -f # bus-off 復旧の記録
journalctl -u 'cbc-slcand@*' -f   # slcand の生き死に（can_dc）
```

**会場では `--no-install` を使う。** 素の `deploy.sh` は `git pull --ff-only` と `uv sync --frozen` と
`pnpm install --frozen-lockfile` を無条件に走らせ、ロックが満たされていなければ依存解決へ降りる
——「UI を 1 行直して反映」しようとした瞬間にネットワークで止まる。`--no-install` は pull も飛ばし、
ネットワークに一切触れない。**会場入りの前に一度
ネットワークのある場所で素の `deploy.sh` を回してキャッシュを温めておくこと。**

`deploy.sh` は `cbc-can` / `cbc-can-watchdog` / `cbc-control` と、採取済みの slcan バスの
`cbc-slcand@<バス名>` を reset-failed したうえで止まっていても起動し直す。**CAN は全バス down/up し、Web UI の接続は全部切れる**ので、操作中の人が
いないときに回す。本体チェックアウト（`cbc-control` の `WorkingDirectory`）以外から実行したとき、
`cbc-control` 以外のプロセス（手で `nohup` した `main.py` など）が 8080 を握っているときは
`exit 1` で拒否する（後者は PID を表示するだけで kill はしない）。

**`cbc-control` は `StartLimitBurst=3` / `RestartSec=2` なので、約 6 秒で `failed` に固定され、
以後 `systemctl start` すら通らなくなる。** CANable が 1 本欠けていると `main.py` は 1 秒未満で
落ちるため、抜けかけた USB を挿し直すより早く固定される。復帰には原因を直したうえで
**`sudo systemctl reset-failed cbc-control`** が要る。会場での切り分けは
[`venue_recovery.md`](venue_recovery.md) §1。

制御プログラムと Web Controller は同一プロセス（`lib/server.py` が `web/dist/` を SPA 配信する）。
**`cbc-control.service` は enable しない** — 電源投入だけで機体が通電・待機状態にならないよう、
起動タイミングは操縦者が握る（`deploy.sh` が起動するのは操縦者が明示的に回したときだけ）。
`cbc-can.service` / `cbc-can-watchdog.service` / `cbc-slcand@<バス名>` は enable する
（どれも機体を動かさない）。`cbc-slcand@` は**テンプレート unit**で、実体は採取済みの slcan
バスごとのインスタンス（現状 `cbc-slcand@can_dc.service` 1 本）。`Before=cbc-can.service` は
順序だけで、依存は張っていない —— この unit が落ちても `cbc-can` は上がる。

### スクリプトの土台

5 本のシェルスクリプトの土台は `scripts/_common.sh`（`SCRIPT_DIR` / `PYTHON` / `CAN_CONFIG` の
存在確認 / EUID による sudo の有無 / `log_*` / 引数解析の作法）。未知の引数は `exit 2` にする
——黙って無視すると `--uninstal` がフルインストールを走らせる。udev ルールのパスと service 名は
`can_config.py paths` が答える（`install.sh` と `setup_can.sh` でずれると、配置されているのに
「未配置」と警告し続ける）。

**ログの接頭辞だけは統一していない** — journal では `[ OK ]` / `[ WD ]` / `[slcan]` /
`[install]` / `[deploy]` で発生元の unit を見分けるので、各スクリプトが `LOG_PREFIX` を上書きする。
`_common.sh` はどこへもコピーされない（unit はリポジトリ内の `scripts/*.sh` をその場で実行する
ので、実行属性も要らない）。
