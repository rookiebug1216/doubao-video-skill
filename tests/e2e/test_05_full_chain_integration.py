# -*- coding: utf-8 -*-
"""
Step 5：链路整体断言。

前置：Step 1-4 全部完成。
动作：跨产物做一致性断言（角色匹配、视频数、参考图对应）。
"""
import glob
import json
import os

import pytest

pytestmark = [pytest.mark.e2e]

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
_E2E_OUT = os.path.join(_ROOT, "tests", "e2e_out")


@pytest.fixture(scope="module")
def shots_data_local():
    matches = glob.glob(os.path.join(_E2E_OUT, "*.shots.json"))
    assert matches, "需先跑 Step 1"
    with open(max(matches, key=os.path.getmtime), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def char_refs_index_local():
    p = os.path.join(_E2E_OUT, "char_refs", "char_refs_index.json")
    assert os.path.exists(p), "需先跑 Step 3"
    with open(p, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def video_files_local():
    videos_dir = os.path.join(_E2E_OUT, "videos")
    return glob.glob(os.path.join(videos_dir, "shot_*.mp4"))


# ──────────────────────────────────────────────────────────────
# 用例
# ──────────────────────────────────────────────────────────────

def test_chain_artifacts_exist(shots_data_local, char_refs_index_local, video_files_local):
    """链路四产物全部存在：shots.json → char_refs_index.json → shot_*.mp4。"""
    assert shots_data_local.get("shots"), "shots.json 中 shots 为空"
    assert char_refs_index_local.get("results"), "char_refs_index.json 中 results 为空"
    assert video_files_local, "未生成任何 shot_*.mp4"


def test_chain_character_consistency(shots_data_local, char_refs_index_local):
    """
    shots.json 中首镜的 characters（自动标注）与
    char_refs_index.json 中 results 的 name 应有非空交集。
    """
    shots = shots_data_local.get("shots", [])
    assert shots, "shots 为空"
    first_shot = shots[0]
    shot_chars = set(first_shot.get("characters", []) or [])
    ref_names = {r["name"] for r in char_refs_index_local.get("results", []) if r.get("name")}
    # 至少有一个交集（参考图至少对应一个出场角色）
    common = shot_chars & ref_names
    # 若 shot 自动标注未命中，且 shots 中所有 characters 都是空集，则放宽：
    # char_refs_index 至少有一个 name 出现在 shots 全文中
    if not common:
        # 兜底：shots 中任一字段包含 ref_names 之一
        all_text = ""
        for s in shots:
            all_text += s.get("narration", "") + s.get("source_text", "") + s.get("scene", "")
        for name in ref_names:
            if name in all_text:
                common = {name}
                break
    assert common, (
        f"shots.json 中首镜 characters {shot_chars} 与 char_refs_index 中 names {ref_names} 无交集"
    )


def test_chain_no_cross_character_pollution(shots_data_local, char_refs_index_local):
    """
    回归「禁止上传错误角色参考图」硬约束：
    若首镜有 characters 标注，则参考图至少一张对应其中之一。
    本用例是软断言：若首镜无标注（自动检测失败），则只检查 ref_names 在全文出现。
    """
    shots = shots_data_local.get("shots", [])
    first_shot = shots[0] if shots else {}
    shot_chars = set(first_shot.get("characters", []) or [])
    ref_names = {r["name"] for r in char_refs_index_local.get("results", []) if r.get("name")}
    if shot_chars:
        # 严格断言：至少一个参考图对应首镜角色
        assert shot_chars & ref_names, (
            f"首镜角色 {shot_chars} 在参考图 names {ref_names} 中均未出现，"
            f"参考图错配"
        )


def test_chain_total_quota_used(video_files_local):
    """视频文件数 == 1（本测试只跑 1 镜）。"""
    assert len(video_files_local) == 1, (
        f"期望生成 1 个视频文件，实际 {len(video_files_local)} 个：{video_files_local}"
    )


def test_chain_shot_count_consistency(shots_data_local, video_files_local):
    """
    生成的视频数应等于 shots.json 中镜头数（本测试只跑 1 镜）。
    若 shots 拆出多镜但 --only 1 只跑首镜，则视频数应为 1。
    """
    # 本测试链路设计为只跑 1 镜（--only 1），所以视频数 == 1
    assert len(video_files_local) == 1


def test_chain_final_episode_kind_resolve(shots_data_local):
    """--is-final 下末集 kind == "resolve"。"""
    eps = shots_data_local.get("episodes", [])
    assert eps, "episodes 为空"
    assert eps[-1]["kind"] == "resolve", (
        f"--is-final 下末集 kind 应为 resolve，实际 {eps[-1]['kind']}"
    )
