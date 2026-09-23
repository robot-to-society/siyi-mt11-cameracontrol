# MT11 Camera Control UI (Python)

UniPod MT11 向けのシンプルなダークUIです。  
以下を操作できます。

- 録画開始
- 録画停止
- 写真トリガー
- ズーム（±ボタンで1倍ずつ）
- 最大ズームは `165x` 固定（バークリックで任意倍率へ設定）
- 映像タイプ切替（RGB / サーマル / サイド・バイ・サイド）
- 現在の撮影状態表示
- カメラIP変更（デフォルト: `192.168.144.25`）
- GPS ROI：登録した座標へカメラを向け続ける（ジョイスティックのボタンに割当可）

## Setup

```bash
cd /home/pi/github/siyi-mt11-cameracontrol
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
. .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

ブラウザで `http://<raspberrypi-ip>:8000` を開いてください。

## systemd service

サービスファイル: `deploy/mt11-camera-ui.service`
（`/home/pi/github/siyi-mt11-cameracontrol` 配置前提）

```bash
sudo cp deploy/mt11-camera-ui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mt11-camera-ui
sudo systemctl status mt11-camera-ui
```

## Notes

- SDK通信は TCP `37260` を使用します。
- 録画制御コマンドはトグル挙動のため、バックエンド側で状態を確認してから start/stop を実行します。
- 1秒ごとに状態をポーリングし、UIに反映します。

## GPS ROI（登録座標への自動指向）

MT11 SDK には ROI 専用コマンドがないため、本アプリが FC の位置・機首方位と目標座標から
方位角/仰角を計算し、`0x0E`（ジンバル角度指令）を 10Hz で送り続けます（開始時に `0x0C` で Follow モードへ）。

```
FC --serial--> Raspberry Pi [Rpanion-server] --UDP 127.0.0.1:15555--> 本アプリ --> MT11 (0x0E)
```

### Rpanion-server 設定

Rpanion の **Flight Controller → Telemetry Destinations** に `127.0.0.1:15555` を追加します（設定済み）。
本アプリはこのポートで待ち受けます（`udpin:127.0.0.1:15555`、ROI タブの MAVLink URL で変更可）。

本アプリは FC の HEARTBEAT を受信すると `MAV_CMD_SET_MESSAGE_INTERVAL` で `GLOBAL_POSITION_INT` を 10Hz 要求します。

### 使い方

1. UI の **ROI** タブで `roi_1`〜`roi_4` の緯度・経度・**海抜高度 (MSL, m)** を入力して保存（`app/roi_config.json`）
2. **Joystick** タブのボタン割当で `roi_1`〜`roi_4`（押すたびに開始/停止）や `roi_stop` を設定
3. パン/チルト操作やセンター操作をすると ROI は自動で解除されます

### 事前確認（実機）

`0x0E` の角度基準は SDK に明記されていないため、初回は以下で確認してください（UI サービスは停止しておく）。

```bash
python -m scripts.probe_0x0e --yaw 90 --pitch -30
```

- 機体を手で回したとき、カメラが機首に対して同じ角度を保つ → 想定どおり（yaw は機体基準）
- 機体を傾けたとき、カメラが水平線を向き続ける → 想定どおり（pitch は水平基準）
- yaw=0 が機首とずれる場合は ROI タブの **Yaw Offset** で補正

実機確認結果（2026-09-23, MT11）：yaw は台座基準（+ = 左）、pitch は水平基準（+ = 上、機体を傾けても保持）。
本アプリの計算はこの前提どおりで、機体姿勢による補正は不要。

### 机上テスト（疑似FC）

実FCの位置が混ざらないよう、ROI タブの MAVLink URL を一時的に `udpin:127.0.0.1:15556` にして、疑似FCを起動します。

```bash
python -m scripts.fake_vehicle --lat 35.xxxxxxx --lon 139.xxxxxxx --alt 40 --heading 120
```

`+30` / `-30` / `h 90` で機首方位、`p LAT LON` で位置、`a ALT` で高度を変更できます。終わったら MAVLink URL を `udpin:127.0.0.1:15555` に戻してください。

### 制約

- 精度は FC の機首方位（コンパス）と GPS 精度に依存します。数度の方位誤差があると、高倍率ズームでは画角から外れることがあります。高倍率で使う場合は、ROI で向けたあと AI トラッキングに切り替える運用を推奨します。
- 機体のロール/ピッチによる yaw 誤差は補正していません。
- 目標から 3m 以内、位置情報が 2 秒以上更新されない、機首方位が不明（hdg=65535）の場合は指令を送りません（ROI タブにエラー表示）。
- 上記で指令が止まっている間、ジンバルは最後の角度（機体基準）を保持します。ROI は自動解除しないので、ROI タブのエラー表示を確認してください。
- ROI 中はジョイスティックの小さな入力（速度 5 未満）をスティックのノイズとみなして無視します。それ以上のパン/チルト入力、または gimbal_stop で ROI を解除します。
- Follow モード（0x0C）は開始時と、ROI 中は 5 秒ごとに再送します（カメラ再起動や他クライアントによるモード変更への対策）。
- 目標が真後ろ付近（±180°）の場合、機首方位のブレで一周しないよう ±190° までは同じ側を維持します。
- `mavlink_url` に指定できるのは `udpin|udpout|udp|tcp:<host>:<port>` か `/dev/tty*[,baud]` だけです（`tcpin` は不可）。

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
