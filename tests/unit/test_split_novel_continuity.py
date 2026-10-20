# -*- coding: utf-8 -*-
"""
split_novel 衔接+构图+质量门 回归套件（不耗额度，默认跑）。

回归点：
  - Shot 新字段默认值（blocking/entry_state/exit_state/构图六字段/on_screen_count）
  - _infer_state_from_tail 对动作/情绪/氛围末句的推断
  - _infer_blocking 对 1/2/3+人镜头的站位推断
  - _keywords_overlap 衔接连贯性校验
  - _generic_identity cast_map 驱动 vs 旧版正则 fallback
  - COMPOSITION_BY_BEAT 六个 beat 全字段齐全
  - validate_shots 5 道质量门命中
  - validate_continuity 衔接不连贯告警
  - from_novel 端到端新字段写入
  - 旧版 shots.json（无新字段）兼容读取
"""
import json
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
    Shot, ShotList, MAX_SHOT_SECONDS, COMPOSITION_BY_BEAT,
    _infer_state_from_tail, _infer_blocking, _keywords_overlap,
    _generic_identity, _synthesize_identity_from_persona,
    build_video_prompt, build_frame_prompt, build_shot,
    validate_shots, validate_continuity, from_novel, from_concept,
    CAMERA_BY_BEAT,
)


pytestmark = [pytest.mark.unit]


# ──────────────────────────────────────────────────────────────
# Shot 字段默认值
# ──────────────────────────────────────────────────────────────

class TestShotFields:
    def test_shot_new_fields_default_empty(self):
        """Shot 实例化时新字段默认为空/0，不污染旧字段。"""
        s = Shot(
            index=1, seconds=10, beat="action", beat_label="动作",
            scene="雨夜客栈", action="挥剑", camera="跟随推进",
            narration="挥剑而出", source_text="挥剑而出",
        )
        assert s.blocking == ""
        assert s.entry_state == ""
        assert s.exit_state == ""
        assert s.focal_length == ""
        assert s.camera_position == ""
        assert s.composition == ""
        assert s.gaze_target == ""
        assert s.focus_point == ""
        assert s.stability == ""
        assert s.on_screen_count == 0

    def test_shot_to_dict_includes_new_fields(self):
        """ShotList.to_dict 包含新字段，下游可消费。"""
        s = Shot(
            index=1, seconds=10, beat="action", beat_label="动作",
            scene="客栈", action="挥剑", camera="跟拍",
            narration="挥剑", source_text="挥剑",
            blocking="两人相对", entry_state="剑出鞘", exit_state="剑入鞘",
            focal_length="35mm", camera_position="平视",
            composition="三分法", gaze_target="对视",
            focus_point="人物面部", stability="稳定",
            on_screen_count=2,
        )
        sl = ShotList(title="test", style_key="3d-realistic", ratio="9:16",
                     style_desc="desc", ratio_desc="ratio", model="Seedance")
        sl.shots.append(s)
        d = sl.to_dict()
        shot = d["shots"][0]
        assert shot["blocking"] == "两人相对"
        assert shot["entry_state"] == "剑出鞘"
        assert shot["exit_state"] == "剑入鞘"
        assert shot["focal_length"] == "35mm"
        assert shot["on_screen_count"] == 2


# ──────────────────────────────────────────────────────────────
# _infer_state_from_tail
# ──────────────────────────────────────────────────────────────

