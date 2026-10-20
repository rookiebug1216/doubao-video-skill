# -*- coding: utf-8 -*-
"""
split_novel 已知 bug 回归套件（不耗额度，默认跑）。

回归点：
  - 15s 钳制（任何输出镜头 ≤ 15s）
  - 悬念切分归位词（含「终于/从此/恢复平静」的镜头被顺延）
  - 单句超长触发 _split_long_sentence
  - 无标点退化按字数硬切
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# skills/doubao-manju-video 目录含连字符，不是合法 Python 包名，
# 不能用 from skills.doubao_manju_video.scripts import ...
# 直接把 scripts 目录加入 sys.path 后 import 顶层模块（与脚本自身的导入方式一致）
_SCRIPTS_DIR = os.path.join(_ROOT, "skills", "doubao-manju-video", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import split_novel
from split_novel import (
    MAX_SHOT_SECONDS, MIN_SHOT_SECONDS, RESOLVE_RE,
    greedy_group, _split_by_length, _split_long_sentence,
    assign_episodes, shot_tension, HOOK_TENSION,
)
from split_novel import Shot


pytestmark = [pytest.mark.unit]


# ──────────────────────────────────────────────────────────────
# 15s 钳制
# ──────────────────────────────────────────────────────────────

class TestClamp15Seconds:
    def test_split_by_length_never_exceeds_15s(self):
        """无标点长文本被 _split_by_length 切后每块 ≤ 15s。"""
        text = "雨" * 200   # 200 个汉字，远超单镜容量
        chunks = _split_by_length(text)
        assert chunks, "应至少切出 1 块"
        for txt, sec in chunks:
            assert sec <= MAX_SHOT_SECONDS, (
                f"切出的块时长 {sec}s > {MAX_SHOT_SECONDS}s：{txt[:30]}…"
            )

    def test_split_long_sentence_never_exceeds_15s(self):
        """单句超长被 _split_long_sentence 切后每块 ≤ 15s。"""
        # 用逗号分隔但仍超长的长句
        text = "雨夜客栈灯火摇曳，" + ("剑客握紧长剑，" * 10) + "猛地回头。"
        chunks = _split_long_sentence(text)
        assert chunks, "应至少切出 1 块"
        for txt, sec in chunks:
            assert sec <= MAX_SHOT_SECONDS, (
                f"切出的块时长 {sec}s > {MAX_SHOT_SECONDS}s：{txt[:30]}…"
            )

    def test_greedy_group_final_clamp(self):
        """greedy_group 最终输出每块 ≤ 15s（防御性钳制）。"""
        # 混合：长句 + 短句
        sentences = [
            "雨夜客栈灯火摇曳，" + ("剑客握紧长剑，" * 8) + "猛地回头。",
            "他握紧长剑。",
            "黑影扑入。",
            "终于结束了。",
        ]
        chunks = greedy_group(sentences)
        for txt, sec in chunks:
            assert sec <= MAX_SHOT_SECONDS, f"输出块超 15s：{sec}s {txt[:30]}…"


# ──────────────────────────────────────────────────────────────
# 悬念切分归位词
# ──────────────────────────────────────────────────────────────

class TestResolveWordBlocksHook:
    def test_resolve_re_matches_typical_words(self):
        """归位词正则匹配典型词。"""
        for w in ("终于", "从此", "再也没有", "尘埃落定", "归于平静", "恢复平静", "长舒一口气"):
            assert RESOLVE_RE.search(w), f"归位词「{w}」未被正则匹配"

    def test_resolve_re_misses_normal_text(self):
        """普通文本不被误判为归位词。"""
        for s in ("他挥剑而出", "黑影倒地", "雨夜客栈", "苏见川握紧长剑"):
            assert not RESOLVE_RE.search(s), f"普通文本「{s}」被误判为归位词"

    def test_shot_tension_exclamation_boost(self):
        """含「！」的镜头张力 +0.5。"""
        base = shot_tension("他挥剑而出", "action")
        with_excl = shot_tension("他挥剑而出！", "action")
        assert with_excl == base + 0.5, (
            f"「！」应+0.5 张力：base={base}, with_excl={with_excl}"
        )

    def test_resolve_word_blocks_hook_in_episode(self):
        """
        含归位词的镜头不应作为悬念结尾，应被顺延到下一集开头。
        """
        # 6 镜，第 5 镜含归位词"终于"
        shots = []
        for i in range(1, 7):
            text = f"剑客挥剑第{i}次，动作凌厉。"
            if i == 5:
                text = "终于结束了。一切归于平静。"
            shots.append(Shot(
                index=i, seconds=8, beat="action" if i != 5 else "narration",
                beat_label="动作", scene="客栈", action=text[:60],
                camera="跟随", narration=text, source_text=text,
            ))
        # ep_size=5，第 5 镜应在第 1 集末尾
        warnings = assign_episodes(shots, ep_size=5, is_final=False)
        # 第 5 镜（index=5）含归位词，应被顺延到第 2 集开头
        shot_5 = next(s for s in shots if s.index == 5)
        # 至少不应是 hook_end（含归位词的镜头不能作为悬念结尾）
        assert not shot_5.hook_end, "含归位词的镜头不应作为悬念结尾"
        # 第 1 集结尾应在第 4 镜（高张力 action）而非第 5 镜（归位词）
        first_episode_shots = [s for s in shots if s.episode == 1]
        if first_episode_shots:
            last_in_ep1 = first_episode_shots[-1]
            assert last_in_ep1.index != 5 or shot_5.carry_over, (
                "含归位词的第 5 镜应被顺延（carry_over=True）或不在第 1 集末尾"
            )


# ──────────────────────────────────────────────────────────────
# 最小镜头时长
# ──────────────────────────────────────────────────────────────

def test_min_shot_seconds_value():
    """MIN_SHOT_SECONDS == 4（低于此值无叙事价值）。"""
    assert MIN_SHOT_SECONDS == 4


def test_max_shot_seconds_value():
    """MAX_SHOT_SECONDS == 15（豆包免费版硬上限）。"""
    assert MAX_SHOT_SECONDS == 15


def test_hook_tension_value():
    """HOOK_TENSION == 2.0（>= 此值的镜头才有资格收尾一集）。"""
    assert HOOK_TENSION == 2.0
