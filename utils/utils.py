import sys
import threading
import os
from datetime import datetime
from typing import Optional

# 全局输出锁，保证线程安全
_stdout_lock = threading.Lock()

# 判断是否在 PyCharm 中
_IN_PYCHARM = os.getenv("PYCHARM_HOSTED") == "1"

# ANSI 颜色定义（仅在终端生效）
COLORS = {
    "SUCCESS": "\033[32m",  # 绿色
    "FAIL": "\033[31m",     # 红色
    "WARN": "\033[33m",     # 黄色
    "INFO": "\033[36m",     # 青色
}
RESET_COLOR = "\033[0m"


def _safe_console_write(text: str):
    """安全写入控制台，不使用 print()，兼容 PyCharm / Windows"""
    with _stdout_lock:
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def log(level: str, message: str, log_path: Optional[str] = None):
    """统一日志打印，带时间戳，可安全写入文件"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = COLORS.get(level, "") if not _IN_PYCHARM else ""
    reset = RESET_COLOR if color else ""
    formatted_message = f"{timestamp} [{level}] {message}"
    colored_message = f"{color}{formatted_message}{reset}"

    # 控制台输出（安全）
    # _safe_console_write(colored_message)

    # 文件写入
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(formatted_message + "\n")
        except Exception as e:
            _safe_console_write(f"{timestamp} [WARN] 日志写入失败: {e}")


def log_multiline(level: str, *messages, log_path: Optional[str] = None):
    """多行日志打印，第一行带时间戳，其余行缩进对齐，可安全写入文件"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix_len = len(timestamp) + 1 + len(f"[{level}] ")
    indent = " " * prefix_len
    log_lines_for_file = []

    color = COLORS.get(level, "") if not _IN_PYCHARM else ""
    reset = RESET_COLOR if color else ""

    for i, msg in enumerate(messages):
        lines = str(msg).split("\n")
        for j, line in enumerate(lines):
            if i == 0 and j == 0:
                formatted_line = f"{timestamp} [{level}] {line}"
            else:
                formatted_line = f"{indent}{line}"
            log_lines_for_file.append(formatted_line)
    #         # 控制台输出带颜色
    #         # _safe_console_write(f"{color}{formatted_line}{reset}")

    # 文件写入
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n".join(log_lines_for_file) + "\n")
        except Exception as e:
            _safe_console_write(f"{timestamp} [WARN] 多行日志写入失败: {e}")
