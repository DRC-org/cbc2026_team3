# 実装計画 — 移転済み

この文書は「実装計画・設計判断・作業日誌」を 1 本に積み上げたもので、6,973 行のうち
規範的な記述と経緯が混ざり、章によって古い記述が残っていた。役割ごとに分けて移した。

**ここには何も書かない。** 下の行き先へ書くこと。

| 元の章 | 行き先 |
|---|---|
| 概要 / ハードウェア構成 / アーキテクチャ / ディレクトリ構成 | [`architecture.md`](architecture.md) |
| WebSocket プロトコル / 試合運用フロー / Web UI ページ構成 | [`architecture.md`](architecture.md) |
| 設定ファイルの構成 / 機構位置定数 / メインハンド実機構成 | [`architecture.md`](architecture.md) |
| サービス化（systemd）/ テスト戦略 / 未解決の課題 | [`architecture.md`](architecture.md) |
| 各章に埋まっていた「なぜそうしたか」「崩すと何が起きるか」 | [`invariants.md`](invariants.md) |
| 自作モータドライバ用 CAN プロトコル | [`motor_driver_can_protocol.md`](motor_driver_can_protocol.md) |
| 日付付きの節（実機で観測された事象）| [`history/incidents.md`](history/incidents.md) |
| Phase 1〜12 の作業分解表 / 全域リファクタリング / 安全機構の穴 | [`history/decisions.md`](history/decisions.md) |
| 机上ベンチ・実機の PID 実測ログ | [`history/incidents.md`](history/incidents.md) |
| パラメータ変更（`set_param`）/ Phase 12 の PID 調整 UI | 機能ごと削除済みのため引き継がない |

分割前の全文は git 履歴にある。

```bash
git log --follow -- docs/impl_plan.md
git show <このコミットの親>:docs/impl_plan.md
```

文書の地図は [`../README.md`](../README.md) にある。
