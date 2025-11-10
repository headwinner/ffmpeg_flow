import platform
import subprocess
import os
import signal
import threading
import time
import hashlib
import traceback
from datetime import datetime
import psutil
from storage import sm
from utils.utils import log
from utils.init_ffmpeg import init_ffmpeg

FFMPEG_PATH = init_ffmpeg()



class FFmpegProcessManager:
    """
    独立进程管理类：周期读取 info，根据 status 控制 FFmpeg 进程
    负责启动/停止/重启/错误重试/清理未知 ffmpeg 进程
    状态机：
        need_start → starting → started → need_stop → stopping → stopped
                                     ↘
                                      → need_restart → restarting → started
    """

    def __init__(self, storage_manager, use_gpu=True, has_gpu=False, device_name="CPU"):
        self.sm = storage_manager
        self.use_gpu = use_gpu
        self.has_gpu = has_gpu
        self.device_name = device_name
        self.processes = {}  # uid -> subprocess.Popen
        self.running = True

        # 日志目录
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d_%H-%M-%S")
        log_dir = os.path.join("logs", date_str)
        os.makedirs(log_dir, exist_ok=True)
        self.log_file_path = os.path.join(log_dir, "stream_controller.log")

        threading.Thread(target=self._auto_manager_loop, daemon=True).start()

    # ------------------------------------
    # 主循环：周期检测并根据状态操作
    # ------------------------------------
    def _auto_manager_loop(self):
        while self.running:
            try:
                bindings = self.sm.list_bindings()
                valid_uids = set(bindings.keys())
                # 清理未知 ffmpeg
                self._kill_unknown_ffmpeg(valid_uids)

                for uid, info in bindings.items():
                    status = info.get("status")

                    if status in ("need_start", "restarting"):
                        self.sm.update_status(uid, "starting")
                        self._start_ffmpeg(uid, info)
                        self.sm.update_status(uid, "started")

                    elif status == "need_stop":
                        self.sm.update_status(uid, "stopping")
                        self._stop_ffmpeg(uid)
                        self.sm.update_status(uid, "stopped")

                    elif status == "need_restart":
                        self.sm.update_status(uid, "restarting")
                        self._stop_ffmpeg(uid)
                        self._start_ffmpeg(uid, info)
                        self.sm.update_status(uid, "started")

                    elif status == "started":
                        # 检测异常退出
                        if uid not in self.processes or self.processes[uid].poll() is not None:
                            log("FAIL", f"[监控] {uid} 异常退出，标记 need_restart", log_path=self.log_file_path)
                            self.sm.update_status(uid, "need_restart")

                time.sleep(10)

            except Exception as e:
                log("FAIL", f"[进程管理] 主循环异常: {e}", log_path=self.log_file_path)
                time.sleep(5)

    # ------------------------------------
    # 杀死陌生 ffmpeg 进程
    # ------------------------------------
    def _kill_unknown_ffmpeg(self, valid_uids):
        try:
            killed = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    name = proc.info['name']
                    cmdline = " ".join(proc.info['cmdline']) if proc.info['cmdline'] else ""
                    if not name:
                        continue

                    if "ffmpeg" in name.lower():
                        if not any(uid in cmdline for uid in valid_uids):
                            pid = proc.info['pid']
                            killed.append(pid)
                            proc.terminate()
                            log("WARN", f"检测到未知 ffmpeg 进程 {pid}，正在终止", log_path=self.log_file_path)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if killed:
                log("INFO", f"已清理未知 ffmpeg 进程: {killed}", log_path=self.log_file_path)

        except Exception as e:
            log("FAIL", f"杀死未知 ffmpeg 进程失败: {e}", log_path=self.log_file_path)

    # ------------------------------------
    # 启动 FFmpeg
    # ------------------------------------
    def _start_ffmpeg(self, uid, info, max_retries=5):
        for attempt in range(1, max_retries + 1):
            try:
                url = info.get("url")
                watermarks = info.get("water_mark", {}) or {}
                wm_paths = list(watermarks.values())
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
                        *self._hls_output_args(playlist_wm, self.use_gpu and self.has_gpu),
                        "-map", "0:v", "-map", "0:a?",
                        *self._hls_output_args(playlist_no_wm, self.use_gpu and self.has_gpu)
                    ]
                else:
                    cmd += [
                        "-map", "0:v", "-map", "0:a?",
                        *self._hls_output_args(playlist_no_wm, self.use_gpu and self.has_gpu)
                    ]

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0
                )
                self.processes[uid] = process
                threading.Thread(target=self._capture_stderr, args=(uid, process), daemon=True).start()
                log("SUCCESS", f"启动 FFmpeg 成功: {uid}", log_path=self.log_file_path)
                return

            except Exception as e:
                log("FAIL", f"启动 {uid} 失败 (尝试 {attempt}/{max_retries}): {e}", log_path=self.log_file_path)
                time.sleep(5)

        log("FAIL", f"{uid} 启动失败超过最大重试次数", log_path=self.log_file_path)
        self.sm.update_status(uid, "need_restart")

    # ------------------------------------
    # 停止 FFmpeg
    # ------------------------------------
    def _stop_ffmpeg(self, uid):
        try:
            process = self.processes.pop(uid)
            os.kill(process.pid, signal.SIGTERM)
            log("INFO", f"停止转流 {uid}", log_path=self.log_file_path)
        except Exception as e:
            log("FAIL", f"停止 {uid} 失败: {e}", log_path=self.log_file_path)

    # ------------------------------------
    # 捕获 FFmpeg stderr
    # ------------------------------------
    def _capture_stderr(self, uid, process):
        for line in iter(process.stderr.readline, b''):
            if not line:
                break
            log("FAIL", f"[FFMPEG] {uid}: {line.decode(errors='ignore').strip()}",
                log_path=self.log_file_path)

    # ------------------------------------
    # 构建水印滤镜
    # ------------------------------------
    @staticmethod
    def _build_filter(watermark_paths):
        filters = ""
        last = "[0:v]"
        for i, path in enumerate(watermark_paths.values()):
            filters += f"[{i + 1}:v]scale=iw:ih[wm{i}];"
            filters += f"{last}[wm{i}]overlay=0:0:format=auto[v{i}];"
            last = f"[v{i}]"
        if filters.endswith(";"):
            filters = filters[:-1]
        return filters, last

    # ------------------------------------
    # 输出 HLS 参数
    # ------------------------------------
    @staticmethod
    def _hls_output_args(playlist, gpu=False):
        if gpu:
            vcodec = ["-c:v", "h264_nvenc", "-preset", "p2", "-cq", "19"]
        else:
            vcodec = ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
        return [
            *vcodec,
            "-r", "10",
            "-b:v", "3000k",
            "-maxrate", "4000k",
            "-bufsize", "10000k",
            "-c:a", "aac",
            "-f", "hls",
            "-hls_time", "5",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            playlist
        ]