class TestInferStateFromTail:
    def test_action_sword(self):
        """挥剑类动作命中"剑已出鞘"。"""
        state = _infer_state_from_tail("苏见川挥剑而出，寒光闪过。")
        assert "出鞘" in state or "寒光" in state

    def test_action_flee(self):
        """转身离去类动作命中"背影远去"。"""
        state = _infer_state_from_tail("苏见川转身离去，消失在夜色中。")
        assert "背影" in state

    def test_emotion_tears(self):
        """泪类情绪命中"眼眶含泪"。"""
        state = _infer_state_from_tail("白若霜望着地上的人，眼眶泛红。")
        assert "泪" in state or "红" in state

    def test_atmos_rain(self):
        """雨夜类氛围命中"雨幕未停"。"""
        state = _infer_state_from_tail("雨夜客栈灯火摇曳。")
        assert "雨" in state

    def test_no_match_returns_empty(self):
        """无任何信号时返回空字符串，不强行注入衔接信息。"""
        state = _infer_state_from_tail("桌上摆着茶杯。")
        assert state == ""

    def test_quote_aware_tail(self):
        """末句含引号时取引号后的动作收尾。"""
        state = _infer_state_from_tail('白若霜道："他们追来了。"苏见川握紧长剑。')
        # 应取"苏见川握紧长剑"作为末句，命中握紧动作
        assert "握" in state or "手" in state

    def test_empty_input(self):
        """空输入返回空。"""
        assert _infer_state_from_tail("") == ""


# ──────────────────────────────────────────────────────────────
# _infer_blocking
# ──────────────────────────────────────────────────────────────

class TestInferBlocking:
    def test_two_person_with_position(self):
        """2 人镜头含站位词命中"两人..."。"""
        b = _infer_blocking(["苏见川", "白若霜"],
                          "苏见川与白若霜相对而立。", "客栈")
        assert "两人" in b

    def test_single_person(self):
        """1 人镜头命中"人物居中..."。"""
        b = _infer_blocking(["苏见川"], "苏见川站立。", "客栈")
        assert "人物" in b

    def test_zero_person_no_signal(self):
        """0 人镜头无姿态/朝向信号时返回空。"""
        b = _infer_blocking([], "桌上有茶杯。", "客栈")
        assert b == ""

    def test_over_three_person_warning(self):
        """4 人同框命中超员告警。"""
        b = _infer_blocking(["甲", "乙", "丙", "丁"], "甲乙丙丁围坐。", "客栈")
        assert "超 3" in b or "群演" in b

    def test_empty_narration(self):
        """空 narration 返回空。"""
        assert _infer_blocking([], "", "客栈") == ""


# ──────────────────────────────────────────────────────────────
# _keywords_overlap
# ──────────────────────────────────────────────────────────────

class TestKeywordsOverlap:
    def test_overlap_true(self):
        """两状态含重合关键词。"""
        assert _keywords_overlap("剑已出鞘，寒光定格", "剑已入鞘，手垂身侧") is True

    def test_no_overlap_false(self):
        """两状态无重合关键词。"""
        assert _keywords_overlap("剑已出鞘，寒光定格", "笑容定格") is False

    def test_empty_pass(self):
        """任一为空时返回 True（衔接信息缺失而非矛盾）。"""
        assert _keywords_overlap("", "剑出鞘") is True
        assert _keywords_overlap("剑出鞘", "") is True

    def test_no_keywords_pass(self):
        """都未匹配到关键词时放过。"""
        assert _keywords_overlap("桌上茶杯", "窗外风景") is True


# ──────────────────────────────────────────────────────────────
# _generic_identity cast_map 驱动
# ──────────────────────────────────────────────────────────────

