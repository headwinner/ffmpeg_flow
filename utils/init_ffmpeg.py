import os
import urllib.request
import zipfile
import shutil
from utils.utils import log

FFMPEG_DIR = os.path.join(os.getcwd(), "ffmpeg")
FFMPEG_EXE = os.path.join(FFMPEG_DIR, "bin", "ffmpeg.exe")
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"  # Windows release 包

def _progress_hook(count, block_size, total_size):
    """下载进度回调"""
    percent = int(count * block_size * 100 / total_size)
    if percent > 100:
        percent = 100
    log("[DOWNLOAD]", f"下载进度: {percent}%")

def init_ffmpeg():
    """初始化 ffmpeg，如果没有就自动下载并解压"""
    if os.path.exists(FFMPEG_EXE):
        log("[INFO]", "ffmpeg 已存在:", FFMPEG_EXE)
        return FFMPEG_EXE

    log("[INFO]", "未找到 ffmpeg，开始下载...")
    zip_path = "ffmpeg.zip"

    # 下载 zip 带进度
    urllib.request.urlretrieve(FFMPEG_URL, zip_path, _progress_hook)
    log("[SUCCESS]", "下载完成:", zip_path)

    # 解压缩
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall("ffmpeg_tmp")

    # 移动到项目 ffmpeg 文件夹
    extracted_dir = [d for d in os.listdir("ffmpeg_tmp") if os.path.isdir(os.path.join("ffmpeg_tmp", d))][0]
    shutil.move(os.path.join("ffmpeg_tmp", extracted_dir), FFMPEG_DIR)

    # 清理临时文件
    os.remove(zip_path)
    shutil.rmtree("ffmpeg_tmp")

    log("[SUCCESS]", "ffmpeg 已解压到:", FFMPEG_EXE)
    return FFMPEG_EXE
