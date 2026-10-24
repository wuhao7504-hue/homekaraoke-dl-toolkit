#!/bin/bash
# 打包 HomeKaraoke 下载器（本地 Web 界面 + 自绘图标）
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/HomeKaraoke 下载器.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>HomeKaraoke 下载器</string>
  <key>CFBundleDisplayName</key><string>HomeKaraoke 下载器</string>
  <key>CFBundleIdentifier</key><string>app.homekaraoke.downloader</string>
  <key>CFBundleVersion</key><string>2.0.0</string>
  <key>CFBundleShortVersionString</key><string>2.0.0</string>
  <key>CFBundleExecutable</key><string>HomeKaraokeDownloader</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSRequiresAquaSystemAppearance</key><false/>
  <key>LSBackgroundOnly</key><false/>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/HomeKaraokeDownloader" <<'SH'
#!/bin/bash
# 1) 拉起本地服务（已运行则复用）  2) 用 Chrome 应用窗口显示界面
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/../Resources"
PORT="${HKDL_PORT:-18765}"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
LOG="$HOME/Library/Logs/hkdl.log"
mkdir -p "$HOME/Library/Logs"

alive() { curl -s -m 1 "http://127.0.0.1:$PORT/api/ping" >/dev/null 2>&1; }

if ! alive; then
  nohup /usr/bin/python3 "$RES/hkapp.py" >>"$LOG" 2>&1 &
  for _ in $(seq 1 80); do
    alive && break
    sleep 0.25
  done
fi

PROFILE="$HOME/Library/Application Support/homekaoke-downloader/chrome-profile"
URL="http://127.0.0.1:$PORT/"
if [ -d "/Applications/Google Chrome.app" ]; then
  open -na "Google Chrome" --args \
    --app="$URL" \
    --user-data-dir="$PROFILE" \
    --window-size=1200,880 \
    --no-first-run --no-default-browser-check \
    --disable-features=Translate \
    --disable-background-timer-throttling \
    --disable-backgrounding-occluded-windows \
    --disable-renderer-backgrounding
else
  open "$URL"
fi
SH
chmod +x "$APP/Contents/MacOS/HomeKaraokeDownloader"

# 图标：优先用 assets/icon.icns，没有就用 assets/icon.png 现场生成
if [ -f "$SRC_DIR/assets/icon.icns" ]; then
  elif [ -f "$SRC_DIR/assets/icon.png" ]; then
  TMP=$(mktemp -d)/icon.iconset
  mkdir -p "$TMP"
  for s in 16 32 64 128 256 512 1024; do
    sips -z $s $s "$SRC_DIR/assets/icon.png" --out "$TMP/icon_${s}x${s}.png" >/dev/null
  done
  cp "$TMP/icon_32x32.png" "$TMP/icon_16x16@2x.png"
  cp "$TMP/icon_64x64.png" "$TMP/icon_32x32@2x.png"
  cp "$TMP/icon_256x256.png" "$TMP/icon_128x128@2x.png"
  cp "$TMP/icon_512x512.png" "$TMP/icon_256x256@2x.png"
  cp "$TMP/icon_1024x1024.png" "$TMP/icon_512x512@2x.png"
  iconutil -c icns "$TMP" -o "$APP/Contents/Resources/icon.icns"
fi

cp "$SRC_DIR/hkdl.py" "$APP/Contents/Resources/hkdl.py"
cp "$SRC_DIR/hkapp.py" "$APP/Contents/Resources/hkapp.py"

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" >/dev/null 2>&1 || true

echo "已安装：$APP"
