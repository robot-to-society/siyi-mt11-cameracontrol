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
- ライブ映像表示（RTSP → WebRTC）と、映像クリックでの AI トラッキング開始・追跡枠の表示
- メインストリームのエンコード切替（H.264 / H.265、720p〜4K）

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

## ライブ映像とクリックでのAIトラッキング

```
MT11 --RTSP video1--> MediaMTX（ラズパイ、再エンコードなし）--WebRTC--> ブラウザ
ブラウザ --WHEP（/api/video/whep、本アプリが中継）--> MediaMTX 127.0.0.1:8889
```

ブラウザから届く必要があるのは、本アプリの 8000（HTTP）と、映像用の 8189（UDP、通らなければ TCP）です。

### MediaMTX の導入（ラズパイ）

1. [MediaMTX のリリース](https://github.com/bluenviron/mediamtx/releases)から `linux_arm64` 版を取得し、`/usr/local/bin/mediamtx` に置きます（32bit OS の場合は `linux_armv7`）。
2. 設定ファイルとサービスを配置して起動します。

```bash
sudo cp deploy/mediamtx.yml /usr/local/etc/mediamtx.yml
sudo cp deploy/mediamtx.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx
journalctl -u mediamtx -f
```

- 事前に `ss -lntup | grep -E ':8889|:8189'` で、ポートが使われていないことを確認してください。
- MediaMTX が設定項目名でエラーを出した場合は、そのバージョンの `mediamtx.yml` の書式に合わせてください。
- 映像用の 8189 は全インターフェースで待ち受けます。ラズパイがグローバル IP を持つ回線に直接つながる場合は、ファイアウォールで LAN と VPN からの接続だけを許可してください。
- SoftEther VPN 越しに映像がつながらない場合は、`webrtcAdditionalHosts` にラズパイの VPN 側の IP を追加して `sudo systemctl restart mediamtx` を実行します。

### 使い方

- **Camera タブの LIVE**：映像をクリックすると、その位置を中心に **Box（既定 150px、カメラ映像の画素基準）** の四角で AI トラッキングを開始します。
  - マウスを乗せると、指定される範囲が点線で表示されます。
  - 追跡中は、追跡枠が色付きで表示されます（緑：追跡中、黄：一時的に見失い、赤：見失い）。
- **Ctrl＋クリック**：カメラの AI が検出した人・車などを選んで追跡します（0x5F で検出枠を受け取り、0x56 の点指定で送信）。
  - Ctrl を押している間は検出枠が水色で表示され、マウスの下の枠が強調されます。
  - 映像の遅延を考えて直近 1 秒分の検出枠で当たり判定し、同じ物体の最新の位置を送ります。
  - 検出枠の外をクリックすると「その位置に検出枠がありません」と表示され、何も送りません。
  - カメラのファームウェアが 0x5F に対応していない場合（検出枠が届かない場合）は、水色の枠は出ず、クリック位置をそのまま点指定で送ります（カメラ側がその位置の検出物体を選ぶ想定）。
  - 検出枠やファームウェアの状態は `GET /api/debug/rx` で確認できます（`counts` に `0x5F` があるか、`firmware` のバージョン）。
- **Full**：映像パネルを全画面にします（Esc で戻る）。全画面でもクリックで追跡できます。
- **PiP**：Chrome では、映像と追跡枠を別の小窓に移して、他のアプリの上に浮かせられます（Document Picture-in-Picture）。小窓の中でもクリックで追跡できます。閉じると元の位置に戻ります。ほかのブラウザでは映像だけの PiP になります。
- LTE 越しでは映像が遅れるため、ドラッグではなくクリックで指定する方式にしています。右上に受信側の遅延の目安を表示します（カメラ側のエンコードや RTSP の遅延は含みません）。
- AI トラッキングを開始できるのは RGB モードのときだけです。
  - 従来の「Start Tracking」ボタンとジョイスティックの `ai_tracking_toggle` も同じ制限になりました（中央 200px で開始）。サーマル／2画面表示のときや、起動直後で解像度が未取得のときはエラーになります。
- エンコードを切り替えた直後は、新しい解像度を取得するまで（通常 1〜2 秒）クリックで追跡を開始できません。
- AI トラッキングを開始すると ROI は停止し、ROI を開始すると AI トラッキングは解除されます。
- **STREAM ENCODING**：メインストリームの符号化方式と解像度を切り替えます（録画ストリームは別設定）。
  - Chrome（M136 以降）は、PC の GPU が H.265 のハードウェア再生に対応していれば WebRTC で H.265 を再生できます。同じ画質なら帯域が少なく済むので、LTE 越しでは H.265 が有利です（MediaMTX は最新版を使ってください）。
  - Firefox や、GPU が H.265 に対応していない PC では映像が出ません。その場合は H.264 にしてください。
  - LTE で映像が止まりがちなら 720p にします（ビットレートは SDK で変更できないため、解像度で下げます）。

## Tests

```bash
pip install -r requirements-dev.txt
pytest
node --test tests/js/*.test.mjs
```
