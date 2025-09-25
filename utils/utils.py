from datetime import datetime
from typing import Optional


class LogColors:
    INFO = "\033[94m"  # 蓝色
    WARNING = "\033[93m"  # 黄色
    FAIL = "\033[91m"  # 红色
    SUCCESS = "\033[92m"  # 绿色
    RESET = "\033[0m"  # 重置颜色


class LogColors:
    INFO = "\033[94m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    SUCCESS = "\033[92m"
    RESET = "\033[0m"


def log(level: str, message: str, log_path: Optional[str] = None):
    """统一彩色日志打印，带白色时间戳，可写入文件"""
    color_map = {
        "INFO": LogColors.INFO,
        "WARNING": LogColors.WARNING,
        "FAIL": LogColors.FAIL,
        "SUCCESS": LogColors.SUCCESS
    }
    color = color_map.get(level, LogColors.INFO)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    WHITE = "\033[97m"
    RESET = LogColors.RESET
    formatted_message = f"{WHITE}{timestamp}{RESET} {color}[{level}]{RESET} {message}"

    # 控制台打印
    print(formatted_message)

    # 写入日志文件（无颜色码）
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{timestamp} [{level}] {message}\n")


def log_multiline(level: str, *messages, log_path: Optional[str] = None):
    """多行日志打印，第一行带时间戳，其余行缩进对齐，可写入文件"""
    color_map = {
        "INFO": LogColors.INFO,
        "WARNING": LogColors.WARNING,
        "FAIL": LogColors.FAIL,
        "SUCCESS": LogColors.SUCCESS
    }
    color = color_map.get(level, LogColors.INFO)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    WHITE = "\033[97m"
    RESET = LogColors.RESET
    prefix_len = len(timestamp) + 1 + len(f"[{level}] ")  # 时间戳 + 空格 + 标签
    indent = " " * prefix_len

    # 生成要写入文件的完整文本
    log_lines_for_file = []

    for i, msg in enumerate(messages):
        lines = str(msg).split("\n")
        for j, line in enumerate(lines):
            if i == 0 and j == 0:
                formatted_line = f"{WHITE}{timestamp}{RESET} {color}[{level}]{RESET} {line}"
                file_line = f"{timestamp} [{level}] {line}"
            else:
                formatted_line = f"{indent}{line}"
                file_line = f"{' ' * prefix_len}{line}"

            # 控制台打印
            print(formatted_line)
            log_lines_for_file.append(file_line)

    # 写入日志文件
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(log_lines_for_file) + "\n")
