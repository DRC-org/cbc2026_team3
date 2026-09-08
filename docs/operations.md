# コマンドと運用

日常のコマンドと、systemd / CAN まわりの運用手順。**試合当日に手が止まったときは
[`venue_recovery.md`](venue_recovery.md)（会場カード）を先に見ること。** ここに書いた設計の
理由は [`invariants.md`](invariants.md) にある。

## Python バックエンド（uv 管理）

```bash
uv run python main.py             # サーバー起動（localhost:8080）
uv run python main.py --dry-run   # CAN バスなしで起動（virtual バス。配線確認に使える）
uv run python main.py --dev-tools # 開発用コマンドを解禁（CBC_DEV_TOOLS=1 でも可）
uv run python main.py --log-level debug # debug|info|warning|error（既定 info）

uv run pytest                     # 全テスト
uv run pytest tests/drivers/      # ドライバテストのみ
uv run pytest -x                  # 最初の失敗で停止
uv run pytest -k "m3508"          # 特定テストのみ
uv run ruff check .               # リント
uv run ruff format .              # フォーマット
```

構成の差し替えは `--system` / `--config` / `--checklist`（机上ベンチ用の一式は
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

### DC / サーボ（PlatformIO）

`-d` にプロジェクトディレクトリを渡せばリポジトリ直下から実行できる。

```bash
pio test -e native -d firmware/dc_motor   # 実機不要。firmware/test/ の全ケース
pio test -e native -d firmware/servo      # 上とまったく同じ全ケース

pio run -e uno_r4_minima -d firmware/dc_motor
pio run -e nano -d firmware/servo -t upload          # サーボ基板 #0 / #1（Nano）
pio run -e uno_r4_minima -d firmware/servo -t upload # サーボ基板 #2（UNO R4）
```

テストは `firmware/test/` にあり両プロジェクトが `test_dir` で共有するので、**native テストは
どちらか一方で足りる**（電磁弁基板のロジック層 `test_solenoid` もここに含まれる。実機ビルドは
CMake だが `MotorCan` を共有しているため）。

一方**実機ビルド（`pio run`）は 3 つとも必要**（dc_motor / servo の `nano` / servo の
`uno_r4_minima`）。共有しているのは `firmware/lib/MotorCan/` までで `main.cpp` と `config.h` は
別物のため。**サーボの 2 env は `main.cpp` と `config.h` を共有するが、ピンの `static_assert` も
CAN バックエンドも env ごとに別物なので、片方だけ通しても壊れているのは常にもう片方である。**

### 電磁弁（CMake）

**電磁弁基板だけビルド系が違う**（STM32F303K8 / CubeMX 生成の HAL）。`Drivers/` だけ
`.gitignore` してあるので、clone 直後に 1 回取得すればビルドできる。

```bash
firmware/solenoid/scripts/fetch_hal.sh          # 初回のみ。CubeMX は要らない
cmake --preset Debug -S firmware/solenoid
cmake --build firmware/solenoid/build/Debug
```

**`arm-none-eabi-gcc` は 11 以降が要る。** CubeMX のリンカスクリプトが使う `READONLY`
キーワードが GCC11 以降にしか無く、古い版では**コンパイルは全部通ってリンクだけが落ちる**。
Ubuntu 24.04 以降なら `sudo apt install gcc-arm-none-eabi binutils-arm-none-eabi
libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib`（**libstdc++ を省くとリンクだけが
落ちる**）。詳細と xPack 版の手順は [`../firmware/README.md`](../firmware/README.md)。

`Core/Src/main.c` の USER CODE 領域には `setup()` / `loop()` の呼び出ししか置かないこと
（それ以外は再生成で消える）。ロジックは `src/app.cpp`、ピン割当と CAN のビットタイミングは
`solenoid.ioc` が持つ。**CubeMX が要るのは `.ioc` を変えたときだけ。**

## CAN セットアップ

