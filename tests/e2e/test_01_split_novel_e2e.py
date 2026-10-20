# -*- coding: utf-8 -*-
"""
Step 1：拆解（split_novel CLI）真实链路测试。

前置：tests/fixtures/novel_e2e.txt 已写入样本小说。
动作：触发 `python split_novel.py novel_e2e.txt -o e2e_out --is-final`
验证：shots.json + storyboard.md + prompts.txt + voiceover.md 四产物 + 结构断言。
"""
import glob
import json
import os
import re
import subprocess
import sys

import pytest

# 标记为 e2e：默认 skip，需 `pytest -m e2e` 启用
pytestmark = [pytest.mark.e2e]

# 项目根（与 conftest 一致）
_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_SPLIT_NOVEL = os.path.join(_ROOT, "skills", "doubao-manju-video", "scripts", "split_novel.py")
_E2E_OUT = os.path.join(_ROOT, "tests", "e2e_out")
_NOVEL = os.path.join(_ROOT, "tests", "fixtures", "novel_e2e.txt")


def _run_split_novel():
    """触发 split_novel CLI。返回 CompletedProcess。"""
    os.makedirs(_E2E_OUT, exist_ok=True)
    cmd = [
        sys.executable, _SPLIT_NOVEL,
        _NOVEL,
        "-o", _E2E_OUT,
        "--style", "3d-realistic",
        "--ratio", "9:16",
        "--is-final",
    ]
    # 在项目根执行，使 skill 脚本的 sys.path 注入逻辑生效
    return subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True, encoding="utf-8")


@pytest.fixture(scope="module")
def split_result(e2e_out_dir):
    """module 级 fixture：本模块所有用例共享一次 CLI 调用。"""
    res = _run_split_novel()
    return res


@pytest.fixture(scope="module")
def shots_json(split_result, e2e_out_dir):
    """在 e2e_out 下找最新 *.shots.json。"""
    matches = glob.glob(os.path.join(e2e_out_dir, "*.shots.json"))
    assert matches, (
        f"未找到 *.shots.json。CLI 退出码={split_result.returncode}\n"
        f"STDOUT:\n{split_result.stdout}\nSTDERR:\n{split_result.stderr}"
    )
    return max(matches, key=os.path.getmtime)


@pytest.fixture(scope="module")
def shots_data(shots_json):
    with open(shots_json, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def storyboard_md(e2e_out_dir, split_result):
    matches = glob.glob(os.path.join(e2e_out_dir, "*.storyboard.md"))
    assert matches, (
        f"未找到 *.storyboard.md。CLI 退出码={split_result.returncode}\n"
        f"STDOUT:\n{split_result.stdout}"
    )
    return max(matches, key=os.path.getmtime)


@pytest.fixture(scope="module")
def prompts_txt(e2e_out_dir, split_result):
    matches = glob.glob(os.path.join(e2e_out_dir, "*.prompts.txt"))
    assert matches, (
        f"未找到 *.prompts.txt。CLI 退出码={split_result.returncode}\n"
        f"STDOUT:\n{split_result.stdout}"
    )
    return max(matches, key=os.path.getmtime)


@pytest.fixture(scope="module")
def voiceover_md(e2e_out_dir, split_result):
    matches = glob.glob(os.path.join(e2e_out_dir, "*.voiceover.md"))
    assert matches, (
        f"未找到 *.voiceover.md。CLI 退出码={split_result.returncode}\n"
        f"STDOUT:\n{split_result.stdout}"
    )
    return max(matches, key=os.path.getmtime)


# ──────────────────────────────────────────────────────────────
# 用例
# ──────────────────────────────────────────────────────────────

def test_split_cli_exit_0(split_result):
    """CLI 退出码必须为 0。"""
    assert split_result.returncode == 0, (
        f"split_novel CLI 退出码非 0：{split_result.returncode}\n"
        f"STDOUT:\n{split_result.stdout}\nSTDERR:\n{split_result.stderr}"
    )


def test_shots_json_exists(shots_json):
    """shots.json 文件存在。"""
    assert os.path.exists(shots_json)


def test_shots_count_ge_1(shots_data):
    """至少拆出 1 个镜头。"""
    shots = shots_data.get("shots", [])
    assert len(shots) >= 1, f"未拆出任何镜头：{shots_data}"


def test_each_shot_seconds_le_15(shots_data):
    """所有镜头时长 ≤ 15s（豆包免费版硬限制）。"""
    shots = shots_data.get("shots", [])
    assert shots, "shots 数组为空"
    for s in shots:
        assert s["seconds"] <= 15, f"镜头 {s.get('index')} 时长 {s['seconds']}s 超过 15s 上限"


def test_shot_prompt_nonempty(shots_data):
    """每个镜头的 prompt 非空且含「时长N秒」字样。"""
    for s in shots_data["shots"]:
        prompt = s.get("prompt", "")
        assert prompt.strip(), f"镜头 {s.get('index')} prompt 为空"
        assert re.search(r"时长\d+秒", prompt), (
            f"镜头 {s.get('index')} prompt 缺「时长N秒」字样：{prompt[:120]}"
        )


def test_episode_count_ge_1(shots_data):
    """episode_count ≥ 1。"""
    assert shots_data.get("episode_count", 0) >= 1


def test_first_episode_shots_le_5(shots_data):
    """第一集镜头数 ≤ 5（对齐豆包每日额度）。"""
    eps = shots_data.get("episodes", [])
    assert eps, "episodes 为空"
    assert eps[0]["shot_count"] <= 5, f"第一集镜头数 {eps[0]['shot_count']} 超过 5 镜上限"


def test_markdown_storyboard_has_shot_title(storyboard_md):
    """storyboard.md 含「## 镜头 01」标题。"""
    with open(storyboard_md, encoding="utf-8") as f:
        content = f.read()
    assert re.search(r"##\s*镜头\s*0?1", content), "storyboard.md 未找到「## 镜头 01」标题"


def test_prompts_txt_has_separator(prompts_txt):
    """prompts.txt 含「===== 镜头 01」分隔符。"""
    with open(prompts_txt, encoding="utf-8") as f:
        content = f.read()
    assert re.search(r"=====\s*镜头\s*0?1", content), "prompts.txt 未找到「===== 镜头 01」分隔符"


def test_voiceover_has_srt(voiceover_md):
    """voiceover.md 含 srt 代码块。"""
    with open(voiceover_md, encoding="utf-8") as f:
        content = f.read()
    assert "```srt" in content, "voiceover.md 未找到 srt 代码块"


def test_voiceover_srt_timestamps(voiceover_md):
    """srt 时间戳格式正确（HH:MM:SS,mmm）。"""
    with open(voiceover_md, encoding="utf-8") as f:
        content = f.read()
    # 至少匹配一处 00:00:00,000 格式
    assert re.search(r"\d{2}:\d{2}:\d{2},\d{3}", content), (
        "voiceover.md 未找到有效 srt 时间戳"
    )


def test_final_chapter_kind_resolve(shots_data):
    """--is-final 下末集 kind == "resolve"。"""
    eps = shots_data.get("episodes", [])
    assert eps, "episodes 为空"
    last = eps[-1]
    assert last["kind"] == "resolve", (
        f"--is-final 下末集 kind 应为 resolve，实际为 {last['kind']}"
    )
