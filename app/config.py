import os
from pathlib import Path

APP_NAME = "BirdDesk"

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
EXIFTOOL = BASE_DIR / "vendor" / "exiftool" / "exiftool"

DATA_DIR = Path(
    os.environ.get(
        "BIRDDESK_DATA_DIR",
        str(Path.home() / "Library" / "Application Support" / APP_NAME),
    )
)
THUMB_DIR = DATA_DIR / "thumbs"
JOURNAL_DIR = DATA_DIR / "journals"
DB_PATH = DATA_DIR / "cache.sqlite3"
SETTINGS_PATH = DATA_DIR / "settings.json"

for _d in (DATA_DIR, THUMB_DIR, JOURNAL_DIR):
    _d.mkdir(parents=True, exist_ok=True)

RAW_EXTS = {
    ".arw", ".srf", ".sr2", ".cr2", ".cr3", ".crw", ".nef", ".nrw",
    ".raf", ".orf", ".rw2", ".dng", ".pef", ".srw", ".3fr", ".iiq",
}
JPG_EXTS = {".jpg", ".jpeg"}
RASTER_EXTS = {".heic", ".heif", ".hif", ".tif", ".tiff", ".png", ".webp"}
IMAGE_EXTS = RAW_EXTS | JPG_EXTS | RASTER_EXTS
SIDECAR_EXTS = {".xmp"}

UNDATED_FOLDER = "unknown-date"
BURST_PREFIX = "burst_"
REJECT_FOLDER = "_淘汰"
EXPORT_FOLDER = "_出片"      # 出片输出目录，扫描时要跳过，避免成品被当成新照片
VIDEO_FOLDER = "_视频"       # 连拍升格视频输出目录，同样要跳过扫描

DEFAULT_SETTINGS = {
    "gap": 0.5,            # 连拍判定：相邻两张最大间隔（秒）
    "min_burst": 2,        # 至少多少张才算一组连拍
    "split_by_camera": True,
    "recursive": True,
    "rename_conflicts": True,
    "cleanup_empty": True,
    "last_root": "",
    "strictness": "normal",     # 合焦严格度 loose / normal / strict
    "sort": "time",             # 排序：time / score / score_asc / rating（仅影响显示）
    "auto_analyze": True,       # 扫描后自动做合焦分析
    "reject_to_folder": False,  # 整理时把淘汰照片放进 _淘汰 子文件夹
    "auto_species": True,       # 分析时顺带识别鸟种（需 1.7GB 模型，约占 +90ms/张）
}
