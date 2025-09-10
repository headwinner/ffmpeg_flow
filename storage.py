import os
import json
from threading import Lock
import uuid
from config import STORAGE_JSON_FILE, HLS_OUTPUT_DIR, DATA_FILE

os.makedirs(DATA_FILE, exist_ok=True)
os.makedirs(HLS_OUTPUT_DIR, exist_ok=True)

class StorageManager:
    """
    管理视频流 UID、URL、多水印及 HLS 输出路径
    每次操作都会 load JSON 文件，保证数据最新
    """

    def __init__(self, storage_file=STORAGE_JSON_FILE, hls_output_dir=HLS_OUTPUT_DIR):
        self.storage_file = storage_file
        self.hls_output_dir = hls_output_dir
        self._ensure_file()

        if not os.path.exists(hls_output_dir):
            os.makedirs(hls_output_dir)

    # ----------------------
    # 确保 JSON 文件存在
    # ----------------------
    def _ensure_file(self):
        if not os.path.exists(self.storage_file):
            os.makedirs(os.path.dirname(self.storage_file), exist_ok=True)
            with open(self.storage_file, "w") as f:
                json.dump({}, f)

    # ----------------------
    # 加载
    # ----------------------
    def _load(self):
        with open(self.storage_file, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}

    # ----------------------
    # 保存
    # ----------------------
    def _save(self, data):
        with open(self.storage_file, "w") as f:
            json.dump(data, f, indent=2)

    # ----------------------
    # 添加或更新绑定关系
    # ----------------------
    def set_binding(self, url=None, watermark_paths=None, uid=None, status="stopped"):
        """
        uid: 可选，如果为空自动生成
        url: 流地址
        watermark_paths: png列表
        status: 流状态，默认 stopped
        """
        if uid is None:
            uid = str(uuid.uuid4())  # 自动生成唯一ID
        if not isinstance(watermark_paths, list):
            watermark_paths = [watermark_paths]
        data = self._load()
        # 基础路径
        playlist_base = f"{self.hls_output_dir}/{uid}"
        playlist_no_wm = f"{playlist_base}_no_wm.m3u8"
        playlist_wm = f"{playlist_base}_wm.m3u8"
        data[uid] = {
            "url": url,
            "water_mark": watermark_paths,
            "hls_no_wm": playlist_no_wm,
            "hls_wm": playlist_wm,
            "status": status
        }
        self._save(data)

        return uid  # 返回最终 UID

    # ----------------------
    # 删除绑定
    # ----------------------
    def remove_binding(self, uid):
        data = self._load()
        if uid in data:
            del data[uid]
            self._save(data)

    # ----------------------
    # 查询信息
    # ----------------------
    def get_info(self, uid):
        data = self._load()
        return data.get(uid, None)

    def get_url(self, uid):
        info = self.get_info(uid)
        return info["url"] if info else None

    def get_watermarks(self, uid):
        info = self.get_info(uid)
        return info["water_mark"] if info else []

    def get_hls_url(self, uid):
        info = self.get_info(uid)
        return info["hls_url"] if info else None

    def list_bindings(self):
        return self._load()

    # ----------------------
    # 更新状态
    # ----------------------
    def update_status(self, uid, status):
        data = self._load()
        if uid in data:
            data[uid]["status"] = status
            self._save(data)
            return True
        return False

sm = StorageManager()