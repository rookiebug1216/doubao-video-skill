# -*- coding: utf-8 -*-
"""
Step 2：角色提取（extract_characters CLI）真实链路测试。

前置：tests/fixtures/novel_e2e.txt 已写入样本小说。
动作：触发 `python extract_characters.py novel_e2e.txt -o e2e_out --style 3d-realistic`
验证：characters.json 产出 + 结构断言（角色非空、name/desc 完整、style 字段）。
"""
import glob
import json
import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.e2e]

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_EXTRACT = os.path.join(_ROOT, "skills", "doubao-character-ref", "scripts", "extract_characters.py")
_E2E_OUT = os.path.join(_ROOT, "tests", "e2e_out")
_NOVEL = os.path.join(_ROOT, "tests", "fixtures", "novel_e2e.txt")


def _run_extract():
    """触发 extract_characters CLI。"""
    os.makedirs(_E2E_OUT, exist_ok=True)
    cmd = [
        sys.executable, _EXTRACT,
        _NOVEL,
        "-o", _E2E_OUT,
        "--style", "3d-realistic",
    ]
    return subprocess.run(cmd, cwd=_ROOT, capture_output=True, text=True, encoding="utf-8")


@pytest.fixture(scope="module")
def extract_result(e2e_out_dir):
    return _run_extract()


@pytest.fixture(scope="module")
def characters_json_path(extract_result, e2e_out_dir):
    matches = glob.glob(os.path.join(e2e_out_dir, "*.characters.json"))
    assert matches, (
        f"未找到 *.characters.json。CLI 退出码={extract_result.returncode}\n"
        f"STDOUT:\n{extract_result.stdout}\nSTDERR:\n{extract_result.stderr}"
    )
    return max(matches, key=os.path.getmtime)


@pytest.fixture(scope="module")
def characters_data(characters_json_path):
    with open(characters_json_path, encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────────────────────
# 用例
# ──────────────────────────────────────────────────────────────

def test_extract_cli_exit_0(extract_result):
    assert extract_result.returncode == 0, (
        f"extract_characters CLI 退出码非 0：{extract_result.returncode}\n"
        f"STDOUT:\n{extract_result.stdout}\nSTDERR:\n{extract_result.stderr}"
    )


def test_characters_json_exists(characters_json_path):
    assert os.path.exists(characters_json_path)


def test_characters_list_nonempty(characters_data):
    chars = characters_data.get("characters", [])
    assert len(chars) >= 1, f"未提取出任何角色：{characters_data}"


def test_first_character_has_name_and_desc(characters_data):
    chars = characters_data.get("characters", [])
    assert chars, "characters 为空"
    first = chars[0]
    assert first.get("name"), f"首角色 name 为空：{first}"
    assert first.get("desc"), f"首角色 desc 为空：{first}"


def test_style_field_set(characters_data):
    assert characters_data.get("style") == "3d-realistic", (
        f"style 字段应为 3d-realistic，实际为 {characters_data.get('style')}"
    )


def test_project_field_set(characters_data):
    """project 字段从章节标题推断。"""
    project = characters_data.get("project", "")
    assert project, "project 字段为空"


def test_characters_json_serializable(characters_json_path):
    """characters.json 必须可被 json.load 回读（已是 fixture 验证，这里显式断言）。"""
    with open(characters_json_path, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict)
    assert "characters" in data
