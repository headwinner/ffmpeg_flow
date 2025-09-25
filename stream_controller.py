import subprocess
import os
import signal
import time
import hashlib
from datetime import datetime
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

    def __init__(self):
        self.processes = {}  # key: uid, value: subprocess.Popen
        self.sm = sm
        # 拆成三个缓存：路径快照、md5缓存、url缓存
        self.wm_paths_cache = {}  # { uid: {wm_uid: path, ...}, ... }
        self.wm_md5_cache = {}  # { uid: {wm_uid: md5, ...}, ... }
        self.url_cache = {}  # { uid: url }

        # 初始化缓存
        for uid, info in self.sm.list_bindings().items():
            watermarks = info.get("water_mark", {}) or {}
            self.wm_paths_cache[uid] = dict(watermarks)
            self.wm_md5_cache[uid] = {wm_uid: self._file_md5(path) for wm_uid, path in watermarks.items()}
            self.url_cache[uid] = info.get("url")

        # ------------------------
        # 日志文件路径
        # ------------------------
        os.makedirs("logs", exist_ok=True)
        now_str = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_file_path = f"logs/stream_controller-{now_str}.log"

        log_multiline("INFO",
                      f"wm_paths_cache: {self.wm_paths_cache}",
                      f"wm_md5_cache: {self.wm_md5_cache}",
                      f"url_cache: {self.url_cache}",
                      log_path=self.log_file_path)

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
        filters = ""
        last = "[0:v]"
        for i, path in enumerate(watermark_paths.values()):
            filters += f"[{i + 1}:v]scale=iw:ih[wm{i}];"
            filters += f"{last}[wm{i}]overlay=0:0:format=auto[v{i}];"
            last = f"[v{i}]"
        if filters.endswith(";"):
            filters = filters[:-1]
        return filters, last

    # ----------------------
    # 启动转流
    # ----------------------
    def start_stream(self, uid, gpu=False, max_retries=3):
        if uid in self.processes:
            log("WARNING", f"{uid} 已经在转流中", log_path=self.log_file_path)
            return

        info = self.sm.get_info(uid)
        if not info:
            log("FAIL", f"UID {uid} 未找到绑定信息", log_path=self.log_file_path)
            return

        url = info["url"]
        watermarks = info.get("water_mark", {})  # dict {wm_uid: path}
        wm_paths = [path for path in watermarks.values() if path]

        playlist_no_wm = info.get("hls_no_wm")
        playlist_wm = info.get("hls_wm")

        cmd = [FFMPEG_PATH, "-loglevel", "error", "-i", url]
        for wm in wm_paths:
            cmd += ["-i", wm]

        if wm_paths:
            filter_complex, last = self._build_filter(watermarks)
            cmd += [
                "-filter_complex", filter_complex,
                "-map", last, "-map", "0:a?",
                *self._hls_output_args(playlist_wm, gpu),
                "-map", "0:v", "-map", "0:a?",
                *self._hls_output_args(playlist_no_wm, gpu),
            ]
        else:
            cmd += [
                "-map", "0:v", "-map", "0:a?",
                *self._hls_output_args(playlist_no_wm, gpu),
                "-map", "0:v", "-map", "0:a?",
                *self._hls_output_args(playlist_wm, gpu),
            ]

        log_text_list = [f"启动转流 {uid}", f"无水印 {BASE_URL}/{playlist_no_wm}", f"带水印 {BASE_URL}/{playlist_wm}"]
        log_multiline("INFO", *log_text_list, log_path=self.log_file_path)
        self.sm.update_status(uid, "running")

        for attempt in range(1, max_retries + 1):
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                time.sleep(1)
                if process.poll() is not None:
                    # FFmpeg 立即退出，读取 stderr
                    stderr = process.stderr.read().decode(errors="ignore")
                    log_multiline(
                        "FAIL",
                        f"FFmpeg 启动失败 {uid} (尝试 {attempt}/{max_retries}):",
                        stderr,
                        log_path=self.log_file_path
                    )
                    raise RuntimeError("FFmpeg 进程立即退出")
                self.processes[uid] = process
                log("SUCCESS", f"转流 {uid} 启动成功 (尝试 {attempt})", log_path=self.log_file_path)
                return
            except Exception as e:
                if attempt < max_retries:
                    log("INFO", f"等待 2 秒后重试 {uid}", log_path=self.log_file_path)
                    time.sleep(2)
                else:
                    log("FAIL", f"转流 {uid} 启动失败，停止转流", log_path=self.log_file_path)
                    self.sm.update_status(uid, "stopped")

    def _hls_output_args(self, playlist, gpu=False):
        if gpu:
            vcodec = ["-c:v", "h264_nvenc", "-preset", "p3"]
        else:
            vcodec = ["-c:v", "libx264", "-preset", "medium"]
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
            log("WARNING", f"{uid} 不在转流列表中", log_path=self.log_file_path)
            sm.update_status(uid, "stopped")
            return
        process = self.processes.pop(uid)
        os.kill(process.pid, signal.SIGTERM)
        sm.update_status(uid, "stopped")
        log("INFO", f"已停止转流 {uid}", log_path=self.log_file_path)

    # ----------------------
    # 停止所有流
    # ----------------------
    def stop_all(self):
        for uid, process in list(self.processes.items()):
            try:
                if process.poll() is None:
                    process.terminate()
                    log("INFO", f"已停止转流 {uid}", log_path=self.log_file_path)
            except Exception as e:
                log("FAIL", f"停止转流 {uid} 失败: {e}", log_path=self.log_file_path)
        self.processes.clear()

    # ----------------------
    # 查询正在运行的流
    # ----------------------
    def list_running(self):
        return list(self.processes.keys())

    # ----------------------
    # 监控水印变化
    # ----------------------
    def monitor_watermarks(self, interval=10):
        """循环检测水印和 URL 变化，发现变化就重启流"""
        while True:
            for uid in list(self.processes.keys()):
                info = self.sm.get_info(uid)
                if not info:
                    continue

                watermarks = info.get("water_mark", {}) or {}
                url = info.get("url")
                cached_paths = self.wm_paths_cache.get(uid)
                cached_md5s = self.wm_md5_cache.get(uid, {})
                cached_url = self.url_cache.get(uid)

                changed = False

                # URL 或水印路径变化
                if url != cached_url or watermarks != cached_paths:
                    changed = True
                else:
                    # md5 不同也认为变化
                    for wm_uid, path in watermarks.items():
                        md5 = self._file_md5(path)
                        if md5 != cached_md5s.get(wm_uid):
                            changed = True
                            break

                if changed:
                    log("INFO", f"检测到 URL 或水印变化，重启流 uid={uid}", log_path=self.log_file_path)
                    self.stop_stream(uid)
                    time.sleep(1)
                    self.start_stream(uid)
                    # 更新缓存
                    self.wm_paths_cache[uid] = dict(watermarks)
                    self.wm_md5_cache[uid] = {wm_uid: self._file_md5(path) for wm_uid, path in watermarks.items()}
                    self.url_cache[uid] = url

            time.sleep(interval)

            time.sleep(interval)


sc = StreamController()
