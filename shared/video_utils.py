# -*- coding: utf-8 -*-
"""
视频工具集 —— ffmpeg 末帧提取等视频处理辅助函数。

用途：
  提供视频时长探测与末帧提取能力，可用作排障或后续扩展的辅助工具。
  主流程不依赖本模块，未安装 ffmpeg 时所有函数返回 None。

依赖：系统需安装 ffmpeg 并加入 PATH。未安装时所有函数返回 None，不影响主流程。
"""

import os
import shutil
import subprocess
from .cdp_browser import log


def ffmpeg_available() -> bool:
    """检查 ffmpeg 是否可用。"""
    return shutil.which("ffmpeg") is not None


def extract_last_frame(video_path: str, output_path: str,
                       seek_seconds: float = -1.0) -> str | None:
    """
    从视频提取末帧（或指定秒数的帧）保存为 PNG。

    video_path: 输入视频路径
    output_path: 输出图片路径（.png）
    seek_seconds: 提取哪一秒的帧，-1 表示最后一帧

    返回输出路径，失败返回 None。
    """
    if not ffmpeg_available():
        log("  · ffmpeg 不可用，跳过末帧提取（需安装 ffmpeg 并加入 PATH）")
        return None
    if not os.path.exists(video_path):
        log(f"  · 视频文件不存在: {video_path}")
        return None

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # 用 -sseof -0.1 定位到倒数 0.1 秒，取一帧（避免最后一帧是黑屏/转场）
    # -frames:v 1 只取一帧，-y 覆盖输出
    seek = "-0.1" if seek_seconds < 0 else str(seek_seconds)
    cmd = [
        "ffmpeg", "-y",
        "-sseof", seek,
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and os.path.exists(output_path) \
                and os.path.getsize(output_path) > 1024:
            size = os.path.getsize(output_path) // 1024
            log(f"  · 末帧提取成功: {output_path} ({size} KB)")
            return output_path
        log(f"  · ffmpeg 末帧提取失败: {result.stderr[-200:] if result.stderr else 'unknown'}")
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"  · ffmpeg 调用异常: {e}")
    return None


def get_video_duration(video_path: str) -> float | None:
    """用 ffprobe 获取视频时长（秒）。失败返回 None。"""
    if not shutil.which("ffprobe"):
        return None
    if not os.path.exists(video_path):
        return None
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return None