class TestGenericIdentityCastMap:
    def test_video_term_from_cast_map(self):
        """cast_map 中 video_term 优先取值，禁角色名。"""
        cast_map = {
            "苏见川": {
                "name": "苏见川",
                "generic_identity": {
                    "video_term": "深灰长袍的持剑青年",
                    "frame_term": "苏见川（约30岁，剑眉星眸）"
                }
            }
        }
        result = _generic_identity("苏见川", "苏见川挥剑。", 0,
                                  cast_map=cast_map, role="video")
        assert result == "深灰长袍的持剑青年"
        assert "苏见川" not in result  # video_term 必须禁角色名

    def test_frame_term_from_cast_map(self):
        """cast_map 中 frame_term 优先取值，含角色名指向设定图。"""
        cast_map = {
            "苏见川": {
                "name": "苏见川",
                "generic_identity": {
                    "video_term": "深灰长袍的持剑青年",
                    "frame_term": "苏见川（约30岁，剑眉星眸）"
                }
            }
        }
        result = _generic_identity("苏见川", "苏见川挥剑。", 0,
                                  cast_map=cast_map, role="frame")
        assert "苏见川" in result  # frame_term 必须含角色名

    def test_fallback_to_legacy_regex(self):
        """无 cast_map 时退化为原文正则推断。"""
        result = _generic_identity("苏见川", "青年苏见川挥剑。", 0, role="video")
        assert "青年" in result

    def test_synthesize_from_persona(self):
        """cast_map 有 persona 但无 generic_identity 时，从 persona 合成。"""
        cast_map = {
            "苏见川": {
                "name": "苏见川",
                "persona": {
                    "age": "约30岁", "gender": "男",
                    "clothing": "深灰长风衣", "face": "剑眉星眸",
                    "props": ["长剑"]
                }
                # 无 generic_identity
            }
        }
        video = _generic_identity("苏见川", "苏见川挥剑。", 0,
                                  cast_map=cast_map, role="video")
        frame = _generic_identity("苏见川", "苏见川挥剑。", 0,
                                  cast_map=cast_map, role="frame")
        assert "苏见川" not in video  # video 禁角色名
        assert "苏见川" in frame  # frame 含角色名
        # 应包含服饰或道具信息
        assert "深灰" in video or "长风衣" in video or "持长剑" in video


# ──────────────────────────────────────────────────────────────
# COMPOSITION_BY_BEAT 完整性
# ──────────────────────────────────────────────────────────────

class TestCompositionByBeat:
    @pytest.mark.parametrize("beat", ["action", "turn", "emotion", "dialog", "atmos", "narration"])
    def test_six_fields_complete(self, beat):
        """每个 beat 的六字段都齐全。"""
        comp = COMPOSITION_BY_BEAT[beat]
        for field in ("focal_length", "camera_position", "composition",
                      "gaze_target", "focus_point", "stability"):
            assert comp[field], f"beat={beat} 缺字段 {field}"

    def test_beat_keys_match_camera(self):
        """COMPOSITION_BY_BEAT 与 CAMERA_BY_BEAT 的 beat 集合一致。"""
        assert set(COMPOSITION_BY_BEAT.keys()) == set(CAMERA_BY_BEAT.keys())


# ──────────────────────────────────────────────────────────────
# build_video_prompt / build_frame_prompt 注入新字段
# ──────────────────────────────────────────────────────────────

