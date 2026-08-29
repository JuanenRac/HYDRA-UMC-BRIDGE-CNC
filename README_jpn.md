<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - CNCセル連携ブリッジ
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

<p align="center">
  <img src="images/HYDRA_UMC_BANNER.svg" alt="HYDRA-UMC-BRIDGE-CNC バナー" width="100%">
</p>

# 🔧 HYDRA-UMC-BRIDGE-CNC

<p align="center"><a href="README.md">🇺🇸 English</a> | <a href="README_spa.md">🇪🇸 Español</a> | <a href="README_fra.md">🇫🇷 Français</a> | <a href="README_ita.md">🇮🇹 Italiano</a> | <a href="README_deu.md">🇩🇪 Deutsch</a> | <a href="README_zho.md">🇨🇳 简体中文</a> | 🇯🇵 <b>日本語</b></p>

### 🛑 CNCセル向けフェイルセーフ連携ブリッジ

<p align="left">
  <img src="https://img.shields.io/badge/ライセンス-GPL%203.0-blue.svg" alt="GPL 3.0">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Safety-Fails%20Closed-red.svg" alt="フェイルセーフ">
</p>

---

## 1. 🛠️ 技術概要

**HYDRA-UMC-BRIDGE-CNC** は、CNCセルとHYDRA-UMCロボット補助装置とを結ぶ高レベルブリッジであり、搬入・搬出・部品ハンドリング、監視下にある補助タスクを扱う。実時間軌道コントローラーになることは決してない —— CNCコントローラー(LinuxCNCなど)が常に軌道・主軸・機械限界の権限を保持する。

本リポジトリは **External Automation Bridges** ファミリーに属する。CNC・LASER・OPENPNP・PRINTER3D・ROS2という兄弟リポジトリ群が、すべて `HYDRA-UMC-SDK` の同じ安全契約を共有しており、いずれのブリッジも独自の「作業に安全」という定義を勝手に作ることはできない。

### 主な機能:
* ✅ **実在するフェイルセーフなセルスナップショット:** `cell.py` の `CncSnapshot.machine_state()` は、コントローラーが報告する内容を見る*前に*E-STOPとドア状態を確認する —— E-STOPが作動しているか、ドアが閉じていない場合、`controller_state` の内容に関わらず常に `SAFE_STOP` へ解決される。*(実装済み、`tests/test_cell.py` でテスト済み)*
* ✅ **実在する共有安全ゲート:** 観測されたすべてのジョブは `HYDRA-UMC-SDK` の `bridge_contract` にある `evaluate_job()` を通じて再評価される。これは他のすべての兄弟ブリッジとHYDRA-UMC-SERVERが使うのと同じゲートである。*(実装済み)*
* ✅ **保守的な状態マッピング:** `IDLE` のみがアイドルとして扱われる。`RUN`/`RUNNING`/`HOLD` は `RUNNING` に、`FAULT`/`ERROR`/`OFF` は `FAULT` にマッピングされ、認識されない値はすべて `OFFLINE` にフォールバックする —— 生産的な作業を許可してしまうような状態には決してフォールバックしない。*(実装済み)*
* ✅ **非破壊的なビルド/テスト:** `build-test.bat`/`.sh` はソースをコンパイルし、バージョンファイルやCHANGELOGに一切触れずに安全ゲートのテストスイートを実行する。*(実装済み、下記「ビルドと実行」を参照)*
* 🔜 **LinuxCNC HALアダプター** —— 保留中。実際のCNCコントローラー、HALピン、ロボットはまだ一度も駆動されていない。*(計画中)*

---

## 2. 🔄 セル連携フロー

```mermaid
flowchart LR
    CNC["CNCコントローラー / HAL<br/>(状態, E-STOP, ドア)"] --> BRIDGE["BRIDGE-CNC<br/>CncSnapshot.machine_state()"]
    BRIDGE -- "BridgeJob + 観測された MachineState" --> SDK["HYDRA-UMC-SDK<br/>evaluate_job()"]
    SDK -- GateDecision --> SERVER["HYDRA-UMC-SERVER"]
    SERVER -- "ジョブ / 中止" --> MCU["MCU + セル安全"]
```

---

## 3. 🧱 アーキテクチャと設計判断

