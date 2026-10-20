# -*- coding: utf-8 -*-
"""
Step 4：视频生成 + 无水印下载验证（消耗 1 次额度）。

前置：
  - Step 3 全绿（first_char_ref_path fixture 校验通过）
  - 用户已在专用 profile 登录豆包
动作：触发 `python doubao_gen.py <safe>.shots.json -o e2e_out/videos --char-refs e2e_out/char_refs/char_refs_index.json --only 1 --keep-tab`
验证：1 个 shot_01_*.mp4 + ftyp 头 + 大小 + 无水印链路日志 + 确认次数 == 1（回归重复确认） + 参考图 basename 对应。

⛔ 若 Step 3 失败 → first_char_ref_path fixture 自动 skip 本模块全部用例。
"""
import glob
import os
import re
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.e2e]

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_DOUBAO_GEN = os.path.join(_ROOT, "skills", "doubao-manju-video", "scripts", "doubao_gen.py")
_E2E_OUT = os.path.join(_ROOT, "tests", "e2e_out")


def _find_shots_json():
    matches = glob.glob(os.path.join(_E2E_OUT, "*.shots.json"))
    assert matches, "需先跑 Step 1 产出 *.shots.json"
    return max(matches, key=os.path.getmtime)


def _run_doubao_gen():
    shots_json = _find_shots_json()
    out_dir = os.path.join(_E2E_OUT, "videos")
    os.makedirs(out_dir, exist_ok=True)
    char_refs = os.path.join(_E2E_OUT, "char_refs", "char_refs_index.json")
    assert os.path.exists(char_refs), "需先跑 Step 3 产出 char_refs_index.json"
    cmd = [
        sys.executable, _DOUBAO_GEN,
        shots_json,
        "-o", out_dir,
        "--char-refs", char_refs,
        "--only", "1",
        "--keep-tab",
    ]
    # 渲染超时 15 分钟，加 buffer 取 25 分钟
    return subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True,
                          encoding="utf-8", timeout=1500)


@pytest.fixture(scope="module")
def gen_result(first_char_ref_path):
    """
    依赖 first_char_ref_path fixture 实现中断点：
      Step 3 失败 → first_char_ref_path 已 pytest.skip，本 fixture 不会被执行
      Step 3 成功 → 正常触发 CLI
    """
    return _run_doubao_gen()


@pytest.fixture(scope="module")
def video_path(gen_result, e2e_out_dir):
    """生成的 shot_01_*.mp4 路径。"""
    videos_dir = os.path.join(e2e_out_dir, "videos")
    matches = glob.glob(os.path.join(videos_dir, "shot_01_*.mp4"))
    if not matches:
        # 失败时打详细日志便于排查
        pytest.fail(
            f"未找到 shot_01_*.mp4。CLI 退出码={gen_result.returncode}\n"
            f"STDOUT:\n{gen_result.stdout}\nSTDERR:\n{gen_result.stderr}"
        )
    return max(matches, key=os.path.getmtime)


# ──────────────────────────────────────────────────────────────
# 用例
# ──────────────────────────────────────────────────────────────

def test_doubao_gen_cli_exit_0(gen_result):
    assert gen_result.returncode == 0, (
        f"doubao_gen CLI 退出码非 0：{gen_result.returncode}\n"
        f"STDOUT:\n{gen_result.stdout}\nSTDERR:\n{gen_result.stderr}"
    )


def test_video_file_exists(video_path):
    assert os.path.exists(video_path)


def test_mp4_ftyp_header(video_path):
    """mp4 文件头校验：偏移 4-8 字节为 'ftyp'。"""
    with open(video_path, "rb") as f:
        head = f.read(12)
    assert head[4:8] == b"ftyp", (
        f"视频文件头非 mp4 ftyp（head={head!r}），可能下载到非视频文件"
    )