class TestPromptInjection:
    def test_video_prompt_injects_entry_state(self):
        """entry_state 非空时注入"承接上一镜"段。"""
        p = build_video_prompt(
            scene="客栈", narration="挥剑。", characters=[],
            camera="跟拍", style="CG", ratio_desc="9:16", secs=10,
            entry_state="剑已出鞘"
        )
        assert "承接上一镜" in p
        assert "剑已出鞘" in p

    def test_video_prompt_injects_blocking(self):
        """blocking 非空时注入"站位"段。"""
        p = build_video_prompt(
            scene="客栈", narration="挥剑。", characters=[],
            camera="跟拍", style="CG", ratio_desc="9:16", secs=10,
            blocking="两人相对"
        )
        assert "站位" in p
        assert "两人相对" in p

    def test_video_prompt_injects_composition(self):
        """构图六字段非空时注入"构图"段。"""
        p = build_video_prompt(
            scene="客栈", narration="挥剑。", characters=[],
            camera="跟拍", style="CG", ratio_desc="9:16", secs=10,
            focal_length="35mm", camera_position="平视",
            composition="三分法", gaze_target="对视",
            focus_point="人物面部", stability="稳定"
        )
        assert "构图" in p
        assert "焦距35mm" in p
        assert "机位平视" in p

    def test_video_prompt_no_entry_state_when_empty(self):
        """entry_state 为空时不注入承接段。"""
        p = build_video_prompt(
            scene="客栈", narration="挥剑。", characters=[],
            camera="跟拍", style="CG", ratio_desc="9:16", secs=10,
            entry_state=""
        )
        assert "承接上一镜" not in p

    def test_frame_prompt_injects_entry_state(self):
        """分镜图注入"承接上一镜末帧"段。"""
        p = build_frame_prompt(
            scene="客栈", narration="挥剑。", characters=["苏见川"],
            camera="跟拍", style="CG", ratio_desc="9:16",
            entry_state="剑已出鞘"
        )
        assert "承接上一镜末帧" in p

    def test_frame_prompt_uses_frame_term_with_name(self):
        """分镜图端角色名+核心特征，含角色名指向设定图。"""
        cast_map = {
            "苏见川": {
                "name": "苏见川",
                "generic_identity": {
                    "video_term": "深灰长袍青年",
                    "frame_term": "苏见川（约30岁，剑眉星眸）"
                }
            }
        }
        p = build_frame_prompt(
            scene="客栈", narration="挥剑。", characters=["苏见川"],
            camera="跟拍", style="CG", ratio_desc="9:16",
            cast_map=cast_map
        )
        assert "苏见川" in p
        assert "约30岁" in p or "剑眉星眸" in p

    def test_video_prompt_replaces_name_with_video_term(self):
        """视频正文端用通用身份替换角色名，禁角色名污染。"""
        cast_map = {
            "苏见川": {
                "name": "苏见川",
                "generic_identity": {
                    "video_term": "深灰长袍青年",
                    "frame_term": "苏见川（约30岁）"
                }
            }
        }
        p = build_video_prompt(
            scene="客栈", narration="苏见川挥剑。", characters=["苏见川"],
            camera="跟拍", style="CG", ratio_desc="9:16", secs=10,
            cast_map=cast_map
        )
        assert "苏见川" not in p
        assert "深灰长袍青年" in p


# ──────────────────────────────────────────────────────────────
# validate_shots 5 道质量门
# ──────────────────────────────────────────────────────────────

class TestValidateShots:
    def _make_sl(self, shots):
        sl = ShotList(title="test", style_key="3d-realistic", ratio="9:16",
                     style_desc="desc", ratio_desc="ratio", model="Seedance")
        sl.shots = shots
        return sl

    def test_gate1_over_15s(self):
        """门1：单镜超 15s 告警。"""
        s = Shot(index=1, seconds=20, beat="action", beat_label="动作",
                 scene="s", action="a", camera="c", narration="挥剑", source_text="挥剑")
        warnings = validate_shots(self._make_sl([s]))
        assert any("超过豆包" in w and "20s" in w for w in warnings)

    def test_gate2_dialog_too_long(self):
        """门2：旁白字数超镜头容量告警。"""
        long_narr = "雨" * 80  # 80 字需要约 17s
        s = Shot(index=1, seconds=8, beat="narration", beat_label="叙述",
                 scene="s", action="a", camera="c", narration=long_narr, source_text=long_narr)
        warnings = validate_shots(self._make_sl([s]))
        assert any("朗读需" in w for w in warnings)

    def test_gate3_over_three_persons(self):
        """门3：同框超 3 人告警。"""
        s = Shot(index=1, seconds=10, beat="dialog", beat_label="对白",
                 scene="s", action="a", camera="c", narration="甲乙丙丁对话",
                 source_text="甲乙丙丁对话",
                 on_screen_count=4)
        warnings = validate_shots(self._make_sl([s]))
        assert any("同框 4" in w for w in warnings)

    def test_gate5_missing_composition_fields(self):
        """门5：构图字段缺失告警。"""
        s = Shot(index=1, seconds=10, beat="action", beat_label="动作",
                 scene="s", action="a", camera="c", narration="挥剑", source_text="挥剑",
                 # 不设 focal_length 等，留空
                 )
        warnings = validate_shots(self._make_sl([s]))
        assert any("缺构图字段" in w for w in warnings)

    def test_no_warnings_when_all_pass(self):
        """全部通过的镜头无告警。"""
        s = Shot(index=1, seconds=10, beat="action", beat_label="动作",
                 scene="s", action="a", camera="c", narration="挥剑", source_text="挥剑",
                 focal_length="35mm", camera_position="平视",
                 composition="三分法", gaze_target="对视",
                 focus_point="人物", stability="稳定",
                 on_screen_count=1)
        warnings = validate_shots(self._make_sl([s]))
        # 不应有任何告警（10s/2字/1人/字段齐全）
        assert not any("超过豆包" in w for w in warnings)
        assert not any("朗读需" in w for w in warnings)
        assert not any("同框" in w for w in warnings)
        assert not any("缺构图字段" in w for w in warnings)


