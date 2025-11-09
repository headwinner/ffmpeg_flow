import subprocess
import os
import signal
import threading
import time
import hashlib
from datetime import datetime
from config import FLOW_URL
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

        # 检测设备
        self.use_gpu = True
        self.has_gpu, self.device_name = self.check_device(self.use_gpu)

        # 初始化缓存
        for uid, info in self.sm.list_bindings().items():
            watermarks = info.get("water_mark", {}) or {}
            self.wm_paths_cache[uid] = dict(watermarks)
            self.wm_md5_cache[uid] = {wm_uid: self._file_md5(path) for wm_uid, path in watermarks.items()}
            self.url_cache[uid] = info.get("url")

        # ------------------------
        # 日志文件路径
        # ------------------------
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d_%H-%M-%S")  # YYYY-MM-DD_HH-MM-SS
        log_dir = os.path.join("logs", date_str)
        os.makedirs(log_dir, exist_ok=True)
        self.log_file_path = os.path.join(log_dir, "stream_controller.log")

        if self.use_gpu:
            if self.has_gpu:
                log("INFO", f"使用 GPU: {self.device_name} 进行转流", log_path=self.log_file_path)
            else:
                log("WARNING", f"GPU 不可用，将使用 CPU: {self.device_name} 进行转流", log_path=self.log_file_path)
        else:
            log("INFO", f"使用 CPU: {self.device_name} 进行转流", log_path=self.log_file_path)

        log_multiline(
            "INFO",
            f"wm_paths_cache: {self.wm_paths_cache}",
            f"wm_md5_cache: {self.wm_md5_cache}",
            f"url_cache: {self.url_cache}",
            log_path=self.log_file_path
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
            else:
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

    def _capture_stderr(self, uid, process):
        """实时捕获 FFmpeg stderr 并写入日志"""
        for line in iter(process.stderr.readline, b''):
            if not line:
                break
            log("FAIL", f"[FFMPEG] {uid}: {line.decode(errors='ignore').strip()}",
                log_path=self.log_file_path)

    # ----------------------
    # 启动转流
    # ----------------------
    def start_stream(self, uid, max_retries=30):
        """启动流，同时启动监控线程自动重启"""
        # 是否使用GPU
        if self.use_gpu:
            gpu = self.has_gpu

        if uid in self.processes:
            self.sm.update_status(uid, "running")
            log("WARNING", f"{uid} 已经在转流中", log_path=self.log_file_path)
            return

        info = self.sm.get_info(uid)
        if not info:
            log("FAIL", f"UID {uid} 未找到绑定信息", log_path=self.log_file_path)
            return

        url = info["url"]
        watermarks = info.get("water_mark", {})  # dict {wm_uid: path}
        wm_paths = [path for path in watermarks.values() if path]
        
        # 记录启动信息
        log_multiline(
            "INFO",
            f"准备启动转流 {uid}",
            f"URL: {url}",
            f"水印数量: {len(wm_paths)}",
            f"水印详情: {watermarks}",
            log_path=self.log_file_path
        )

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

        log_text_list = [f"启动转流 {uid}", f"无水印 https://jrlyy.fusionfintrade.com:39100/{playlist_no_wm}", f"带水印 https://jrlyy.fusionfintrade.com:39100/{playlist_wm}"]
        log_multiline("INFO", *log_text_list, log_path=self.log_file_path)
        self.sm.update_status(uid, "running")

        # 开启独立线程监控 FFmpeg 进程，自动重启
        threading.Thread(target=self._monitor_process, args=(uid, cmd, gpu, max_retries), daemon=True).start()

    def _monitor_process(self, uid, cmd, gpu, max_retries):
        attempt = 0
        while attempt < max_retries:
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0  # 避免 RuntimeWarning
                )
                self.processes[uid] = process

                # 启动 stderr 捕获线程
                threading.Thread(target=self._capture_stderr, args=(uid, process), daemon=True).start()

                log("SUCCESS", f"转流 {uid} 启动成功 (尝试 {attempt + 1})", log_path=self.log_file_path)

                # 等待 FFmpeg 结束
                process.wait()
                if process.returncode != 0:
                    log("FAIL", f"FFmpeg {uid} 异常退出，返回码 {process.returncode}", log_path=self.log_file_path)
                else:
                    log("INFO", f"FFmpeg {uid} 正常退出", log_path=self.log_file_path)
                attempt += 1
                if attempt < max_retries:
                    log("INFO", f"等待 10 秒后重启 {uid} (尝试 {attempt + 1}/{max_retries})", log_path=self.log_file_path)
                    time.sleep(10)
            except Exception as e:
                attempt += 1
                log("FAIL", f"启动 {uid} 异常: {e}", log_path=self.log_file_path)
                if attempt < max_retries:
                    log("INFO", f"等待 10 秒后重试 {uid} (尝试 {attempt + 1}/{max_retries})", log_path=self.log_file_path)
                    time.sleep(10)

        # 超过最大尝试次数仍失败
        if uid in self.processes:
            self.processes.pop(uid, None)
        self.sm.update_status(uid, "stopped")
        log("FAIL", f"转流 {uid} 达到最大重试次数，停止转流", log_path=self.log_file_path)

    def _hls_output_args(self, playlist, gpu=False):
        if gpu:
            vcodec = ["-c:v", "h264_nvenc", "-preset", "p2", "-cq", "19"]
        else:
            vcodec = ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
        return [
            *vcodec,
            "-r", "10",  # 帧率
            "-b:v", "3000k",  # 码率
            "-maxrate", "4000k",
            "-bufsize", "10000k",
            "-c:a", "aac",
            "-f", "hls",
            "-hls_time", "5",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments",
            playlist
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
                if url != cached_url:
                    log("INFO", f"检测到 URL 变化: {cached_url} -> {url}", log_path=self.log_file_path)
                    changed = True
                if watermarks != cached_paths:
                    log("INFO", f"检测到水印路径变化: {cached_paths} -> {watermarks}", log_path=self.log_file_path)
                    changed = True
                else:
                    # md5 不同也认为变化
                    for wm_uid, path in watermarks.items():
                        md5 = self._file_md5(path)
                        if md5 != cached_md5s.get(wm_uid):
                            changed = True
                            break

                if changed:
                    self.stop_stream(uid)
                    time.sleep(1)
                    self.start_stream(uid)
                    # 更新缓存
                    self.wm_paths_cache[uid] = dict(watermarks)
                    self.wm_md5_cache[uid] = {wm_uid: self._file_md5(path) for wm_uid, path in watermarks.items()}
                    self.url_cache[uid] = url

            time.sleep(interval)


sc = StreamController()
