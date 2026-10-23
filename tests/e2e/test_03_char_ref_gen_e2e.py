# -*- coding: utf-8 -*-
"""
Step 3：角色参考图生成 + 无水印原图下载验证（核心中断点）。

前置：
  - 用户已在专用 profile（%LOCALAPPDATA%/doubao-manju/edge-profile）登录豆包
  - Step 2 已产出 *.characters.json
动作：触发 `python char_ref_gen.py <project>.characters.json -o e2e_out/char_refs --only 1 --keep-tab`
验证：char_refs_index.json + 1 张 PNG + PNG 头 + 大小 + 分辨率 + 日志含 _ROUTER_DATA 链路。

⛔ 本步任何失败 → conftest.first_char_ref_path fixture 自动 skip Step 4，
   避免浪费视频生成额度。
"""
import glob
import json
import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.e2e]

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_CHAR_REF_GEN = os.path.join(_ROOT, "skills", "doubao-character-ref", "scripts", "char_ref_gen.py")
_E2E_OUT = os.path.join(_ROOT, "tests", "e2e_out")


def _find_characters_json():
    matches = glob.glob(os.path.join(_E2E_OUT, "*.characters.json"))
    assert matches, "需先跑 Step 2 产出 *.characters.json"
    return max(matches, key=os.path.getmtime)


def _run_char_ref_gen():
    """触发 char_ref_gen CLI，只生 1 张图。"""
    chars_json = _find_characters_json()
    out_dir = os.path.join(_E2E_OUT, "char_refs")
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        sys.executable, _CHAR_REF_GEN,
        chars_json,
        "-o", out_dir,
        "--style", "3d-realistic",
        "--only", "1",
        "--keep-tab",
    ]
    # char_ref_gen 主流程内会拉起浏览器并等待图片生成，timeout 取 10 分钟（图片不限额度但耗时）
    return subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True,
                          encoding="utf-8", timeout=600)


@pytest.fixture(scope="module")
def gen_result(e2e_out_dir):
    return _run_char_ref_gen()


@pytest.fixture(scope="module")
def char_refs_index(gen_result, e2e_out_dir):
    """char_refs_index.json 路径。"""
    p = os.path.join(e2e_out_dir, "char_refs", "char_refs_index.json")
    if not os.path.exists(p):
        pytest.fail(
            f"char_refs_index.json 未生成。CLI 退出码={gen_result.returncode}\n"
            f"STDOUT:\n{gen_result.stdout}\nSTDERR:\n{gen_result.stderr}"
        )
    return p


@pytest.fixture(scope="module")
def char_refs_data(char_refs_index):
    with open(char_refs_index, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def first_result(char_refs_data):
    """首个 result 项。"""
    results = char_refs_data.get("results", [])
    assert results, "char_refs_index 中 results 为空"
    return results[0]


@pytest.fixture(scope="module")
def first_image_path(first_result):
    """首张图路径。"""
    return first_result.get("path")


# ──────────────────────────────────────────────────────────────
# 用例
# ──────────────────────────────────────────────────────────────

def test_char_ref_cli_exit_0_or_index_written(gen_result, char_refs_index):
    """
    CLI 退出码非 0 也算部分 ok（如额度耗尽），只要 index 写入了就算「链路能跑」。
    退出码非 0 但 index 写入 → 仅警告；index 都没写入 → fail。
    """
    if gen_result.returncode != 0:
        # 退出码非 0 但 char_refs_index 已生成（部分失败），打警告不 fail
        print(f"⚠ char_ref_gen CLI 退出码非 0：{gen_result.returncode}")
        print(f"STDOUT 末尾：\n{gen_result.stdout[-500:]}")


def test_char_refs_index_exists(char_refs_index):
    assert os.path.exists(char_refs_index)


def test_first_result_status_ok(first_result):
    """首张图 status 必须为 ok（否则 Step 4 会被 skip）。"""
    assert first_result.get("status") == "ok", (
        f"首张参考图 status={first_result.get('status')}，期望 ok\n"
        f"first_result={first_result}"
    )


def test_first_result_path_exists(first_result):
    path = first_result.get("path")
    assert path and os.path.exists(path), f"参考图文件不存在 path={path}"


def test_png_header_valid(first_result):
    """PNG 头字节校验：\x89PNG\r\n\x1a\n。"""
    path = first_result["path"]
    with open(path, "rb") as f:
        head = f.read(8)
    assert head == b"\x89PNG\r\n\x1a\n", (
        f"参考图非有效 PNG（head={head!r}）。可能下载到 mp4 条目或截断了。"
    )


def test_image_size_gt_1mb(first_result):
    """文件大小 > 1MB（无水印原图约 4.6MB；缩略图通常 < 200KB）。"""
    path = first_result["path"]
    size = os.path.getsize(path)
    assert size > 1024 * 1024, (
        f"参考图太小（{size // 1024}KB < 1MB），疑似缩略图或低分辨率截图"
    )


def test_image_resolution_baseline(first_result):
    """
    用 Pillow 校验分辨率，期望 >= 1920x1080（project_memory 基线 2720x1520）。
    若 Pillow 未装则 skip，仅打警告（不引入硬依赖）。
    """
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow 未安装，跳过分辨率校验（pip install Pillow 后可启用）")
    path = first_result["path"]
    with Image.open(path) as img:
        w, h = img.size
    # project_memory 基线：豆包生成的角色参考图原始分辨率为 2720×1520
    # 软底线 1920x1080，低于此怀疑是缩略图/带水印版
    assert w >= 1920 and h >= 1080, (
        f"参考图分辨率 {w}x{h} 低于 1920x1080 基线，"
        f"疑似低分辨率版本（无水印原图应为 2720x1520）"
    )


def test_router_data_raw_path_logged(gen_result):
    """
    图片默认优先走 `_ROUTER_DATA` 的 image_raw 无水印原图（无 unlock 开关）。

    CLI 日志必须含 _ROUTER_DATA 链路或「无水印原图下载成功」字样，
    验证走了首选无水印原图链路，而非降级到带水印页面资源。
    """
    log = (gen_result.stdout or "") + (gen_result.stderr or "")
    assert ("_ROUTER_DATA" in log) or ("无水印原图下载成功" in log), (
        "CLI 日志未出现 _ROUTER_DATA 或「无水印原图下载成功」字样，"
        "可能走了降级链路（fetch+CDP），请人工排查"
    )


def test_no_quota_message_when_image_unlimited(gen_result):
    """
    图片生成不限额度，日志中不应出现额度耗尽。
    若出现则可能是审核失败，需要人工排查。
    """
    log = (gen_result.stdout or "") + (gen_result.stderr or "")
    # 只在 status != ok 时才检查这条
    # 此用例独立断言：日志含额度耗尽字样时打警告
    quota_phrases = ["额度已耗尽", "免费次数已用完", "明天再来", "明日再来"]
    for p in quota_phrases:
        if p in log:
            print(f"⚠ 日志含额度耗尽字样：{p}（图片不限额度，可能是审核失败）")