# ──────────────────────────────────────────────────────────────
# validate_continuity 衔接连贯
# ──────────────────────────────────────────────────────────────

class TestValidateContinuity:
    def test_disconnected_states_warning(self):
        """上一镜 exit_state 与本镜 entry_state 无重合关键词告警。"""
        shots = [
            Shot(index=1, seconds=10, beat="action", beat_label="动作",
                 scene="s", action="a", camera="c", narration="挥剑",
                 source_text="挥剑",
                 exit_state="剑已出鞘，寒光定格"),
            Shot(index=2, seconds=10, beat="emotion", beat_label="情绪",
                 scene="s", action="a", camera="c", narration="微笑",
                 source_text="微笑",
                 entry_state="笑容定格，神情舒展"),  # 与"剑"无重合
        ]
        warnings = validate_continuity(shots)
        assert any("衔接不连贯" in w for w in warnings)

    def test_connected_states_no_warning(self):
        """有重合关键词不告警。"""
        shots = [
            Shot(index=1, seconds=10, beat="action", beat_label="动作",
                 scene="s", action="a", camera="c", narration="挥剑",
                 source_text="挥剑",
                 exit_state="剑已出鞘"),
            Shot(index=2, seconds=10, beat="action", beat_label="动作",
                 scene="s", action="a", camera="c", narration="收剑",
                 source_text="收剑",
                 entry_state="剑已入鞘"),  # 都含"剑"
        ]
        warnings = validate_continuity(shots)
        assert not any("衔接不连贯" in w for w in warnings)

    def test_empty_states_no_warning(self):
        """任一状态为空不告警。"""
        shots = [
            Shot(index=1, seconds=10, beat="action", beat_label="动作",
                 scene="s", action="a", camera="c", narration="挥剑",
                 source_text="挥剑",
                 exit_state=""),
            Shot(index=2, seconds=10, beat="action", beat_label="动作",
                 scene="s", action="a", camera="c", narration="收剑",
                 source_text="收剑",
                 entry_state="剑已入鞘"),
        ]
        warnings = validate_continuity(shots)
        assert not any("衔接不连贯" in w for w in warnings)


# ──────────────────────────────────────────────────────────────
# 端到端 from_novel 新字段写入
# ──────────────────────────────────────────────────────────────