* **なぜE-STOPとドア状態をコントローラー自身の報告状態より先に確認するのか。** `CncSnapshot.machine_state()` は、`estop` が真であるか、あるいはドアが閉じていない場合、真っ先に `SAFE_STOP` へ短絡する —— ドアが開いたままなのに `IDLE` と報告し続けるコントローラーを、額面通りに信用してはならない。
* **なぜコントローラー状態のマッピングを意図的に保守的にしているのか。** 文字列 `IDLE` のみが `MachineState.IDLE` にマッピングされる。認識されない値はすべて `OFFLINE` にフォールバックし、補助作業を許可してしまうような状態には決してならない —— 未知のコントローラー状態は「わからない」として扱われ、「おそらく大丈夫」とは扱われない。
* **なぜブリッジは新しい `BridgeJob` を組み立て、独自の受理/拒否ロジックを書く代わりに共有の `evaluate_job()` に委譲するのか。** 5つのExternal Automation Bridges(CNC、LASER、OPENPNP、PRINTER3D、ROS2)はすべて `HYDRA-UMC-SDK` の全く同じ `bridge_contract` を再利用しており、「何をもってジョブ開始が安全とみなすか」がそれぞれの間で静かに食い違うことがない。
* **なぜLinuxCNC HALアダプターはまだこのリポジトリに含まれていないのか。** 実際のHALピンを配線することは、実物のコントローラーに対して検証すべきハードウェア/ソフトウェア統合ステップである。その検証前にここで実装済みだと主張することは、このローカルコアが裏付けできない主張になってしまう。
* **エコシステムの他部分とどう関係するか。** BRIDGE-CNCはCNCコントローラーと `HYDRA-UMC-SDK` → `HYDRA-UMC-SERVER` → MCU/セル安全との間に位置する。CNCの周辺で補助ロボット作業を調整するものであり、コントローラー自身の安全権限を置き換えたり上書きしたりすることはない。

---

## 📂 ディレクトリ構成

```text
HYDRA-UMC-BRIDGE-CNC/
├── src/
│   └── hydra_umc_bridge_cnc/
│       ├── __init__.py
│       └── cell.py              # CncSnapshot + CncCellBridge 安全ゲート
├── tests/
│   └── test_cell.py             # 安全アイドル許可、ドア開放時の拒否、中止転送
├── tools/
│   ├── build_test.py            # 非破壊的なコンパイル+テストランナー (build-test.bat/.sh)
│   └── bump_version.py          # pyproject.toml、マニフェスト、CHANGELOG.md を同期
├── build-test.bat / build-test.sh  # 検証のみ、リポジトリを一切変更しない
├── build.bat / build.sh            # 検証後、成功時のみバージョン + CHANGELOG を更新
├── pyproject.toml               # パッケージメタデータ。HYDRA-UMC-SDK に依存 (git)
├── hydra-umc.project.json       # エコシステムマニフェスト(バージョン、成熟度、ファミリー)
├── CHANGELOG.md
├── CODE_OF_CONDUCT.md / CONTRIBUTING.md / SECURITY.md / SUPPORT.md
├── LICENSE / LICENSE.md
└── README.md / README_*.md      # 本ファイルおよびその6言語訳
```

---

## 4. ⚙️ ビルドと実行

Python 3.11以上が必要。`tools/build_test.py` は `HYDRA-UMC-SDK` が兄弟ディレクトリ(`../HYDRA-UMC-SDK`)としてチェックアウトされているか、環境変数 `HYDRA_UMC_SDK_ROOT` で指定されていることを期待する。

```bash
# Windows
build-test.bat      # 検証のみ —— バージョン/CHANGELOGの変更なし
build.bat            # 検証後、成功時にバージョン + CHANGELOG を更新

# Linux/macOS
bash build-test.sh
bash build.sh
```

`build-test` は `src/` 配下の各モジュールを `py_compile` でコンパイルし、`unittest` の全スイート(`tests/test_cell.py`)を実行して、安全アイドル許可、ドア開放時の拒否、中止転送を実証する —— リポジトリを一切変更しない。`build` はまず同じ検証を実行し、成功した場合のみ `tools/bump_version.py` を呼び出して `pyproject.toml`、`hydra-umc.project.json`、`CHANGELOG.md` の間でバージョンを同期する。実際のCNC向け `run` コマンドはまだ存在しない —— それには検証済みのコントローラー統合が必要である。

---

## ✅ 現状と次のステップ

