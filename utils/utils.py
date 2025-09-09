from datetime import datetime


class LogColors:
    INFO = "\033[94m"  # 蓝色
    WARNING = "\033[93m"  # 黄色
    FAIL = "\033[91m"  # 红色
    SUCCESS = "\033[92m"  # 绿色
    RESET = "\033[0m"  # 重置颜色


def log(level: str, message: str):
    """统一彩色日志打印，带白色时间戳"""
    color_map = {
        "INFO": LogColors.INFO,
        "WARNING": LogColors.WARNING,
        "FAIL": LogColors.FAIL,
        "SUCCESS": LogColors.SUCCESS
    }
    color = color_map.get(level, LogColors.INFO)
    # 获取当前时间戳，白色显示
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    WHITE = "\033[97m"
    RESET = LogColors.RESET
    print(f"{WHITE}{timestamp}{RESET} {color}[{level}]{RESET} {message}")