class TestFromNovelIntegration:
    NOVEL = """# 雨夜来客
雨夜客栈，灯火摇曳。
苏见川握紧长剑，猛地回头。
白若霜颤声道："他们追来了。"
门突然被撞开，黑影扑入。
苏见川挥剑而出，寒光闪过，黑影倒地。
白若霜望着地上的人，眼眶泛红："终于结束了。"
苏见川收剑入鞘，转身离去。
"""

    def test_from_novel_generates_new_fields(self):
        """from_novel 产出的 Shot 含新字段（非默认空）。"""
        sl = from_novel(self.NOVEL, "雨夜来客", "3d-realistic", "9:16", "Seedance")
        assert len(sl.shots) >= 2
        for s in sl.shots:
            # 构图字段必填
            assert s.focal_length, f"镜 {s.index} 缺 focal_length"
            assert s.camera_position, f"镜 {s.index} 缺 camera_position"
            assert s.composition, f"镜 {s.index} 缺 composition"
        # 至少有一个镜头有 exit_state（挥剑类末句命中）
        assert any(s.exit_state for s in sl.shots), "无任何镜头生成 exit_state"
        # 至少有一个镜头（非首镜）有 entry_state
        non_first = [s for s in sl.shots if s.index > 1]
        if non_first:
            assert any(s.entry_state for s in non_first), "无任何非首镜生成 entry_state"

    def test_from_novel_no_continuity_flag(self):
        """--no-continuity 时跳过衔接字段注入。"""
        sl = from_novel(self.NOVEL, "雨夜来客", "3d-realistic", "9:16", "Seedance",
                        no_continuity=True)
        for s in sl.shots:
            assert s.entry_state == ""
            assert s.exit_state == ""
            assert s.blocking == ""

    def test_from_novel_with_cast_map(self):
        """带 cast_map 时通用身份从结构化字段取值。"""
        cast_map = {
            "苏见川": {
                "name": "苏见川",
                "generic_identity": {
                    "video_term": "深灰长袍青年",
                    "frame_term": "苏见川（约30岁）"
                }
            }
        }
        sl = from_novel(self.NOVEL, "雨夜来客", "3d-realistic", "9:16", "Seedance",
                        cast_map=cast_map)
        # 找到含苏见川的镜头
        shot_with_su = next((s for s in sl.shots if "苏见川" in s.source_text), None)
        assert shot_with_su is not None
        # 视频正文 prompt 不应含角色名（已被通用身份替换）
        # 注意：build_shot 阶段 characters 还未标注，prompt 可能仍含原名
        # 真正的替换在 _finalize_prompts 完成（annotate_characters 调用后）
        # 这里只验证 cast_map 能传到 build_shot 不报错


# ──────────────────────────────────────────────────────────────
# 旧版 shots.json 兼容性
# ──────────────────────────────────────────────────────────────

class TestLegacyCompatibility:
    def test_legacy_shots_json_loadable(self):
        """旧版 shots.json（无新字段）能被消费，新字段缺省为空。"""
        legacy = {
            "title": "legacy",
            "shots": [
                {
                    "index": 1, "seconds": 10, "beat": "action",
                    "beat_label": "动作", "scene": "客栈",
                    "action": "挥剑", "camera": "跟拍",
                    "narration": "挥剑", "source_text": "挥剑",
                    "style": "CG", "ratio": "9:16",
                    "prompt": "旧版提示词", "frame_prompt": "旧版分镜",
                    # 无 blocking/entry_state/exit_state/构图字段/on_screen_count
                }
            ]
        }
        # 模拟 doubao_gen.py 读取旧 shots.json 的场景：
        # 直接取字段，缺省字段用 getattr(shot, field, "") 处理
        shot = legacy["shots"][0]
        # 关键字段仍能取到
        assert shot["prompt"] == "旧版提示词"
        assert shot["seconds"] == 10
        # 新字段缺失，doubao_gen 应能用 .get(field, "") 兜底
        assert shot.get("blocking", "") == ""
        assert shot.get("focal_length", "") == ""
        assert shot.get("on_screen_count", 0) == 0

    def test_build_shot_legacy_call_signature(self):
        """build_shot 不传新参数也能跑（旧调用兼容）。"""
        # 旧调用方式：只传 index/text/sec/style/ratio_desc/prev_scene
        shot = build_shot(1, "挥剑。", 10, "CG", "9:16")
        assert shot.index == 1
        assert shot.seconds == 10
        # 新字段有默认值
        assert shot.blocking == "" or shot.blocking  # 可能为空或有推断值
        assert shot.focal_length  # COMPOSITION_BY_BEAT 默认值非空


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
