# -*- coding: utf-8 -*-
"""
v3 关键帧图相关单元测试（不耗额度）。

回归点：
  - Shot.end_frame_prompt 字段存在且默认空字符串
  - build_end_frame_prompt 结构正确（含 exit_state、角色名、构图字段）
  - build_end_frame_prompt 与 build_frame_prompt 的区别（entry vs exit）
  - _last_clause_quote_aware 取末句（无引号/弯引号/直引号）
  - from_novel 端到端：每镜 end_frame_prompt 已写入
  - 第一镜同时有 frame_prompt（首帧）和 end_frame_prompt（尾帧）
  - _collect_ref_images 收集角色图+道具图路径
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SCRIPTS_DIR = os.path.join(_ROOT, "skills", "doubao-manju-video", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import split_novel as sn
from split_novel import (
    Shot, ShotList, build_end_frame_prompt, build_frame_prompt,
    _last_clause_quote_aware, from_novel, annotate_characters,
)

# doubao_gen 的 _collect_ref_images
sys.path.insert(0, os.path.join(_ROOT, "skills", "doubao-manju-video", "scripts"))
import doubao_gen as dg


# ── Shot 字段 ──

def test_shot_has_end_frame_prompt_field():
    s = Shot(index=1, seconds=5, beat="action", beat_label="动作",
             scene="庭院", action="挥剑", camera="跟拍",
             narration="他挥剑斩出。", source_text="他挥剑斩出。")
    assert hasattr(s, "end_frame_prompt")
    assert s.end_frame_prompt == ""


# ── build_end_frame_prompt ──

def test_end_frame_prompt_contains_exit_state():
    p = build_end_frame_prompt(
        scene="悬崖边", narration="他纵身跃下。", characters=["叶青云"],
        exit_state="身影消失在云雾中", style="CG写实3D", ratio_desc="9:16竖屏",
    )
    assert "尾帧定格画面" in p
    assert "结束状态：身影消失在云雾中" in p
    assert "叶青云" in p  # 直呼角色名


def test_end_frame_prompt_no_entry_state():
    """end_frame_prompt 不应包含 entry_state（那是 frame_prompt 的职责）。"""
    p = build_end_frame_prompt(
        scene="庭院", narration="他收剑入鞘。", characters=["叶青云"],
        exit_state="收剑而立", style="CG写实3D", ratio_desc="9:16竖屏",
    )
    assert "承接上一镜" not in p
    assert "结束状态：收剑而立" in p


def test_end_frame_vs_frame_prompt_difference():
    """同一镜头的 frame_prompt 用 entry_state，end_frame_prompt 用 exit_state。"""
    common = dict(scene="庭院", narration="他收剑入鞘。", characters=["叶青云"],
                  style="CG写实3D", ratio_desc="9:16竖屏")
    fp = build_frame_prompt(entry_state="承接上一镜末帧", camera="固定", **common)
    ep = build_end_frame_prompt(exit_state="收剑而立", **common)
    assert "承接上一镜末帧" in fp
    assert "承接上一镜末帧" not in ep
    assert "收剑而立" in ep
    assert "收剑而立" not in fp
    assert "尾帧定格画面" in ep
    assert "定格关键帧（t=0.00s）" in fp


def test_end_frame_prompt_with_composition_fields():
    p = build_end_frame_prompt(
        scene="战场", narration="他倒下了。", characters=["叶青云"],
        exit_state="倒在血泊中", style="CG写实3D", ratio_desc="9:16竖屏",
        focal_length="85mm", camera_position="仰视", composition="居中",
        gaze_target="望向天空", focus_point="人物面部", stability="手持轻微晃动",
    )
    assert "焦距85mm" in p
    assert "机位仰视" in p
    assert "构图居中" in p
    assert "视线望向天空" in p
    assert "焦点人物面部" in p
    assert "稳定性手持轻微晃动" in p


def test_end_frame_prompt_no_characters():
    """无角色镜头必须输出明确空镜措辞（2026-10-24 bug12：'主体：画面主体'
    空占位会让豆包自行补人物，实测镜01 空巷生成了红衣女子）。"""
    p = build_end_frame_prompt(
        scene="空荡的大殿", narration="寂静无声。", characters=[],
        exit_state="空无一人", style="CG写实3D", ratio_desc="9:16竖屏",
    )
    assert "画面主体" not in p, "不得再输出'画面主体'空占位"
    assert "空镜" in p and "无任何人物" in p
    assert "不要出现人脸" in p


# ── _last_clause_quote_aware ──

def test_last_clause_no_quotes():
    assert _last_clause_quote_aware("他走进门。他看到了她。") == "他看到了她。"
    assert _last_clause_quote_aware("第一句。第二句。第三句。") == "第三句。"
    # 只有一句
    assert _last_clause_quote_aware("只有一句。") == "只有一句。"


def test_last_clause_curly_quotes():
    text = "他说：“你好。”然后离开了。"
    result = _last_clause_quote_aware(text)
    # 引号对结束后是"然后离开了。"
    assert "然后离开了" in result


def test_last_clause_straight_quotes():
    text = '他说："你好。"然后离开了。'
    result = _last_clause_quote_aware(text)
    assert "然后离开了" in result


def test_last_clause_quote_at_end():
    text = "他缓缓说道：“一切都结束了。”"
    result = _last_clause_quote_aware(text)
    assert "一切都结束了" in result


# ── from_novel 端到端 ──

def test_from_novel_writes_end_frame_prompt():
    text = (
        "叶青云站在废墟中央。他俯身拾起师父的断剑，剑身残留血迹。"
        "他单膝跪地，将断剑插入地面作为墓碑。"
    )
    sl = from_novel(text, title="测试", style_key="3d-realistic", ratio="9:16", model="Seedance 2.0")
    annotate_characters(sl, None)  # 触发 _finalize_prompts，生成 prompt/frame_prompt/end_frame_prompt
    assert len(sl.shots) >= 1
    for s in sl.shots:
        assert s.end_frame_prompt, f"镜 {s.index} end_frame_prompt 为空"
        assert "尾帧定格画面" in s.end_frame_prompt


def test_first_shot_has_both_frame_and_end_frame():
    text = "叶青云挥剑斩出。剑光闪过，敌人倒下。"
    sl = from_novel(text, title="测试", style_key="3d-realistic", ratio="9:16", model="Seedance 2.0")
    annotate_characters(sl, None)
    first = sl.shots[0]
    assert first.frame_prompt  # 首帧图提示词
    assert first.end_frame_prompt  # 尾帧图提示词
    assert first.frame_prompt != first.end_frame_prompt


# ── _collect_ref_images ──

def test_collect_ref_images_roles_and_props(tmp_path):
    # 创建临时文件
    char1 = tmp_path / "叶青云.png"
    char1.write_text("x")
    prop1 = tmp_path / "青锋剑.png"
    prop1.write_text("x")

    char_map = {"叶青云": str(char1)}
    prop_map = {"青锋剑": str(prop1)}
    shot = {"characters": ["叶青云"], "props": ["青锋剑"]}

    refs = dg._collect_ref_images(shot, char_map, prop_map)
    assert len(refs) == 2
    assert str(char1) in refs
    assert str(prop1) in refs


def test_collect_ref_images_missing_files():
    char_map = {"叶青云": "/nonexistent/char.png"}
    prop_map = {"青锋剑": "/nonexistent/prop.png"}
    shot = {"characters": ["叶青云"], "props": ["青锋剑"]}
    refs = dg._collect_ref_images(shot, char_map, prop_map)
    assert refs == []  # 文件不存在，全部跳过


def test_collect_ref_images_empty():
    refs = dg._collect_ref_images({}, {}, {})
    assert refs == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
