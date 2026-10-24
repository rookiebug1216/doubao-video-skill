# -*- coding: utf-8 -*-
"""
doubao_gen 已知 bug 回归套件（不耗额度，默认跑）。

回归点：
  - parse_confirm_card 全角/半角冒号都能解析
  - QUOTA_RE 匹配典型额度耗尽文案、不匹配普通文本
  - ensure_video_mode docstring 含「已禁用」（防止偷偷接回主流程）
  - RENDER_TIMEOUT == 900（15 分钟，避免误杀）
"""
import inspect
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
from shared import doubao_page


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


# ──────────────────────────────────────────────────────────────
# 2026-10-24 现场修复移植回归锁（WorkBuddy 事故记录的 13 个技能 bug）
# ──────────────────────────────────────────────────────────────

def _code_src(func) -> str:
    """getsource 去掉 docstring —— 文档里描述 bug 的字样（如「合成 hit.click()
    不走 React」）不参与负向断言，只检查真实代码。"""
    src = inspect.getsource(func)
    return re.sub(r'"""(?:.|\n)*?"""', "", src, count=1)


class TestFieldFixesConfirmation:
    """参数确认链路的现场修复必须保持。"""

    def test_send_confirmation_uses_real_mouse_click(self):
        """按钮点击必须走 CDP Input.dispatchMouseEvent —— 合成 hit.click()
        不走 React onClick，点了没反应（现场 bug 13①）。"""
        src = _code_src(doubao_gen.send_confirmation)
        assert "Input.dispatchMouseEvent" in src
        assert ".click()" not in src, "禁止合成 click（不走 React onClick）"

    def test_wait_render_no_prelock_at_start(self):
        """起始基线不得把页面已有参数卡计入 confirmed_rounds —— wait_render
        在提交后才启动，新卡已渲染，预锁定会让本镜永不确认直到超时
        （现场 bug 13②，用户两次截图实锤）。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "本镜将重新确认" in src, "起始已有卡只记日志，不预锁定"
        # confirmed_rounds.add 只能出现在确认成功之后
        add_pos = src.find("confirmed_rounds.add(")
        ok_pos = src.find('action != "none"')
        assert add_pos > 0 and ok_pos > 0
        assert add_pos > ok_pos, "confirmed_rounds.add 必须在确认成功（action != none）之后"

    def test_wait_render_confirm_fail_gives_up(self):
        """确认失败不得锁轮次，连续 8 次失败才放弃（防无限重试也防一败弃赛）。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "confirm_fail" in src
        assert "confirm_fail >= 8" in src
        assert "不锁定轮次" in src


class TestFieldFixesSrcIntegrity:
    """src 完整性与跨镜串片防护的现场修复必须保持。"""

    def test_get_video_src_excludes_known_old_srcs(self):
        """get_video_src 必须支持 exclude —— 否则本镜 duration 未就绪时会
        取到上一镜残留 video，下载到旧 mp4（现场 bug 9，MD5 相同实锤）。"""
        sig = inspect.signature(doubao_gen.get_video_src)
        assert "exclude" in sig.parameters
        src = inspect.getsource(doubao_gen.get_video_src)
        assert "startsWith" in src, "src 匹配需容忍 blob/https 前缀差异"

    def test_download_video_prefers_confirmed_src(self):
        """download_video 必须优先用 wait_render 确认的本镜 src。"""
        sig = inspect.signature(doubao_gen.download_video)
        assert "preferred_src" in sig.parameters
        assert "exclude_srcs" in sig.parameters
        src = inspect.getsource(doubao_gen.download_video)
        assert "preferred_src or get_video_src(page, exclude=exclude_srcs)" in src

    def test_generate_shot_wires_src_preferences(self):
        """generate_shot 必须把本镜 src / 已知旧 src 接到下载与等待链路。
        baseline_srcs 经 video['baseline_srcs'] 传递（2026-10-24 实跑 NameError：
        generate_shot 直接引用 wait_render 局部变量导致出片后下载必崩）。"""
        src = inspect.getsource(doubao_gen.generate_shot)
        assert "prev_known_srcs=_KNOWN_VIDEO_SRCS" in src
        assert 'preferred_src=video.get("src")' in src
        assert 'exclude_srcs=video.get("baseline_srcs", set()) | set(_KNOWN_VIDEO_SRCS)' in src
        assert "baseline_srcs |" not in src, "不得引用未定义的 wait_render 局部变量"

    def test_wait_render_returns_baseline_srcs(self):
        """wait_render 成功返回时必须把 baseline_srcs 附着到 video 字典。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert 'out["baseline_srcs"] = baseline_srcs' in src

    def test_wait_render_keeps_full_video_src(self):
        """视频 src 不得截断 —— CDN 签名在 query 参数里，截断即 403（现场 bug 9）。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert ".slice(0,150)" not in src and "slice(0, 150)" not in src


class TestFieldFixesInputBox:
    """豆包新版 ProseMirror 输入框填入/提交方式的现场修复必须保持。"""

    def test_fill_prompt_uses_exec_command_insert_text(self):
        """填入必须用 execCommand('insertText') —— 唯一能同步 React 状态的
        方式；Input.insertText 只改 DOM 不改状态（现场 bug 6）。"""
        src = inspect.getsource(doubao_page.fill_prompt)
        assert "execCommand('insertText'" in src
        assert "execCommand('delete'" in src, "填入前必须清空残留内容"

    def test_submit_prompt_synthetic_enter_main_path(self):
        """提交主路径必须是合成 KeyboardEvent('Enter')；CDP dispatchKeyEvent
        的 Enter 实测页面无反应（现场 bug 6）。"""
        src = _code_src(doubao_page.submit_prompt)
        assert "KeyboardEvent" in src
        assert "Input.dispatchKeyEvent" not in src

    def test_submit_prompt_click_fallback_hides_overlays(self):
        """点击兜底必须先关素材面板、隐藏插件浮层（两者都遮挡发送按钮），
        且点击必须用 CDP 真实鼠标事件（现场 bug 6）。"""
        src = inspect.getsource(doubao_page.submit_prompt)
        assert "_close_asset_panel(page)" in src
        assert "_hide_launcher(page)" in src
        assert "Input.dispatchMouseEvent" in src

    def test_new_conversation_waits_editor_mount(self):
        """new_conversation 导航后必须等 ProseMirror 编辑器挂载 —— page_ready
        只探测 textarea/input，会过早返回就绪（现场 bug 11 前置）。"""
        src = inspect.getsource(doubao_page.new_conversation)
        assert "ProseMirror" in src

    def test_upload_images_waits_file_input_ready(self):
        """注入前必须等 input[type=file] 就绪 —— React 水合期
        setFileInputFiles 会静默失败（现场 bug 11）。"""
        src = inspect.getsource(doubao_page.upload_images)
        assert "_wait_file_input_ready(page" in src
        assert src.index("_wait_file_input_ready(page") < src.index("page.set_file_input(")

    def test_login_state_js_recognizes_plaintext_only_editor(self):
        """登录判定必须认得新版 ProseMirror 编辑器（contenteditable=
        "plaintext-only"），否则已登录页面被误判未登录（2026-10-24 probe 实锤）。"""
        js = doubao_page.LOGIN_STATE_JS
        assert '[contenteditable="plaintext-only"]' in js
        assert ".ProseMirror" in js
