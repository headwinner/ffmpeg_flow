import os
import urllib.request
import zipfile
import shutil

FFMPEG_DIR = os.path.join(os.getcwd(), "ffmpeg")
FFMPEG_EXE = os.path.join(FFMPEG_DIR, "bin", "ffmpeg.exe")
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"  # Windows release 包

def init_ffmpeg():
    """初始化 ffmpeg，如果没有就自动下载并解压"""
    if os.path.exists(FFMPEG_EXE):
        print("[INFO] ffmpeg 已存在:", FFMPEG_EXE)
        return FFMPEG_EXE

    print("[INFO] 未找到 ffmpeg，开始下载...")
    zip_path = "ffmpeg.zip"

    # 下载 zip
    urllib.request.urlretrieve(FFMPEG_URL, zip_path)
    print("[INFO] 下载完成:", zip_path)

    # 解压缩
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall("ffmpeg_tmp")

    # 移动到项目 ffmpeg 文件夹
    extracted_dir = [d for d in os.listdir("ffmpeg_tmp") if os.path.isdir(os.path.join("ffmpeg_tmp", d))][0]
    shutil.move(os.path.join("ffmpeg_tmp", extracted_dir), FFMPEG_DIR)

    # 清理临时文件
    os.remove(zip_path)
    shutil.rmtree("ffmpeg_tmp")

    print("[INFO] ffmpeg 已解压到:", FFMPEG_EXE)
    return FFMPEG_EXE
