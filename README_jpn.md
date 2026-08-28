<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - CNC セル協調ブリッジ
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

# HYDRA-UMC-BRIDGE-CNC

🇺🇸 [English](README.md) | 🇪🇸 [Español](README_spa.md) | 🇫🇷 [Français](README_fra.md) | 🇮🇹 [Italiano](README_ita.md) | 🇩🇪 [Deutsch](README_deu.md) | 🇨🇳 [简体中文](README_zho.md) | 🇯🇵 **日本語**

CNC セルと HYDRA-UMC ロボット補助機構のための上位ブリッジです。ローディング、
アンローディング、部品ハンドリング、監視付き補助タスクを扱います。リアルタイム
軌道コントローラーになることはありません。

## アーキテクチャ

```text
CNC コントローラー/HAL <-> BRIDGE-CNC <-> SDK <-> SERVER <-> MCU とセル安全
```

コアは、観測したコントローラー状態、E-STOP、ドア状態をフェイルセーフな
マシン状態へ変換します。ドアが開いている、または E-STOP が作動している場合、
生産的な補助作業は常にブロックされます。コントローラーは軌道、スピンドル、
機械限界に対する権限を保持します。

## ビルドとテスト

Windows では `build-test.bat`、Linux では `bash build-test.sh` を実行します。
このテストは変更を加えず、安全アイドルの許可、ドア開放時の拒否、中止の転送を
検証します。LinuxCNC HAL アダプターはハードウェア/ソフトウェア統合段階であり、
このローカルコアが実装済みであるという主張ではありません。

## 関連プロジェクト

| プロジェクト | 役割 |
| --- | --- |
| [HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK) | 共有ジョブゲート。 |
| [HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER) | 認証済みコーディネーター。 |
| [HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE) | 将来のコントローラー証跡。 |

## 状態

バージョン `0.0.1` はローカルテスト済みのフェイルセーフコーディネーターを
提供します。実際の CNC、HAL ピン、ロボットはまだ駆動していません。
