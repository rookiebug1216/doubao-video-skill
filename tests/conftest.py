# -*- coding: utf-8 -*-
"""
测试公共 fixture。

职责：
  - 把项目根加入 sys.path，使 `from shared import ...` 与各 skill 脚本一致
  - 提供 e2e 产出目录与各阶段产物路径的 session fixture
  - 实现 Step 3 中断点：图片无水印下载失败则 skip Step 4，避免浪费视频额度
  - 默认 skip e2e/manual/gate 标记的用例，需 `pytest -m e2e` 显式启用
"""

import glob
import json
import os
import sys

import pytest

# ── 项目根加入 sys.path（与 skill 脚本头部一致） ──
_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

E2E_OUT = os.path.join(_ROOT, "tests", "e2e_out")
FIXTURES = os.path.join(_ROOT, "tests", "fixtures")


# ──────────────────────────────────────────────────────────────
# 路径 fixture
# ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def e2e_out_dir():
    """e2e 产出根目录（不存在则创建）。"""
    os.makedirs(E2E_OUT, exist_ok=True)
    return E2E_OUT


@pytest.fixture(scope="session")
def fixtures_dir():
    """样本小说目录。"""
    return FIXTURES


@pytest.fixture(scope="session")
def novel_e2e_path(fixtures_dir):
    """样本小说路径。"""
    p = os.path.join(fixtures_dir, "novel_e2e.txt")
    assert os.path.exists(p), f"样本小说不存在: {p}（请先在 tests/fixtures/novel_e2e.txt 写入微小说）"
    return p


def _latest_glob(pattern):
    """在 e2e_out 下按 mtime 取最新匹配。"""
    matches = glob.glob(os.path.join(E2E_OUT, pattern))
    return max(matches, key=os.path.getmtime) if matches else None


@pytest.fixture(scope="session")
def shots_json_path():
    """Step 1 产出的 shots.json（按 mtime 取最新）。"""
    p = _latest_glob("*.shots.json")
    assert p, "需先跑 test_01_split_novel_e2e 产出 *.shots.json"
    return p


@pytest.fixture(scope="session")
def shots_data(shots_json_path):
    """shots.json 解析后的 dict。"""
    with open(shots_json_path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def characters_json_path():
    """Step 2 产出的 characters.json。"""
    p = _latest_glob("*.characters.json")
    assert p, "需先跑 test_02_extract_characters_e2e 产出 *.characters.json"
    return p


@pytest.fixture(scope="session")
def characters_data(characters_json_path):
    with open(characters_json_path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def char_refs_index_path():
    """Step 3 产出的 char_refs_index.json。"""
    p = os.path.join(E2E_OUT, "char_refs", "char_refs_index.json")
    assert os.path.exists(p), "需先跑 test_03_char_ref_gen_e2e 产出 char_refs_index.json"
    return p


@pytest.fixture(scope="session")
def char_refs_data(char_refs_index_path):
    with open(char_refs_index_path, encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────────────────────
# Step 3 中断点：若第一张图未成功下载或不是有效 PNG → skip Step 4
# ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def first_char_ref_path(char_refs_data):
    """
    Step 3 中断点 fixture：返回第一张成功生成的角色参考图路径。

    任一失败条件命中即 pytest.skip，避免浪费视频生成额度：
      - results 为空或首个 status != "ok"
      - path 字段缺失或文件不存在
      - PNG 头字节校验失败
      - 文件大小 < 1MB（怀疑是缩略图）
    """
    results = char_refs_data.get("results", [])
    if not results:
        pytest.skip("Step 3 失败：char_refs_index 无 results，跳过视频生成")
    first = results[0]
    if first.get("status") != "ok":
        pytest.skip(f"Step 3 失败：首张参考图 status={first.get('status')}，跳过视频生成")
    path = first.get("path")
    if not path or not os.path.exists(path):
        pytest.skip(f"Step 3 失败：参考图文件不存在 path={path}，跳过视频生成")
    # PNG 头校验（豆包生成的角色参考图为 PNG）
    with open(path, "rb") as f:
        head = f.read(8)
    if head != b"\x89PNG\r\n\x1a\n":
        pytest.skip(f"Step 3 失败：参考图非有效 PNG（head={head!r}），跳过视频生成")
    # 文件大小校验：无水印原图约 4.6MB；缩略图通常 < 200KB
    size = os.path.getsize(path)
    if size < 1024 * 1024:
        pytest.skip(f"Step 3 失败：参考图太小（{size // 1024}KB < 1MB），疑似缩略图，跳过视频生成")
    return path


# ──────────────────────────────────────────────────────────────
# 默认 skip e2e/manual/gate 标记的用例
# ──────────────────────────────────────────────────────────────

def pytest_collection_modifyitems(config, items):
    """
    默认不收集 e2e/manual/gate 标记的用例（避免误触真实生成）；
    只有 `pytest -m e2e|manual|gate` 显式选了对应标记，才放行。
    """
    skip_markers = {"e2e", "manual", "gate"}
    mark_opt = config.getoption("-m") or ""
    selected = set(mark_opt.split()) & skip_markers
    if selected:
        return  # 用户显式选了某个 marker，全跑
    skip = pytest.mark.skip(reason="需 -m e2e / -m manual / -m gate 显式启用")
    for item in items:
        if {m.name for m in item.iter_markers()} & skip_markers:
            item.add_marker(skip)
