from flask import Flask, request, send_from_directory
from stream_controller import sc
from storage import sm
from config import HLS_OUTPUT_DIR, HOST, PORT
from utils.app_utils import error, success
from threading import Thread

app = Flask(__name__)


# ----------------------
# 添加或更新流绑定
# ----------------------
@app.route("/api/bind", methods=["POST"])
def bind_stream():
    """
    接收水印PNG并保存本地
    """
    stream_uid = request.form.get("stream_uid")
    file = request.files.get("file")
    url = request.form.get("url")
    save_path = None
    if not stream_uid:
        return error("缺少参数", 400)
    if file:
        save_path = f"watermarks/{stream_uid}.png"
        file.save(save_path)
    # 更新绑定信息
    uid = sm.set_binding(
        url=url,
        watermark_paths=[save_path],
        uid=stream_uid
    )
    return success("绑定成功", {
        "uid": uid,
        "watermark": save_path,
        "hls_url": sm.get_hls_url(uid)
    })


# ----------------------
# 删除绑定
# ----------------------
@app.route("/api/unbind/<uid>", methods=["DELETE"])
def unbind_stream(uid):
    sm.remove_binding(uid)
    sc.stop_stream(uid)
    return success(f"{uid} 解绑成功")


# ----------------------
# 启动转流
# ----------------------
@app.route("/api/start/<uid>", methods=["POST"])
def start_stream(uid):
    sc.start_stream(uid)
    return success(f"{uid} 转流已启动")


# ----------------------
# 停止转流
# ----------------------
@app.route("/api/stop/<uid>", methods=["POST"])
def stop_stream(uid):
    sc.stop_stream(uid)
    return success(f"{uid} 转流已停止")


# ----------------------
# 查询所有绑定信息
# ----------------------
@app.route("/api/list", methods=["GET"])
def list_bindings():
    data = sm.list_bindings()
    return success("绑定列表获取成功", data)


# ----------------------
# 查询当前正在转流的流
# ----------------------
@app.route("/api/running", methods=["GET"])
def list_running():
    data = sc.list_running()
    return success("正在转流列表获取成功", data)


# ----------------------
# HLS 静态文件访问
# ----------------------
@app.route("/hls/<path:filename>")
def serve_hls(filename):
    return send_from_directory(HLS_OUTPUT_DIR, filename)


# ----------------------
# 启动 Flask
# ----------------------
if __name__ == "__main__":
    # 启动监控线程
    monitor_thread = Thread(target=sc.monitor_watermarks, daemon=True)
    monitor_thread.start()
    app.run(host=HOST, port=PORT)