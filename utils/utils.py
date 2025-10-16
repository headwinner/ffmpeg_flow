from datetime import datetime
from typing import Optional


def log(level: str, message: str, log_path: Optional[str] = None):
    """统一日志打印，带时间戳，可写入文件"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted_message = f"{timestamp} [{level}] {message}"

    # 控制台打印
    print(formatted_message)

    # 写入日志文件
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(formatted_message + "\n")


def log_multiline(level: str, *messages, log_path: Optional[str] = None):
    """多行日志打印，第一行带时间戳，其余行缩进对齐，可写入文件"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix_len = len(timestamp) + 1 + len(f"[{level}] ")  # 时间戳 + 空格 + 标签
    indent = " " * prefix_len

    log_lines_for_file = []

    for i, msg in enumerate(messages):
        lines = str(msg).split("\n")
        for j, line in enumerate(lines):
            if i == 0 and j == 0:
                formatted_line = f"{timestamp} [{level}] {line}"
            else:
                formatted_line = f"{indent}{line}"

            # 控制台打印
            print(formatted_line)
            log_lines_for_file.append(formatted_line)

    # 写入日志文件
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(log_lines_for_file) + "\n")
