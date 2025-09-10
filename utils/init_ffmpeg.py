import os
import urllib.request
import zipfile
import shutil
import time
from utils.utils import log

FFMPEG_DIR = os.path.join(os.getcwd(), "ffmpeg")
FFMPEG_EXE = os.path.join(FFMPEG_DIR, "bin", "ffmpeg.exe")
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"  # Windows release 包

_last_time = 0
_last_bytes = 0

def _progress_hook(count, block_size, total_size):
    """下载进度回调"""
    global _last_time, _last_bytes
    downloaded = count * block_size
    if total_size > 0:
        percent = downloaded / total_size * 100
    else:
        percent = 0

    now = time.time()
    elapsed = now - _last_time if _last_time else 0
    speed = 0
    if elapsed > 0:
        speed = (downloaded - _last_bytes) / elapsed / 1024 / 1024  # MB/s

    _last_time = now
    _last_bytes = downloaded

    bar_len = 40
    filled_len = int(bar_len * percent / 100)
    bar = '=' * filled_len + '-' * (bar_len - filled_len)

    print(f"\rDOWNLOAD |{bar}| {percent:6.2f}%  {speed:5.2f} MB/s", end='', flush=True)

def init_ffmpeg():
    """初始化 ffmpeg，如果没有就自动下载并解压"""
    if os.path.exists(FFMPEG_EXE):
        log("[INFO]", "ffmpeg 已存在:", FFMPEG_EXE)
        return FFMPEG_EXE

    log("INFO", "未找到 ffmpeg，开始下载...")
    zip_path = "ffmpeg.zip"

    # 下载 zip 带进度 + 速度
    urllib.request.urlretrieve(FFMPEG_URL, zip_path, _progress_hook)
    print()  # 换行
    log("SUCCESS", "下载完成:", zip_path)

    # 解压缩
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall("ffmpeg_tmp")

    # 移动到项目 ffmpeg 文件夹
    extracted_dir = [d for d in os.listdir("ffmpeg_tmp") if os.path.isdir(os.path.join("ffmpeg_tmp", d))][0]
    shutil.move(os.path.join("ffmpeg_tmp", extracted_dir), FFMPEG_DIR)

    # 清理临时文件
    os.remove(zip_path)
    shutil.rmtree("ffmpeg_tmp")

    log("SUCCESS", "ffmpeg 已解压到:", FFMPEG_EXE)
    return FFMPEG_EXE
