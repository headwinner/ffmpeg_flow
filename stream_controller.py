import subprocess
import os
import signal
import time
import hashlib
from config import BASE_URL
from storage import sm
from utils.utils import log, log_multiline
from utils.init_ffmpeg import init_ffmpeg

FFMPEG_PATH = init_ffmpeg()

class StreamController:
    """
    管理多路视频流转 HLS + 多水印
    支持开启/关闭某条流，检测水印变化自动重启
    """

    def __init__(self, storage_file="./data/stream_map.json", hls_output_dir="./hls"):
        self.processes = {}  # key: uid, value: subprocess.Popen
        self.sm = sm
        self.wm_hash_cache = {}
        # 初始化缓存
        for uid, info in self.sm.list_bindings().items():
            watermarks = [wm for wm in info.get("water_mark", []) if wm]
            self.wm_hash_cache[uid] = {wm: self._file_md5(wm) for wm in watermarks}

    # ----------------------
    # 计算文件 md5
    # ----------------------
    @staticmethod
    def _file_md5(path):
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    # ----------------------
    # 构建 FFmpeg filter_complex 叠加多水印（完全覆盖）
    # ----------------------
    def _build_filter(self, watermark_paths):
        """
        多水印完全覆盖视频
        使用 scale 拉伸水印到视频大小，再 overlay
        """
        filters = ""
        last = "[0:v]"
        for i, wm in enumerate(watermark_paths):
            filters += f"[{i + 1}:v]scale=iw:ih[wm{i}];"
            filters += f"{last}[wm{i}]overlay=0:0:format=auto[v{i}];"
            last = f"[v{i}]"
        if filters.endswith(";"):
            filters = filters[:-1]
        return filters, last

    # ----------------------
    # 启动转流
    # ----------------------
    def start_stream(self, uid, gpu=False):
        if uid in self.processes:
            log("WARNING", f"{uid} 已经在转流中")
            return

        info = self.sm.get_info(uid)
        if not info:
            log("FAIL", f"UID {uid} 未找到绑定信息")
            return

        url = info["url"]
        watermark_paths = [wm for wm in info.get("water_mark", []) if wm]

        # 默认 hls_url 基础路径
        playlist_no_wm = info.get("hls_no_wm")
        playlist_wm = info.get("hls_wm")

        # 构建命令
        cmd = [FFMPEG_PATH, "-loglevel", "error", "-i", url]
        for wm in watermark_paths:
            cmd += ["-i", wm]

        if watermark_paths:
            filter_complex, last = self._build_filter(watermark_paths)
            cmd += [
                "-filter_complex", filter_complex,

                # 带水印输出
                "-map", last, "-map", "0:a?",
                *self._hls_output_args(playlist_wm, gpu),

                # 不带水印输出
                "-map", "0:v", "-map", "0:a?",
                *self._hls_output_args(playlist_no_wm, gpu),
            ]
        else:
            cmd += [
                "-map", "0:v", "-map", "0:a?",
                *self._hls_output_args(playlist_no_wm, gpu),
            ]

        log_multiline("INFO", f"启动转流 {uid}", f"带水印 {BASE_URL}/{playlist_wm}", f"无水印 {BASE_URL}/{playlist_no_wm}")
        sm.update_status(uid, "running")
        process = subprocess.Popen(cmd)
        self.processes[uid] = process

    def _hls_output_args(self, playlist, gpu=False):
        """生成 HLS 输出的公共参数"""
        if gpu:
            vcodec = ["-c:v", "h264_nvenc", "-preset", "p3"]  # GPU
        else:
            vcodec = ["-c:v", "libx264", "-preset", "veryfast"]  # CPU
        return [
            *vcodec, "-r", "15", "-c:a", "aac",
            "-f", "hls", "-hls_time", "5", "-hls_list_size", "5",
            "-hls_flags", "delete_segments", playlist
        ]

    # ----------------------
    # 停止转流
    # ----------------------
    def stop_stream(self, uid):
        if uid not in self.processes:
            log("WARNING", f"{uid} 不在转流列表中")
            return
        process = self.processes.pop(uid)
        os.kill(process.pid, signal.SIGTERM)
        sm.update_status(uid, "stopped")
        log("INFO", f"已停止转流 {uid}")

    # ----------------------
    # 停止所有流
    # ----------------------
    def stop_all(self):
        for uid, process in list(self.processes.items()):
            os.kill(process.pid, signal.SIGTERM)
            log("INFO", f"已停止转流 {uid}")
        self.processes.clear()

    # ----------------------
    # 查询正在运行的流
    # ----------------------
    def list_running(self):
        return list(self.processes.keys())

    # ----------------------
    # 监控水印变化
    # ----------------------
    def monitor_watermarks(self, interval=60):
        """循环检测水印变化，发现变化就重启流"""
        while True:
            for uid in list(self.processes.keys()):
                info = self.sm.get_info(uid)
                if not info:
                    continue
                watermarks = [wm for wm in info.get("water_mark", []) if wm]
                changed = False
                # 检查新增/删除
                cached = self.wm_hash_cache.get(uid, {})
                if set(cached.keys()) != set(watermarks):
                    changed = True
                else:
                    # 检查内容变化
                    for wm in watermarks:
                        md5 = self._file_md5(wm)
                        if md5 != cached.get(wm):
                            changed = True
                            break
                if changed:
                    log("INFO", f"水印变化，重启流 {uid}")
                    self.stop_stream(uid)
                    self.start_stream(uid)
                    self.wm_hash_cache[uid] = {wm: self._file_md5(wm) for wm in watermarks}
            time.sleep(interval)


sc = StreamController()