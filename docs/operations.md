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

**ビルド・書き込み・ツールチェーンの要件は [`../firmware/README.md`](../firmware/README.md) が
持つ。** 3 枚とも MCU もビルド系も違うので、そちらを見ること。

覚えておく点だけ: **native テスト（`pio test -e native`）は 2 プロジェクトが `test_dir` を
共有するのでどちらか一方で足りる**が、**実機ビルドは 3 env とも必要**（dc_motor / servo の
`nano` / servo の `uno_r4_minima`）。電磁弁だけ PlatformIO ではなく CMake で、
`arm-none-eabi-gcc` は 11 以降が要る（古い版は**コンパイルは通ってリンクだけが落ちる**）。

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

### 同じ CAN バスは 1 プロセスしか掴めない

`main.py` は CAN を開く前に、使うバスごとに `/run/lock/cbc-can-<インタフェース名>.lock` を
`flock` して持ち主を主張する（`lib/bus_lock.py`）。別のプロセスが掴んでいれば起動を拒否し、
相手の PID とコマンドラインを 1 行で出す。**ロボット単位ではなくバス単位**で競う ——
`--config config/sub_hand.yaml` と `--config` 省略（全機構成）はロボットの集合が違っても
`can_generic` を共有する。

```
CAN バス 'can_generic' は別のプロセスが掴んでいます (PID 55571: python -u main.py --dev-tools --port 8081)。…
```

- 拒否されたら文面の相手を止める（`kill <PID>` / `systemctl stop cbc-control`）。
- 相手が異常終了・電源断で消えていれば主張はカーネルが外している。**ロックファイルは消さない**
  （消す必要も無い。理由は [`invariants.md`](invariants.md) §5）。
- `--dry-run` は CAN を開かないので掴まない。机上ベンチ（`config/bench/**`）は実バスを開くので掴む。
- `scripts/tune_y_axis.py` など直接 `can.Bus` を開くスクリプトはこの主張を通らない。制御プログラムを
  止めてから使う。

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
