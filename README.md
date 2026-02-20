# MT11 Camera Control UI (Python)

UniPod MT11 向けのシンプルなダークUIです。  
以下を操作できます。

- 録画開始
- 録画停止
- 写真トリガー
- ズーム（±ボタンで1倍ずつ）
- 映像タイプ切替（RGB / サーマル / サイド・バイ・サイド）
- 現在の撮影状態表示
- カメラIP変更（デフォルト: `192.168.144.25`）

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
