# Brain Flow Overlay for Windows

Muse S Athena EEG ヘッドバンドと Bluetooth (BLE) で接続し、リアルタイムで集中度・リラックス度をディスプレイの縁に HueShift カラーのグラデーションオーバーレイとして表示する Windows 常駐アプリです。

## 特徴

- リアルタイム EEG 解析: bleak + OpenMuse プロトコルで Muse S から脳波データを取得し、バンドパワー分析で集中度・リラックス度を算出
- ウルトラ省電力ストリーミング: Muse S Athena 専用プリセット `p50` (EEG4 のみ、光学/ACC/Gyro/Battery 無し) を既定使用し、デバイスの稼働時間を最大化。EEG-only 時は BLE 通知も EEG キャラクタリスティックのみに絞ります。`MUSE_PRESET` で `p1041` などに切り替え可能
- 自動再接続: 接続に失敗した場合や接続が切れた場合、指数バックオフ (1 / 2 / 4 / 8 / 16 / 30 秒上限) で永続的に再接続を試みる
- HueShift オーバーレイ: 3 アンカー補間。Focus 最大で赤、均衡で青、Relax 最大で緑にスムーズに変化
- クリックスルー: オーバーレイは常に最前面に表示されるが、下のウィンドウの操作を妨げない
- グラデーション: 画面中央側は完全透明、ディスプレイの上 / 左 / 右の縁に向かって徐々に不透明になる per-pixel alpha グラデーション (Win32 `UpdateLayeredWindow` 使用)
- 下辺は非表示: タスクバーを避けるため下辺にはオーバーレイを描画しない
- マルチディスプレイ対応: 複数ディスプレイ環境でも、指定した 1 つのディスプレイにのみ表示
- 輪郭太さ調整: システムトレイメニューから 10 px 〜 100 px の範囲で調整可能 (既定 20 px)
- システムトレイ常駐: バックグラウンドで動作し、タスクバーを占有しない

## 必要なもの

- Windows 10/11
- Python 3.10 (推奨。`mise.toml` で固定)
- Muse S (Gen 2) / Athena ヘッドバンド
- Bluetooth LE 対応 PC

## セットアップ

```powershell
# 1. リポジトリをクローン
git clone https://github.com/your-username/brain-flow-into-windows.git
cd brain-flow-into-windows

# 2. Python バージョンを固定 (mise 利用時)
mise use python@3.10

# 3. 仮想環境を作成
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 4. 依存パッケージをインストール
pip install -r requirements.txt

# 5. (任意) 環境変数を設定
#    .env.example を .env にコピー。bleak は名前で自動スキャンするため
#    シリアル番号の設定は通常不要です。
Copy-Item .env.example .env
```

## 省電力プリセット (試験的)

環境変数 `MUSE_PRESET` で BLE プリセットを切り替えられます。`.env` に書くか、起動前に PowerShell でセットします。

- 未設定 / 空文字: `p50`  - **EEG4 only (既定 / ウルトラ省電力)**。Optics / ACC / Gyro / Battery を送信せず、稼働時間が最も長い
- `p1041`         : EEG8 + Optics16 + ACCGYRO + Battery (フルセンサー)
- `p1034`         : EEG8 + Optics8 (フルセンサー、LED 明るめ)
- `p20`           : EEG4 + ACCGYRO (Optics 無し)
- `p21`           : EEG4 + PPG (BrainFlow ネイティブの既定)

```powershell
# 例: フルセンサーモードで起動
$env:MUSE_PRESET = "p1041"
python main.py
```

起動ログに `Scanning for Muse S (preset=p50 - EEG4 only ...)` のように出れば反映されています。

## 起動

```powershell
python main.py
```

起動すると:

1. システムトレイに HueShift 値（0-100）アイコンが表示されます
2. Muse S を BLE で自動スキャンし、ウルトラ省電力プリセット `p50` (EEG4 only) で接続を試みます
3. 接続失敗時は指数バックオフで再試行を続けます
4. 接続成功後、選択中のディスプレイの縁に HueShift (Focus と Relax の複合) で色が変わるグラデーションオーバーレイが表示されます

## 設定 (システムトレイメニュー)

システムトレイアイコンを右クリックすると、以下の操作が可能です。

- 接続状態: 現在の Muse S 接続状態 (Connected / Disconnected) を表示
- メトリクス: 現在の Focus / Relaxation の値を表示
- Border Width: 輪郭の太さを変更 (10 〜 100 px)
- Display: オーバーレイを表示するディスプレイを選択
- Show / Hide Overlay: オーバーレイの表示 / 非表示を切り替え
- Reconnect: Muse S への再接続を即時実行 (バックオフ待機をスキップ)
- Quit: アプリケーションを終了

## メトリクス計算

- Focus = beta / (alpha + theta)  (集中度, 0 〜 100 %)
- Relaxation = alpha / (beta + gamma)  (リラックス度, 0 〜 100 %)

## カラーマッピング (HueShift: 3 アンカー補間)

relax_weight = relaxation / (focus + relaxation) を 0–1 に正規化し、以下の 3 アンカーを 0–0.5 と 0.5–1.0 の 2 区間で、それぞれ短い弧上で補間します。

- relax_weight 0.0 (Focus 最大):  赤    (Hue 0 deg)
- relax_weight 0.5 (均衡):        青    (Hue 240 deg)
- relax_weight 1.0 (Relax 最大):  緑    (Hue 120 deg)
- 未接続:                         グレー

弧の遷移: 赤 → マゼンタ/紫 → 青 → シアン → 緑 (全区間で短い弧を選択)

## プロジェクト構成

```
brain-flow-into-windows/
  main.py              # エントリポイント (Tk + データスレッド管理 + 自動再接続)
  config.py            # 設定定数
  muse_athena.py       # Muse S Athena BLE アダプタ (preset p50 default)
  muse_connector.py    # MuseAthenaBoard ラッパー (lifecycle)
  brain_metrics.py     # EEG メトリクス計算
  display_manager.py   # マルチディスプレイ管理
  overlay_window.py    # per-pixel alpha グラデーションオーバーレイ
  tray_app.py          # システムトレイ UI
  openmuse/            # OpenMuse BLE プロトコル (backends / decode / muse)
  requirements.txt     # Python 依存パッケージ
  .env                 # 環境変数 (任意)
  .env.example         # .env のテンプレート
```

## ライセンス

MIT