class StreamController:
    """只负责状态、缓存和水印变化检测"""

    def __init__(self, sm):
        self.sm = sm
        self.wm_paths_cache = {}
        self.wm_md5_cache = {}
        self.url_cache = {}

        self.use_gpu = True
        self.has_gpu, self.device_name = self.check_device()
        now = datetime.now()
        log_dir = os.path.join("logs", now.strftime("%Y-%m-%d_%H-%M-%S"))
        os.makedirs(log_dir, exist_ok=True)
        self.log_file_path = os.path.join(log_dir, "stream_controller.log")

        # 初始化缓存
        for uid, info in self.sm.list_bindings().items():
            watermarks = info.get("water_mark", {}) or {}
            self.wm_paths_cache[uid] = dict(watermarks)
            self.wm_md5_cache[uid] = {wm_uid: self._file_md5(p) for wm_uid, p in watermarks.items()}
            self.url_cache[uid] = info.get("url")

        # 启动独立进程管理器
        self.process_manager = FFmpegProcessManager(
            storage_manager=self.sm,
            use_gpu=self.use_gpu,
            has_gpu=self.has_gpu,
            device_name=self.device_name
        )

    def check_device(self):
        try:
            result = subprocess.run(
                [FFMPEG_PATH, "-hide_banner", "-encoders"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            has_gpu = "h264_nvenc" in result.stdout
            return has_gpu, "GPU" if has_gpu else "CPU"
        except Exception:
            return False, "未知"

    @staticmethod
    def _file_md5(path):
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    def monitor_watermarks(self, interval=10):
        """监控 URL 和水印变化"""
        while True:
            for uid, info in self.sm.list_bindings().items():
                watermarks = info.get("water_mark", {}) or {}
                url = info.get("url")
                cached_paths = self.wm_paths_cache.get(uid)
                cached_md5s = self.wm_md5_cache.get(uid, {})
                cached_url = self.url_cache.get(uid)

                changed = False
                if url != cached_url or watermarks != cached_paths:
                    changed = True
                else:
                    for wm_uid, path in watermarks.items():
                        md5 = self._file_md5(path)
                        if md5 != cached_md5s.get(wm_uid):
                            changed = True
                            break

                if changed:
                    log("INFO", f"检测到 {uid} 的水印或URL变化，更新状态", log_path=self.log_file_path)
                    self.sm.update_status(uid, "restart")
                    self.wm_paths_cache[uid] = dict(watermarks)
                    self.wm_md5_cache[uid] = {wm_uid: self._file_md5(p) for wm_uid, p in watermarks.items()}
                    self.url_cache[uid] = url
            time.sleep(interval)


sc = StreamController(sm)
