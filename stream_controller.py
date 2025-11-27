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
    负责启动/停止/重启/清理未知 ffmpeg 进程
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
        # 保存异常次数
        error_counts = {}  # uid -> 异常次数

        while self.running:
            try:
                bindings = self.sm.list_bindings()
                valid_uids = set(bindings.keys())
                # 清理未知 ffmpeg
                self._kill_unknown_ffmpeg(valid_uids)

                for uid, info in bindings.items():
                    status = info.get("status")

                    if status == "need_start":
                        self.sm.update_status(uid, "starting")
                        log("INFO", f"{uid} 启动中...", log_path=self.log_file_path)
                        self._start_ffmpeg(uid, info)
                        self.sm.update_status(uid, "started")
                        error_counts[uid] = 0  # 启动成功，重置异常计数

                    elif status == "need_stop":
                        self.sm.update_status(uid, "stopping")
                        log("INFO", f"{uid} 停止中...", log_path=self.log_file_path)
                        self._stop_ffmpeg(uid)
                        self.sm.update_status(uid, "stopped")
                        error_counts[uid] = 0  # 停止成功，重置异常计数

                    elif status == "need_restart":
                        self.sm.update_status(uid, "starting")
                        log("INFO", f"{uid} 重启中...", log_path=self.log_file_path)
                        self._stop_ffmpeg(uid)
                        self._start_ffmpeg(uid, info)
                        self.sm.update_status(uid, "started")
                        error_counts[uid] = 0  # 重启成功，重置异常计数

                    elif status == "started":
                        proc = self.processes.get(uid)
                        if proc is None or proc.poll() is not None:
                            # 异常退出，增加计数
                            error_counts[uid] = error_counts.get(uid, 0) + 1
                            log("WARN", f"[监控] {uid} 异常退出，计数 {error_counts[uid]}", log_path=self.log_file_path)

                            # 如果连续异常 >= 3，标记 need_restart
                            if error_counts[uid] >= 3:
                                log("FAIL", f"[监控] {uid} 异常退出 3 次，标记 need_restart", log_path=self.log_file_path)
                                self.sm.update_status(uid, "need_restart")
                                error_counts[uid] = 0  # 重置计数
                            else:
                                # 等待 60 秒再检测下一轮
                                time.sleep(60)
                        else:
                            # 正常运行，重置异常计数
                            error_counts[uid] = 0

                # 每轮循环可以加短延迟，避免 CPU 占用过高
                time.sleep(1)

            except Exception as e:
                log("ERROR", f"_auto_manager_loop 异常: {e}", log_path=self.log_file_path)
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
    def _start_ffmpeg(self, uid, info):
        try:
            log("INFO", f"启动 FFmpeg 进程 {uid}", log_path=self.log_file_path)
            # 检查是否已经有相同的进程在运行
            if uid in self.processes:
                process = self.processes[uid]
                if process.poll() is None:  # 进程仍在运行
                    log("INFO", f"FFmpeg 进程 {uid} 已经在运行，重启中", log_path=self.log_file_path)
                    self.sm.update_status(uid, "need_restart")
                    return  # 跳过启动操作

            # 继续启动新的 FFmpeg 进程
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

            # 启动新的进程
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            self.processes[uid] = process
            threading.Thread(target=self._capture_stderr, args=(uid, process), daemon=True).start()
            log("SUCCESS", f"启动 FFmpeg 成功: {uid}", log_path=self.log_file_path)

        except Exception as e:
            log("FAIL", f"启动 {uid} 失败: {e}", log_path=self.log_file_path)
            self.sm.update_status(uid, "need_start")

    # ------------------------------------
    # 停止 FFmpeg
    # ------------------------------------
    def _stop_ffmpeg(self, uid):
        try:
            process = self.processes.pop(uid, None)
            if not process:
                log("WARN", f"停止 {uid} 时未找到对应进程对象", log_path=self.log_file_path)
                return

            # 检查进程是否仍在运行
            if process.poll() is None:
                os.kill(process.pid, signal.SIGTERM)
                log("INFO", f"停止转流 {uid}（PID={process.pid}）", log_path=self.log_file_path)
            else:
                log("INFO", f"{uid} 进程已退出，无需停止", log_path=self.log_file_path)

        except ProcessLookupError:
            log("WARN", f"{uid} 进程不存在或已结束（PID={getattr(process, 'pid', '未知')}）", log_path=self.log_file_path)

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

    # ----------------------
    # 检测系统设备（GPU 或 CPU）
    # ----------------------
    def check_device(self, use_gpu=True):
        """ 检查系统设备（GPU 或 CPU），返回 (是否启用GPU, 设备名称) """
        try:
            import platform

            # 默认结果
            has_gpu = False
            device_name = "未知"

            # 检查 ffmpeg 是否支持 GPU 编码
            result = subprocess.run(
                [FFMPEG_PATH, "-hide_banner", "-encoders"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            has_gpu = "h264_nvenc" in result.stdout

            # 获取系统平台
            system = platform.system().lower()

            # 优先检测 GPU（如果启用）
            if has_gpu and use_gpu:
                try:
                    smi_result = subprocess.run(
                        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    gpu_name = smi_result.stdout.strip()
                    if gpu_name:
                        device_name = gpu_name
                    else:
                        device_name = "NVIDIA GPU (未知型号)"
                        use_gpu = False
                except Exception:
                    device_name = "GPU 可用但无法通过 nvidia-smi 获取名称"
                    use_gpu = False
            if not use_gpu:
                # 检测 CPU 型号
                try:
                    if system == "windows":
                        # Windows 平台
                        cpu_result = subprocess.run(
                            ["wmic", "cpu", "get", "name"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True
                        )
                        lines = [l.strip() for l in cpu_result.stdout.splitlines() if l.strip()]
                        if len(lines) > 1:
                            device_name = lines[1]  # 第二行是CPU名称
                        else:
                            device_name = "CPU (型号未知)"
                    elif system == "linux":
                        # Linux 平台
                        cpu_result = subprocess.run(
                            ["cat", "/proc/cpuinfo"],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True
                        )
                        for line in cpu_result.stdout.split('\n'):
                            if "model name" in line:
                                device_name = line.split(":")[1].strip()
                                break
                    else:
                        device_name = f"未知系统: {system}"
                except Exception:
                    device_name = "CPU (型号未知)"

            return has_gpu and use_gpu, device_name

        except Exception as e:
            log("FAIL", f"检测设备异常: {e}", log_path=self.log_file_path)
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
                changed_details = []

                if url != cached_url or watermarks != cached_paths:
                    changed = True
                    if url != cached_url:
                        changed_details.append(f"URL更新")
                    if watermarks != cached_paths:
                        changed_details.append(f"水印文件路径更新")
                else:
                    for wm_uid, path in watermarks.items():
                        md5 = self._file_md5(path)
                        if md5 != cached_md5s.get(wm_uid):
                            changed = True
                            changed_details.append(f"水印文件的 MD5 值发生变化")
                            break

                if changed:
                    log_details = "; ".join(changed_details)
                    if info.get("status") not in ("need_stop", "stopped", "stopping"):
                        log("INFO", f"检测到 {uid} 的配置变化: {log_details}，更新状态", log_path=self.log_file_path)
                        self.sm.update_status(uid, "need_restart")
                    self.wm_paths_cache[uid] = dict(watermarks)
                    self.wm_md5_cache[uid] = {wm_uid: self._file_md5(p) for wm_uid, p in watermarks.items()}
                    self.url_cache[uid] = url

            time.sleep(interval)


sc = StreamController(sm)