**現時点で実在するもの:** バージョン `0.0.2`。ローカルでテスト済みのフェイルセーフなセル調整器(`CncSnapshot` + `CncCellBridge`)が `HYDRA-UMC-SDK` の共有ジョブゲートの上に構築されており、決定論的な `unittest` スイートと、SDKチェックアウトを伴いCIに組み込まれた非破壊的なbuild-testスクリプトを備える。

**統合境界:** CNCコントローラー(LinuxCNCなど)は常に軌道・主軸・機械限界の権限を保持する。このブリッジが調整するのはあくまで*補助的な*ロボット作業のみであり、コントローラー自身の動作には一切関与しない。

**今後の課題:** 実際のCNCコントローラー、LinuxCNC HALピン、ロボットはまだ一度も駆動されていない —— 具体的なHALアダプターの選定と検証は、実機とその文書化されたインターフェースが揃った後に行うハードウェア/ソフトウェア統合ステップである。

---

## 🔗 関連プロジェクト

本プロジェクトは、同じ著者(JuanenRac / Electro Hobby 3D)によるより大きなロボティクス・エコシステムの一部であり、ファームウェア、制御ソフトウェア、AIノード、フリート管理ツールにまたがる。リクエストが実際には本リポジトリではなくこれらのいずれかに関するものである可能性があるため、知っておく価値がある。

### 直接関連

- **[HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK)** —— このブリッジ(および他のすべてのブリッジ)がジョブを評価する共有の `bridge_contract` ジョブゲート。
- **[HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER)** —— このブリッジが報告する認証済みコーディネーター。
- **[HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE)** —— 実際のコントローラー統合のための将来のハードウェア・イン・ザ・ループ実証パス。

### エコシステムのその他

**HYDRA-UMCプラットフォーム** —— このブリッジが補助機能を調整するマルチロボット・マイクロファクトリー
- **[HYDRA-UMC](https://github.com/JuanenRac/HYDRA-UMC)** —— 最大8本のロボットアームを統括するCM5 + STM32H745マザーボード。
- **[HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER)** —— すべての制御クライアントとブリッジが通信するExpress/WebSocketバックエンド。
- **[HYDRA-UMC-STUDIO](https://github.com/JuanenRac/HYDRA-UMC-STUDIO)** —— Webベースの制御ダッシュボード、マルチロボット3D可視化。

**External Automation Bridges** —— 同じ `HYDRA-UMC-SDK` ジョブゲートを共有する兄弟リポジトリ群
- **[HYDRA-UMC-BRIDGE-LASER](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-LASER)** —— レーザーセル連携ブリッジ。
- **[HYDRA-UMC-BRIDGE-OPENPNP](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-OPENPNP)** —— OpenPnP向けボードフローブリッジ。
- **[HYDRA-UMC-BRIDGE-PRINTER3D](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-PRINTER3D)** —— オープンな3Dプリントソフトウェア向け連携ブリッジ。
- **[HYDRA-UMC-BRIDGE-ROS2](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-ROS2)** —— ROS 2との双方向連携境界。

**安全・統合の実証**
- **[HYDRA-UMC-SAFETY-ZONES](https://github.com/JuanenRac/HYDRA-UMC-SAFETY-ZONES)** —— ブリッジファミリー全体で使われるセルゾーンの安全実証。
- **[HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE)** —— ハードウェア・イン・ザ・ループのテスト実証。

## 👤 著者
**JuanenRac** (Electro Hobby 3D)
📧 electrohobby3d@gmail.com

## 📜 ライセンス
GPL-3.0 - 詳細はLICENSEを参照。

## 🛠️ ビルドと実行

リリースビルド前に、バージョンを変更しないビルドチェックを使用する:

| 操作 | Windows | Linux / macOS |
|---|---|---|
| ビルドチェック(バージョンやCHANGELOGの変更なし) | `build-test.bat` | `./build-test.sh` |
| 実行 / 開発(提供されている場合) | `run*.bat` または `dev*.bat` | `./run*.sh` または `./dev*.sh` |

`build-test.bat` と `build-test.sh` は、`hydra-umc.project.json` を更新せず `CHANGELOG.md` も変更せずに、プロジェクトのスタックをコンパイルまたは検証する。生成するのは通常のコンパイラ出力のみである。既存の `build*.bat`、`build*.sh`、`run*`、`dev*` スクリプトは、それぞれプロジェクト固有・バージョン管理・実行時の挙動を保持する。その挙動が必要な場合はそれらを使用すること。
