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

    def __init__(self):
        self.processes = {}  # key: uid, value: subprocess.Popen
        self.sm = sm
        # 拆成两个缓存：路径快照（用于 dict 比较）和 md5 缓存（用于内容比较）
        self.wm_paths_cache = {}  # { uid: {wm_uid: path, ...}, ... }
        self.wm_md5_cache = {}    # { uid: {wm_uid: md5, ...}, ... }

        # 初始化缓存（注意：如果 list_bindings 返回的 water_mark 为空，存空 dict）
        for uid, info in self.sm.list_bindings().items():
            watermarks = info.get("water_mark", {}) or {}
            self.wm_paths_cache[uid] = dict(watermarks)
            self.wm_md5_cache[uid] = {wm_uid: self._file_md5(path) for wm_uid, path in watermarks.items()}
        print(self.wm_paths_cache)
        print(self.wm_md5_cache)

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
        watermark_paths: dict {wm_uid: path}
        使用 scale 拉伸水印到视频大小，再 overlay
        """
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
    def start_stream(self, uid, gpu=False):
        if uid in self.processes:
            log("WARNING", f"{uid} 已经在转流中")
            return

        info = self.sm.get_info(uid)
        if not info:
            log("FAIL", f"UID {uid} 未找到绑定信息")
            return

        url = info["url"]
        watermarks = info.get("water_mark", {})  # dict {wm_uid: path}
        wm_paths = [path for path in watermarks.values() if path]  # 取路径列表

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

        log_text_list = [f"启动转流 {uid}", f"无水印 {BASE_URL}/{playlist_no_wm}"]
        log_text_list += [f"带水印 {BASE_URL}/{playlist_wm}"]
        log_multiline("INFO", *log_text_list)
        self.sm.update_status(uid, "running")
        process = subprocess.Popen(cmd)
        self.processes[uid] = process

    def _hls_output_args(self, playlist, gpu=False):
        """生成 HLS 输出的公共参数"""
        if gpu:
            vcodec = ["-c:v", "h264_nvenc", "-preset", "p3"]  # GPU
        else:
            vcodec = ["-c:v", "libx264", "-preset", "medium"]  # CPU
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
            sm.update_status(uid, "stopped")
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
            try:
                if process.poll() is None:  # 进程还在运行
                    process.terminate()  # 安全结束进程
                    log("INFO", f"已停止转流 {uid}")
            except Exception as e:
                log("ERROR", f"停止转流 {uid} 失败: {e}")
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
        """循环检测水印变化，发现变化就重启流
        逻辑：
          1. 比较当前 water_mark dict 和 wm_paths_cache（路径快照）是否相等；
             - 不相等 -> changed
          2. 如果路径快照相等，再逐个比较文件 md5（wm_md5_cache）；
             - md5 不同 -> changed
        """
        while True:
            for uid in list(self.processes.keys()):
                info = self.sm.get_info(uid)
                if not info:
                    continue

                watermarks = info.get("water_mark", {}) or {}  # dict {wm_uid: path}
                cached_paths = self.wm_paths_cache.get(uid)
                cached_md5s = self.wm_md5_cache.get(uid, {})

                changed = False

                if watermarks != cached_paths:
                    changed = True
                else:
                    for wm_uid, path in watermarks.items():
                        md5 = self._file_md5(path)
                        if md5 != cached_md5s.get(wm_uid):
                            changed = True
                            break

                if changed:
                    log("INFO", f"检测到水印变化，重启流 uid={uid}")
                    self.stop_stream(uid)
                    time.sleep(1)
                    self.start_stream(uid)
                    self.wm_paths_cache[uid] = dict(watermarks)
                    self.wm_md5_cache[uid] = {wm_uid: self._file_md5(path) for wm_uid, path in watermarks.items()}

            time.sleep(interval)


sc = StreamController()

