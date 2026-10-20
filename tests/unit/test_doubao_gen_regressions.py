# -*- coding: utf-8 -*-
"""
doubao_gen 已知 bug 回归套件（不耗额度，默认跑）。

回归点：
  - parse_confirm_card 全角/半角冒号都能解析
  - QUOTA_RE 匹配典型额度耗尽文案、不匹配普通文本
  - ensure_video_mode docstring 含「已禁用」（防止偷偷接回主流程）
  - RENDER_TIMEOUT == 900（15 分钟，避免误杀）
"""
import os
import re
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

import doubao_gen


pytestmark = [pytest.mark.unit]


# ──────────────────────────────────────────────────────────────
# parse_confirm_card
# ──────────────────────────────────────────────────────────────

class TestParseConfirmCard:
    def test_full_card_all_fields(self):
        """标准参数确认卡全字段解析。"""
        card = """
视频生成参数确认
模型：Seedance 2.0
时长：12秒
比例：9:16
风格：CG写实3D
"""
        r = doubao_gen.parse_confirm_card(card)
        assert r["model"] == "Seedance 2.0"
        assert r["seconds"] == 12
        assert r["ratio"] == "9:16"
        assert "CG写实3D" in r["style"]

    def test_half_width_colon(self):
        """半角冒号也能解析。"""
        card = "模型: Seedance\n时长: 8\n比例: 16:9\n风格: cinematic"
        r = doubao_gen.parse_confirm_card(card)
        assert r["model"] == "Seedance"
        assert r["seconds"] == 8
        assert r["ratio"] == "16:9"

    def test_missing_fields_return_none(self):
        """缺字段的对应返回 None。"""
        card = "模型：Seedance 2.0"
        r = doubao_gen.parse_confirm_card(card)
        assert r["model"] == "Seedance 2.0"
        assert r["seconds"] is None
        assert r["ratio"] is None
        assert r["style"] is None

    def test_empty_card(self):
        """空卡返回全 None。"""
        r = doubao_gen.parse_confirm_card("")
        assert r == {"model": None, "seconds": None, "ratio": None, "style": None}


# ──────────────────────────────────────────────────────────────
# QUOTA_RE 额度耗尽文案
# ──────────────────────────────────────────────────────────────

class TestQuotaRe:
    @pytest.mark.parametrize("phrase", [
        "今日免费次数已用完",
        "明天再来吧",
        "明日再来",
        "额度不足",
        "今日次数已用完",
        "免费次数用完了",
    ])
    def test_quota_re_matches_exhaustion(self, phrase):
        """典型额度耗尽文案必须匹配。"""
        assert doubao_gen.QUOTA_RE.search(phrase), f"额度文案「{phrase}」未匹配 QUOTA_RE"

    @pytest.mark.parametrize("phrase", [
        "生成完成",
        "渲染中",
        "确认生成",
        "视频已就绪",
        "请稍候",
    ])
    def test_quota_re_misses_normal_text(self, phrase):
        """普通文本不应被误判为额度耗尽。"""
        assert not doubao_gen.QUOTA_RE.search(phrase), f"普通文本「{phrase}」被误判为额度耗尽"


# ──────────────────────────────────────────────────────────────
# ensure_video_mode 已禁用
# ──────────────────────────────────────────────────────────────

def test_ensure_video_mode_documented_disabled():
    """
    回归「ensure_video_mode 偷偷接回主流程」风险：
    函数 docstring 必须含「已禁用」字样。
    """
    doc = doubao_gen.ensure_video_mode.__doc__ or ""
    assert "已禁用" in doc, (
        "ensure_video_mode 的 docstring 必须含「已禁用」字样，"
        "防止后续被偷偷接回主流程（project_memory 2026-10-16 实测结论）"
    )


# ──────────────────────────────────────────────────────────────
# 关键常量
# ──────────────────────────────────────────────────────────────

def test_render_timeout_value():
    """RENDER_TIMEOUT == 900（15 分钟，避免误杀已出片但还在排队的镜头）。"""
    assert doubao_gen.RENDER_TIMEOUT == 900


def test_reply_state_js_contains_quota_patterns():
    """REPLY_STATE_JS 必须含额度耗尽文案检测字样。"""
    js = doubao_gen.REPLY_STATE_JS
    assert "免费次数" in js, "REPLY_STATE_JS 缺「免费次数」检测"
    assert "明天再来" in js or "明日再来" in js, "REPLY_STATE_JS 缺「明天/明日再来」检测"


# ──────────────────────────────────────────────────────────────
# send_confirmation 的 allow_text=False 不发文本
# ──────────────────────────────────────────────────────────────

class TestSendConfirmation:
    def test_send_confirmation_callable(self):
        """send_confirmation 函数存在且可调用（不实际执行 CDP 调用）。"""
        assert callable(doubao_gen.send_confirmation)
        # 检查函数签名：allow_text 默认 True，且是关键字参数
        import inspect
        sig = inspect.signature(doubao_gen.send_confirmation)
        params = list(sig.parameters.values())
        assert len(params) >= 2, "send_confirmation 应至少有 (page, allow_text=True) 两个参数"
        # allow_text 参数应有默认值 True
        allow_text_param = sig.parameters.get("allow_text")
        assert allow_text_param is not None, "send_confirmation 缺 allow_text 参数"
        assert allow_text_param.default is True, (
            "allow_text 默认值应为 True（主确认时允许发文本兜底）"
        )