def test_mp4_size_gt_1mb(video_path):
    """文件大小 > 1MB。无水印 720x1280/10s ≈ 29MB；带水印版仅约 1.25MB。"""
    size = os.path.getsize(video_path)
    assert size > 1024 * 1024, (
        f"视频文件太小（{size // 1024}KB < 1MB），下载失败或被截断"
    )


def test_mp4_size_suggests_unwatermarked(video_path):
    """
    软断言：无水印链路文件应 > 5MB（明显大于带水印版的 1.25MB）。
    小于则打警告但不 fail（保守起见）。
    """
    size = os.path.getsize(video_path)
    if size < 5 * 1024 * 1024:
        print(f"⚠ 视频文件 {size // 1024}KB 小于 5MB 软基线，"
              f"可能下载到带水印版本（无水印版约 29MB）")


def test_unlock_chain_logged(gen_result):
    """日志含无水印链路字样。"""
    log = (gen_result.stdout or "") + (gen_result.stderr or "")
    assert ("无水印直链已解析" in log) or ("已通过无水印链路下载" in log), (
        "CLI 日志未出现无水印链路字样，可能走了带水印降级策略"
    )


def test_confirm_card_auto_confirmed_once(gen_result):
    """
    回归「参数卡重复确认多扣额度」bug：
    日志中「已自动确认」出现次数必须 == 1（每镜只确认 1 次）。
    """
    log = (gen_result.stdout or "") + (gen_result.stderr or "")
    count = log.count("已自动确认")
    assert count == 1, (
        f"「已自动确认」出现 {count} 次，期望恰好 1 次"
        "（>1 次意味着可能重复确认、重复扣额度）"
    )


def test_no_duplicate_text_confirmation(gen_result):
    """
    回归「按钮优先策略下不应发文本确认」：
    日志全文不应出现「确认，开始生成视频」文本消息（按钮优先策略）。
    若出现 <= 1 次且按钮失败兜底可容忍；> 1 次必须 fail。
    """
    log = (gen_result.stdout or "") + (gen_result.stderr or "")
    count = log.count("确认，开始生成视频")
    # 按钮优先策略下应 == 0；按钮失败兜底时最多 1 次
    assert count <= 1, (
        f"「确认，开始生成视频」文本出现 {count} 次，"
        f"按钮优先策略下应 <= 1 次（疑似重复发文本确认）"
    )


def test_char_ref_uploaded_matches_character(gen_result, char_refs_data):
    """
    回归「参考图错配」bug：
    日志中「上传参考图: N 张（basename）」的 basename 必须与
    char_refs_index.json 中 results[0].path 的 basename 一致。
    """
    log = (gen_result.stdout or "") + (gen_result.stderr or "")
    # 匹配 "上传参考图: 1 张（xxx.png）"
    m = re.search(r"上传参考图:\s*\d+\s*张（([^）]+)）", log)
    assert m, f"日志未出现「上传参考图: N 张（…）」字样，可能未触发上传或日志格式变了"
    uploaded_basenames = [b.strip() for b in m.group(1).split(",")]
    # 取 char_refs_index 中首个 ok 项的 basename
    first_ok = next((r for r in char_refs_data["results"] if r.get("status") == "ok"), None)
    assert first_ok, "char_refs_index 中无 status=ok 的项"
    expected_basename = os.path.basename(first_ok["path"])
    assert expected_basename in uploaded_basenames, (
        f"上传的参考图 basename {uploaded_basenames} 不含期望的 {expected_basename}，"
        f"参考图错配"
    )


def test_shot_seconds_in_range(video_path):
    """文件名 shot_01_<secs>s.mp4 中的 secs ∈ [4, 15]。"""
    basename = os.path.basename(video_path)
    m = re.match(r"shot_01_(\d+)s\.mp4", basename)
    assert m, f"视频文件名格式异常：{basename}"
    secs = int(m.group(1))
    assert 4 <= secs <= 15, f"镜头时长 {secs}s 不在 [4, 15] 区间"
