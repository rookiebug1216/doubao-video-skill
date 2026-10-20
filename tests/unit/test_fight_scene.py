# -*- coding: utf-8 -*-
"""
打斗场景增强（fight 节拍）测试套件（不耗额度，默认跑）。

覆盖：
  - is_fight_scene 三路判定（显性打斗词 / 枪战爆炸 / 兵器+攻击动作组合）
  - 误判防护：静态持械、单纯移动、日常用语不判打斗
  - detect_beat：打斗优先于 action
  - 五张映射表 fight 键齐全
  - build_video_prompt 打斗分支：密集时间线、镜头语言指令、打斗氛围段、末拍慢动作
  - 非打斗镜 prompt 不受污染（不含打斗增强内容）
  - from_novel 端到端：打斗镜 beat/beat_label/prompt 正确
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

from split_novel import (
    is_fight_scene, detect_beat, build_video_prompt, from_novel,
    _split_beats, _fight_beat_directives, _fight_chain_desc,
    _fight_weapon_word, _fight_point_word, shot_tension,
    BEAT_TENSION, BEAT_LABEL, BEAT_PAD, CAMERA_BY_BEAT, COMPOSITION_BY_BEAT,
)

pytestmark = [pytest.mark.unit]


# ──────────────────────────────────────────────────────────────
# is_fight_scene 判定
# ──────────────────────────────────────────────────────────────

class TestIsFightScene:
    @pytest.mark.parametrize("text", [
        "门突然被撞开，黑影扑入。苏见川挥剑而出，寒光闪过，黑影倒地。",
        "两人交手十余招，难分高下。",
        "他挥刀劈向对手。",
        "拳掌相交，气浪四散。",
        "黑衣人一拥而上，围攻上来。",
        "他拔枪射击，子弹呼啸而出。",
        "两人扭打在一起，厮打不休。",
        "她拔刀出鞘，直取对方咽喉。",
        "长枪横扫，逼退三人。",
        "爆炸声响起，火光冲天。",
    ])
    def test_fight_positive(self, text):
        assert is_fight_scene(text), f"应判为打斗：{text}"

    @pytest.mark.parametrize("text", [
        "苏见川握紧长剑，猛地回头。",          # 静态持械 + 回头
        "白若霜颤声道：\"他们追来了。\"",       # 对白 + 移动
        "他打着灯笼走进院子。",                 # 日常"打"字
        "台下掌声雷动。",                       # 掌=名词
        "他拔腿就跑，消失在巷口。",             # 拔腿（不收"腿"防误判）
        "雨夜客栈，灯火摇曳。",                 # 纯氛围
        "她低头看着桌上的信。",                 # 纯叙述
        "佩剑悬在腰间，他缓步上楼。",           # 静态持械
        "",                                     # 空文本
    ])
    def test_fight_negative(self, text):
        assert not is_fight_scene(text), f"不应判为打斗：{text}"


# ──────────────────────────────────────────────────────────────
# detect_beat 与映射表
# ──────────────────────────────────────────────────────────────

class TestFightBeat:
    def test_fight_beat_detected(self):
        assert detect_beat("苏见川挥剑而出，寒光闪过，黑影倒地。") == "fight"

    def test_action_not_fight(self):
        """普通动作仍是 action，不被打斗抢走。"""
        assert detect_beat("他猛地回头，冲向门口。") == "action"

    def test_tables_have_fight(self):
        for table in (BEAT_TENSION, BEAT_LABEL, BEAT_PAD, CAMERA_BY_BEAT, COMPOSITION_BY_BEAT):
            assert "fight" in table, f"映射表缺 fight 键：{table}"

    def test_fight_tension_high(self):
        assert BEAT_TENSION["fight"] >= 3.0
        assert shot_tension("他挥剑而出", "fight") >= 3.0

    def test_fight_camera_has_multiple_terms(self):
        """打斗运镜必须是多条轮换短语，不能再是单一"动态跟拍"。"""
        terms = [t for t in CAMERA_BY_BEAT["fight"].split("，") if t.strip()]
        assert len(terms) >= 3

    def test_fight_composition_six_fields(self):
        comp = COMPOSITION_BY_BEAT["fight"]
        for field in ("focal_length", "camera_position", "composition",
                      "gaze_target", "focus_point", "stability"):
            assert comp[field], f"fight 构图缺字段 {field}"


# ──────────────────────────────────────────────────────────────
# 密集节拍拆分
# ──────────────────────────────────────────────────────────────

class TestDenseSplit:
    TEXT = "门突然被撞开，黑影扑入。他挥剑而出，寒光闪过，黑影倒地"

    def test_dense_split_denser_than_normal(self):
        normal = _split_beats(self.TEXT, 12, dense=False)
        dense = _split_beats(self.TEXT, 12, dense=True)
        assert len(dense) > len(normal), "打斗密集拆分应产生更多节拍"

    def test_dense_split_clause_level(self):
        """打斗模式逐子句成拍。"""
        dense = _split_beats(self.TEXT, 12, dense=True)
        assert "寒光闪过" in dense
        assert "黑影倒地" in dense

    def test_normal_split_unchanged(self):
        """普通模式维持原 2 子句合并行为（回归保护）。"""
        normal = _split_beats(self.TEXT, 12, dense=False)
        assert any("，" in b for b in normal), "普通模式应保留子句合并"


# ──────────────────────────────────────────────────────────────
# 节拍指令
# ──────────────────────────────────────────────────────────────

class TestFightBeatDirectives:
    def test_swing_gets_motion_blur(self):
        cam, desc = _fight_beat_directives("他挥剑劈出")
        assert "残影" in desc or "速度感" in desc
        assert cam  # 挥击有专属运镜覆盖

    def test_dodge_gets_handheld(self):
        cam, desc = _fight_beat_directives("侧身闪避")
        assert "闪避" in desc or "动态模糊" in desc

    def test_fall_gets_reaction(self):
        cam, desc = _fight_beat_directives("黑影倒地")
        assert "受击" in desc or "打击感" in desc

    def test_last_beat_slow_motion(self):
        _, desc = _fight_beat_directives("黑影倒地", is_last=True)
        assert "慢动作" in desc

    def test_non_last_beat_no_slow_motion(self):
        _, desc = _fight_beat_directives("黑影倒地", is_last=False)
        assert "慢动作" not in desc

    def test_no_match_returns_empty(self):
        cam, desc = _fight_beat_directives("雨还在下")
        assert cam is None and desc == ""


# ──────────────────────────────────────────────────────────────
# build_video_prompt 打斗分支
# ──────────────────────────────────────────────────────────────

FIGHT_NARRATION = "门突然被撞开，黑影扑入。他挥剑而出，寒光闪过，黑影倒地。"


def _build_fight_prompt(secs=12):
    return build_video_prompt(
        scene="雨夜的客栈", narration=FIGHT_NARRATION, characters=[],
        camera=CAMERA_BY_BEAT["fight"], style="CG", ratio_desc="9:16", secs=secs,
        exit_state="剑已出鞘，寒光定格",
    )


class TestFightPrompt:
    def test_fight_atmosphere_injected(self):
        p = _build_fight_prompt()
        assert "打斗氛围" in p
        assert "打击感" in p

    def test_fight_timeline_dense(self):
        """打斗时间线至少 4 拍（12s / 短拍快切）。"""
        p = _build_fight_prompt()
        import re as _re
        segs = _re.findall(r"\d+-\d+s：", p)
        assert len(segs) >= 4, f"打斗时间线拍数不足：{segs}"

    def test_fight_camera_variety(self):
        """不同节拍使用不同运镜，拒绝全程单一口吻。"""
        p = _build_fight_prompt()
        hits = sum(1 for kw in ("仰拍", "特写", "环绕", "晃动") if kw in p)
        assert hits >= 2, "打斗运镜缺少多样性"

    def test_fight_last_beat_slow_motion(self):
        p = _build_fight_prompt()
        assert "慢动作" in p

    def test_fight_impact_language(self):
        p = _build_fight_prompt()
        assert "受击" in p or "残影" in p or "火花" in p

    def test_exit_state_still_appended(self):
        p = _build_fight_prompt()
        assert "定格" in p and "剑已出鞘" in p

    def test_non_fight_prompt_not_polluted(self):
        """普通镜 prompt 不得出现打斗增强内容。"""
        p = build_video_prompt(
            scene="客栈", narration="她低头看着桌上的信。",
            characters=[], camera="中景缓慢平移", style="CG",
            ratio_desc="9:16", secs=8,
        )
        assert "打斗氛围" not in p
        assert "慢动作" not in p
        assert "打击感" not in p

    def test_static_weapon_not_fight(self):
        """静态持械（握紧长剑）不触发打斗分支。"""
        p = build_video_prompt(
            scene="客栈", narration="他握紧长剑，猛地回头。",
            characters=[], camera="动态跟拍", style="CG",
            ratio_desc="9:16", secs=8,
        )
        assert "打斗氛围" not in p


# ──────────────────────────────────────────────────────────────
# 打斗编排增强（因果链：出招 → 命中 → 受击倒地）
# ──────────────────────────────────────────────────────────────

class TestFightChoreography:
    """2026-10-20 用户反馈：原提示词只有"挥剑→倒地"，中间无可见命中，"
    视频里人突然倒地、因果断裂。以下用例锁定修复后的因果链编排。"""

    def test_weapon_word_detection(self):
        assert _fight_weapon_word("他挥剑而出") == "剑"
        assert _fight_weapon_word("长枪横扫，逼退三人") == "长枪"
        assert _fight_weapon_word("他挥刀劈下") == "刀"
        assert _fight_weapon_word("她一掌拍出") == "掌"
        assert _fight_weapon_word("雨还在下") == "兵器"   # 无兵器 → 通用兜底

    def test_point_word_mapping(self):
        assert _fight_point_word("剑") == "剑锋"
        assert _fight_point_word("长枪") == "枪尖"
        assert _fight_point_word("拳") == "拳头"
        assert _fight_point_word("未知兵器") == "未知兵器"   # 无映射原样返回

    def test_body_entry_not_wood_dust(self):
        """"扑入"是人身动作，不得写成破门的"木屑飞溅"。"""
        _, desc = _fight_beat_directives("黑影扑入")
        assert "木屑" not in desc
        assert "闯入" in desc

    def test_door_break_gets_wood_dust(self):
        """"撞开"是破门，保留木屑飞溅。"""
        _, desc = _fight_beat_directives("门突然被撞开")
        assert "木屑" in desc

    def test_attack_beat_names_hit_region(self):
        """攻击拍后 1~2 拍内有倒地拍时，标注攻击目标与命中部位。"""
        beats = ["他挥剑而出", "寒光闪过", "黑影倒地"]
        chain = _fight_chain_desc("他挥剑而出", beats, 0, "剑")
        assert "直取对方" in chain and "命中其" in chain

    def test_fall_beat_has_cause_and_mechanism(self):
        """倒地拍给出"被击中 + 失衡"的因（供前置到结果之前）。"""
        chain = _fight_chain_desc("黑影倒地", ["黑影倒地"], 0, "剑")
        assert "被剑锋命中" in chain
        assert "重心失衡" in chain

    def test_chain_empty_for_neutral_beat(self):
        """寒光/氛围类中性拍不加命中描述，避免重复命中。"""
        assert _fight_chain_desc("寒光闪过", ["寒光闪过", "黑影倒地"], 0, "剑") == ""

    def test_prompt_causal_chain_present(self):
        """整段打斗 prompt 同时含攻击命中与倒地原因。"""
        p = _build_fight_prompt()
        assert "命中其" in p, "攻击拍缺命中部位"
        assert "被剑锋命中" in p and "重心失衡" in p, "倒地拍缺原因与机制"

    def test_fall_cause_before_result_and_slow_motion(self):
        """原因必须排在"倒地"结果与末拍慢动作之前（先因后果）。"""
        p = _build_fight_prompt()
        assert p.index("被剑锋命中") < p.index("受击倒地"), "原因排在了结果之后"
        assert p.index("重心失衡") < p.index("瞬间慢动作特写")

    def test_non_fight_prompt_no_chain(self):
        """非打斗 prompt 不得出现命中部位编排。"""
        p = build_video_prompt(
            scene="客栈", narration="她低头看着桌上的信。",
            characters=[], camera="中景缓慢平移", style="CG",
            ratio_desc="9:16", secs=8,
        )
        assert "命中其" not in p and "重心失衡" not in p


# ──────────────────────────────────────────────────────────────
# from_novel 端到端
# ──────────────────────────────────────────────────────────────

class TestFightEndToEnd:
    NOVEL = """# 雨夜来客
雨夜客栈，灯火摇曳。
苏见川握紧长剑，猛地回头。
门突然被撞开，黑影扑入。
苏见川挥剑而出，寒光闪过，黑影倒地。
白若霜望着地上的人，眼眶泛红。
"""

    def test_fight_shot_beat_label(self):
        sl = from_novel(self.NOVEL, "雨夜来客", "3d-realistic", "9:16", "Seedance")
        fight_shots = [s for s in sl.shots if s.beat == "fight"]
        assert fight_shots, "应至少有一个打斗镜"
        for s in fight_shots:
            assert s.beat_label == "打斗"
            assert "打斗氛围" in s.prompt
            assert "慢动作" in s.prompt

    def test_static_weapon_shot_stays_action(self):
        """"握紧长剑，猛地回头"镜保持 action 节拍，不混入打斗。"""
        sl = from_novel(self.NOVEL, "雨夜来客", "3d-realistic", "9:16", "Seedance")
        static_shots = [s for s in sl.shots if "握紧长剑" in s.source_text
                        and "挥剑" not in s.source_text]
        for s in static_shots:
            assert s.beat != "fight", f"镜 {s.index} 静态持械被误判为打斗"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