```bash
sudo scripts/install.sh           # udev ルール配置 + systemd 有効化（初回のみ）
scripts/setup_can.sh              # 手動 up。見つかったバスだけ立ち上げる（開発用）
scripts/setup_can.sh --strict     # 試合前点検。定義済みの全バス（現行 4 本）が揃わなければ異常終了
```

**`--strict` を打つ導線は指差喚呼**（`config/checklist.yaml` の `can_bus_strict`）。
`cbc-can.service` は `--strict` を付けずに呼ぶので **CAN が 0 本でも success で終わり**、
`cbc-control.service` の `Wants=cbc-can.service` は揃っていることを保証しない。
**「揃っているか」に答えるのは人である。**

`--strict` を付けない理由は、片ハンドだけの練習・机上ベンチ・`--dry-run` がどれもバスの
揃っていない構成なので、付けると毎回この unit が `failed` で残り、`Requires=cbc-can.service` の
ままの `cbc-can-watchdog.service` が上がらなくなるため。`Requires=` ではなく `Wants=` なのは、
udev の CAN 再起動で制御プログラムを道連れにしないため。

vcan を使うテストは無い（`--dry-run` は python-can の `virtual` インタフェースで vcan ではない）。

## サービス運用（systemd）

```bash
sudo scripts/install.sh           # 3 unit を配置（cbc-control だけ enable しない）
scripts/deploy.sh                 # 依存導入 + Web UI ビルド + サービス再起動
scripts/deploy.sh --no-install    # 会場用。依存導入を飛ばしてビルドと再起動だけ
sudo systemctl start cbc-control  # 制御プログラム + Web UI 起動（8080）
journalctl -u cbc-control -f      # ログ追跡
journalctl -u cbc-can-watchdog -f # bus-off 復旧の記録
```

**会場では `--no-install` を使う。** 素の `deploy.sh` は `uv sync --frozen` と
`pnpm install --frozen-lockfile` を無条件に走らせ、ロックが満たされていなければ依存解決へ降りる
——「UI を 1 行直して反映」しようとした瞬間にネットワークで止まる。**会場入りの前に一度
ネットワークのある場所で素の `deploy.sh` を回してキャッシュを温めておくこと。**

**`cbc-control` は `StartLimitBurst=3` / `RestartSec=2` なので、約 6 秒で `failed` に固定され、
以後 `systemctl start` すら通らなくなる。** CANable が 1 本欠けていると `main.py` は 1 秒未満で
落ちるため、抜けかけた USB を挿し直すより早く固定される。復帰には原因を直したうえで
**`sudo systemctl reset-failed cbc-control`** が要る。会場での切り分けは
[`venue_recovery.md`](venue_recovery.md) §1。

制御プログラムと Web Controller は同一プロセス（`lib/server.py` が `web/dist/` を SPA 配信する）。
**`cbc-control.service` は enable しない** — 電源投入だけで機体が通電・待機状態にならないよう、
起動タイミングは操縦者が握る。`cbc-can.service` と `cbc-can-watchdog.service` は enable する
（どちらも機体を動かさない）。

### スクリプトの土台

4 本のシェルスクリプトの土台は `scripts/_common.sh`（`SCRIPT_DIR` / `PYTHON` / `CAN_CONFIG` の
存在確認 / EUID による sudo の有無 / `log_*` / 引数解析の作法）。未知の引数は `exit 2` にする
——黙って無視すると `--uninstal` がフルインストールを走らせる。udev ルールのパスと service 名は
`can_config.py paths` が答える（`install.sh` と `setup_can.sh` でずれると、配置されているのに
「未配置」と警告し続ける）。

**ログの接頭辞だけは統一していない** — journal では `[ OK ]` / `[ WD ]` / `[install]` /
`[deploy]` で発生元の unit を見分けるので、各スクリプトが `LOG_PREFIX` を上書きする。
`_common.sh` はどこへもコピーされない（unit はリポジトリ内の `scripts/*.sh` をその場で実行する
ので、実行属性も要らない）。
