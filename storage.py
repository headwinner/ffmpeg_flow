import os
import json
import uuid
from config import STORAGE_JSON_FILE, HLS_OUTPUT_DIR, DATA_FILE, WATER_MARK_PATH

os.makedirs(DATA_FILE, exist_ok=True)
os.makedirs(HLS_OUTPUT_DIR, exist_ok=True)
os.makedirs(WATER_MARK_PATH, exist_ok=True)


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
        self.stop_all_streams()

    # ----------------------
    # 将所有流状态设为 stopped
    # ----------------------
    def stop_all_streams(self):
        data = self._load()
        updated = False
        for uid, info in data.items():
            if info.get("status") != "stopped":
                info["status"] = "stopped"
                updated = True
        if updated:
            self._save(data)

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
        watermark_paths: dict {wm_uid: path}
        status: 流状态，默认 stopped
        """
        if uid is None:
            uid = str(uuid.uuid4())
        if watermark_paths is None:
            watermark_paths = {}

        data = self._load()
        playlist_base = f"{self.hls_output_dir}/{uid}"
        playlist_no_wm = f"{playlist_base}_no_wm.m3u8"
        playlist_wm = f"{playlist_base}_wm.m3u8"

        data[uid] = {
            "url": url,
            "water_mark": watermark_paths,  # 改为 {wm_uid: path}
            "hls_no_wm": playlist_no_wm,
            "hls_wm": playlist_wm,
            "status": status
        }
        self._save(data)

    # ----------------------
    # 更新流默认水印
    # ----------------------
    def update_watermark(self, uid, watermark_paths):
        """
        更新指定流的默认水印
        :param uid: 流的唯一ID
        :param watermark_paths: dict {wm_uid: path}
        :return: True 更新成功，False UID不存在
        """
        data = self._load()
        stream_data = data.get(uid)
        if not stream_data:
            return False

        stream_data["water_mark"] = watermark_paths
        self._save(data)
        return True

    # ----------------------
    # 更新指定围栏/水印
    # ----------------------
    def update_watermark_by_wm_uid(self, uid, wm_uid, watermark_path):
        """
        更新指定流的某个围栏/水印
        :param uid: 流的唯一ID
        :param wm_uid: 围栏/水印唯一ID
        :param watermark_path: 文件路径
        :return: True 更新成功，False UID不存在
        """
        data = self._load()
        stream_data = data.get(uid)
        if not stream_data:
            return False

        if "water_mark" not in stream_data:
            stream_data["water_mark"] = {}

        stream_data["water_mark"][wm_uid] = watermark_path
        self._save(data)
        return True



    # ----------------------
    # 更新url
    # ----------------------
    def update_url(self, uid, url):
        data = self._load()
        data[uid]["url"] = url
        self._save(data)
        return True


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
        return info.get("water_mark", {}) if info else {}


    def get_hls_url(self, uid):
        info = self.get_info(uid)
        return info["hls_no_wm"] if info else None


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


    # ----------------------
    # 清空水印
    # ----------------------
    def clear_watermarks(self, uid):
        data = self._load()
        stream_data = data.get(uid)
        if not stream_data:
            return False

        wm_dict = stream_data.get("water_mark", {})
        for wm_path in wm_dict.values():
            try:
                if wm_path and os.path.exists(wm_path):
                    os.remove(wm_path)
            except Exception as e:
                print(f"[WARN] 删除水印文件失败: {wm_path}, 错误: {e}")

        stream_data["water_mark"] = {}
        self._save(data)
        return True


sm = StorageManager()
