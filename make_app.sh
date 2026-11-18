#!/bin/zsh
# 生成 BirdDesk.app（双击即可启动的桌面应用），放在本目录下。
# 用法：./make_app.sh            （首次会自动创建 .venv 并安装依赖）
set -e
HERE="${0:A:h}"
cd "$HERE"

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

APP="$HERE/BirdDesk.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>BirdDesk</string>
  <key>CFBundleDisplayName</key><string>BirdDesk 鸟片整理</string>
  <key>CFBundleIdentifier</key><string>local.birddesk.app</string>
  <key>CFBundleVersion</key><string>0.9.1</string>
  <key>CFBundleShortVersionString</key><string>0.9.1</string>
  <key>CFBundleExecutable</key><string>BirdDesk</string>
  <key>CFBundleIconFile</key><string>BirdDesk</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/BirdDesk" <<LAUNCH
#!/bin/zsh
cd "$HERE"
LOG="\$HOME/Library/Application Support/BirdDesk/birddesk.log"
mkdir -p "\${LOG:h}"
exec "$HERE/.venv/bin/python" -m app.main >> "\$LOG" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/BirdDesk"

# 图标：用 Python 画一个，再转成 icns
.venv/bin/python - "$APP/Contents/Resources" <<'PY'
import sys, subprocess, tempfile
from pathlib import Path
from PIL import Image, ImageDraw
out = Path(sys.argv[1])
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([64, 64, S - 64, S - 64], radius=200, fill=(238, 244, 236, 255))
k = S / 32
pts = [(4, 19), (9, 18), (12, 15), (14, 12), (16, 8), (19, 6), (23, 6), (26, 7), (28, 9), (25, 10), (24, 16), (20, 21), (13, 23), (10, 27), (9, 23), (6, 22)]
pts = [(x * k * 0.8 + S * 0.1, y * k * 0.8 + S * 0.1) for x, y in pts]
d.polygon(pts, fill=(47, 125, 91, 255))
cx, cy = 23 * k * 0.8 + S * 0.1, 9 * k * 0.8 + S * 0.1
d.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], fill=(255, 255, 255, 255))
iconset = Path(tempfile.mkdtemp()) / "BirdDesk.iconset"
iconset.mkdir()
for sz in (16, 32, 128, 256, 512):
    img.resize((sz, sz), Image.LANCZOS).save(iconset / f"icon_{sz}x{sz}.png")
    img.resize((sz * 2, sz * 2), Image.LANCZOS).save(iconset / f"icon_{sz}x{sz}@2x.png")
subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "BirdDesk.icns")], check=False)
PY

echo "已生成：$APP"
echo "双击打开，或拖到「应用程序」/ 程序坞。注意：它依赖本目录的代码和 .venv，请不要移动 birddesk 文件夹。"
