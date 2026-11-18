#!/usr/bin/env bash
# BirdDesk 依赖与模型准备脚本。
#
#   ./setup.sh              # 建 venv、装依赖、下载模型（已存在则跳过）
#   ./setup.sh --code-only  # 只建 venv 和依赖，不下载 1.8GB 模型
#
# 模型来源与许可见 README「模型与许可」一节；脚本只做下载，不做任何照片操作。
set -euo pipefail
cd "$(dirname "$0")"

CODE_ONLY=0
[ "${1:-}" = "--code-only" ] && CODE_ONLY=1
[ "${BIRDDESK_SKIP_MODELS:-0}" = "1" ] && CODE_ONLY=1

say() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn(){ printf '  \033[33m!\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- 1. Python
say "[1/3] Python 环境"
PY=${PYTHON:-python3}
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 python3，请先安装 Python 3.11+（brew install python@3.12）" >&2
  exit 1
fi
PYVER=$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')
ok "python3 = $PYVER"

if [ ! -d .venv ]; then
  say "  创建 .venv"
  "$PY" -m venv .venv
fi
ok ".venv 就绪"

say "[2/3] 安装依赖（requirements.txt）"
./.venv/bin/python -m pip install -q --upgrade pip
./.venv/bin/python -m pip install -q -r requirements.txt
ok "依赖安装完成"

# ---------------------------------------------------------------- 3. 模型
say "[3/3] 模型权重"
mkdir -p models/bioclip2 models/nafnet

fetch() {  # fetch <url> <dest> [期望字节数]
  local url=$1 dest=$2 want=${3:-0}
  if [ -s "$dest" ]; then
    local have; have=$(wc -c < "$dest" | tr -d ' ')
    if [ "$want" != "0" ] && [ "$have" != "$want" ]; then
      warn "$(basename "$dest") 大小异常（$have ≠ $want），重新下载"
    else
      ok "$(basename "$dest") 已存在，跳过"; return
    fi
  fi
  echo "  ↓ $(basename "$dest")"
  curl -fL --retry 3 --retry-delay 2 -# -o "$dest.part" "$url"
  mv "$dest.part" "$dest"
  ok "$(basename "$dest") 下载完成"
}

if [ "$CODE_ONLY" = "1" ]; then
  warn "已跳过模型下载（--code-only）；没有模型时：鸟体检测/鸟眼/鸟种/降噪 都不可用"
else
  BIO_REV="2957b322090f"
  BIO_BASE="https://huggingface.co/imageomics/bioclip-2/resolve/${BIO_REV}"
  fetch "${BIO_BASE}/open_clip_pytorch_model.bin" models/bioclip2/open_clip_pytorch_model.bin
  fetch "${BIO_BASE}/open_clip_config.json"       models/bioclip2/open_clip_config.json
  fetch "${BIO_BASE}/tokenizer.json"              models/bioclip2/tokenizer.json
  fetch "${BIO_BASE}/vocab.json"                  models/bioclip2/vocab.json
  fetch "${BIO_BASE}/merges.txt"                  models/bioclip2/merges.txt

  fetch "https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11m-seg.pt" \
        models/yolo11m-seg.pt 45400152

  EYE_REV="6e7bd361ba84b17d81f0fb4315a44a868df5b692"
  fetch "https://huggingface.co/synthet/eye-pose-v0/resolve/${EYE_REV}/eye_pose_v0.pt" \
        models/eye_pose_v0.pt
  if command -v shasum >/dev/null 2>&1 && [ -s models/eye_pose_v0.pt ]; then
    GOT=$(shasum -a 256 models/eye_pose_v0.pt | awk '{print $1}')
    WANT="ec9e4b80a93eb6e2b21633e2ff2df30c49faa1eb7919dd24345c7dc295b2d1be"
    [ "$GOT" = "$WANT" ] && ok "eye_pose_v0.pt 校验通过" || warn "eye_pose_v0.pt 校验不符，鸟眼功能会拒绝加载"
  fi

  fetch "https://huggingface.co/nyanko7/nafnet-models/resolve/main/NAFNet-SIDD-width32.pth" \
        models/nafnet/nafnet_sidd_width32.pth
fi

# ---------------------------------------------------------------- 收尾
say "环境自检"
./.venv/bin/python - <<'PY'
from pathlib import Path
base = Path("models")
checks = [
    ("鸟体分割  yolo11m-seg", base / "yolo11m-seg.pt"),
    ("鸟眼关键点 eye_pose_v0", base / "eye_pose_v0.pt"),
    ("鸟种识别  bioclip2", base / "bioclip2" / "open_clip_pytorch_model.bin"),
    ("AI 降噪   NAFNet", base / "nafnet" / "nafnet_sidd_width32.pth"),
]
for name, p in checks:
    print(f"  {'✓' if p.is_file() else '·'} {name:24} {'已就绪' if p.is_file() else '缺失'}")
print("  ✓ ExifTool（随仓库分发）" if Path("vendor/exiftool/exiftool").exists() else "  ! ExifTool 缺失")
PY

cat <<'EOF'

下一步：
  ./make_app.sh          # 打包成 BirdDesk.app（双击运行）
  ./.venv/bin/python -m app.main          # 或者直接起服务并打开窗口
  ./.venv/bin/python -m app.main --no-open --port 8765   # 仅起服务（调试）

测试（需要一个示例照片目录，见 README）：
  BIRDDESK_SAMPLE_DIR=/path/to/samples ./.venv/bin/python tests/test_develop.py
EOF
