# -*- coding: utf-8 -*-
"""
doubao_gen 交互流已知 bug 回归套件（用 FakeCDPPage，不耗额度，默认跑）。

回归点：
  - shot_confirmed 锁定防止重复发文本确认（核心：project_memory 2026-10-17 修复）
  - set_file_input 不派发额外 input/change 事件（核心：project_memory 2026-10-17 修复）
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# skills/doubao-manju-video 目录含连字符，不是合法 Python 包名，
# 直接把 scripts 目录加入 sys.path 后 import 顶层模块
_SCRIPTS_DIR = os.path.join(_ROOT, "skills", "doubao-manju-video", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from shared.cdp_browser import CDPPage, CDPError


pytestmark = [pytest.mark.mock]


# ──────────────────────────────────────────────────────────────
# FakeCDPPage 替身
# ──────────────────────────────────────────────────────────────

class FakeCDPPage:
    """极简 CDPPage 替身：记录 eval_js/call 调用，按预设脚本返回值。"""

    def __init__(self, scripts=None):
        # scripts: list of (js_substring, return_value)，按顺序匹配消费
        self.scripts = list(scripts) if scripts else []
        self.calls = []          # 所有 eval_js/call 的 js 或 method
        self.full_js = []        # 完整 js 字符串
        self._cookies = []

    def eval_js(self, js, await_promise=False, recv_timeout=120):
        self.calls.append(js)
        self.full_js.append(js)
        for i, (sub, ret) in enumerate(self.scripts):
            if sub in js:
                self.scripts.pop(i)
                if isinstance(ret, Exception):
                    raise ret
                return ret
        return None

    def call(self, method, recv_timeout=120, **params):
        self.calls.append(method)
        return {}

    def cookies(self):
        return self._cookies

    def screenshot(self, path):
        pass

    def close_tab(self):
        pass


# ──────────────────────────────────────────────────────────────
# set_file_input 不派发额外事件
# ──────────────────────────────────────────────────────────────

class TestSetFileInputNoExtraEvents:
    """
    回归「参考图重复上传两次」bug（2026-10-17 用户实测）：
    set_file_input 调用 DOM.setFileInputFiles 后，
    不应通过 Runtime.evaluate 派发任何 input/change/dispatchEvent 事件。
    """

    def test_set_file_input_does_not_dispatch_input_event(self, tmp_path):
        """
        构造一个真实的 CDPPage 并 stub 其 call/eval_js，
        验证 set_file_input 调用后，没有出现 dispatchEvent / new Event / 'input' 事件派发。
        """
        # 直接构造 CDPPage 实例但不连真实 WebSocket：替换其 call/eval_js 方法
        page = CDPPage.__new__(CDPPage)
        page.port = 9222
        page.target = "fake"
        page._id = 0
        page._events = []

        recorded_methods = []
        recorded_js = []

        def fake_call(method, recv_timeout=120, **params):
            recorded_methods.append((method, params))
            # Runtime.evaluate 返回 input 元素的 objectId
            # 注意结构：set_file_input 读 res.get("result", {}).get("objectId")
            if method == "Runtime.evaluate":
                return {"result": {"objectId": "fake-object-id"}}
            # DOM.enable / DOM.setFileInputFiles 等返回空 dict 即可
            return {}

        def fake_eval_js(js, await_promise=False, recv_timeout=120):
            recorded_js.append(js)
            return None

        page.call = fake_call
        page.eval_js = fake_eval_js

        # 调用 set_file_input
        dummy_file = tmp_path / "ref.png"
        dummy_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        page.set_file_input([str(dummy_file)])

        # 检查：不应出现 dispatchEvent / new Event 字样
        all_js = "\n".join(recorded_js)
        assert "dispatchEvent" not in all_js, (
            "set_file_input 后不应派发 dispatchEvent 事件"
            "（DOM.setFileInputFiles 已原生触发，重复派发导致图片重复上传）"
        )
        assert "new Event" not in all_js, (
            "set_file_input 后不应 new Event 派发事件"
        )

        # 应调用 DOM.setFileInputFiles 一次
        dom_calls = [m for m, _ in recorded_methods if m == "DOM.setFileInputFiles"]
        assert len(dom_calls) == 1, (
            f"应只调用 1 次 DOM.setFileInputFiles，实际 {len(dom_calls)} 次"
        )


# ──────────────────────────────────────────────────────────────
# send_confirmation 按钮优先 + allow_text=False 不发文本
# ──────────────────────────────────────────────────────────────

class TestSendConfirmationButtonFirst:
    """
    回归「参数卡重复确认多扣额度」bug：
    send_confirmation 应优先点按钮；allow_text=False 时绝不发文本。
    """

    def test_button_clicked_first_when_present(self):
        """有按钮时应优先点击，不调 fill_prompt。"""
        import doubao_gen
        page = FakeCDPPage(scripts=[
            # 第一次 eval_js 是按钮查找，返回 'button:确认生成'
            ("querySelector", "button:确认生成"),
        ])
        # 替换 fill_prompt 检测
        fill_called = [False]

        def fake_fill_prompt(p, text):
            fill_called[0] = True
            return True

        orig = doubao_gen.fill_prompt
        doubao_gen.fill_prompt = fake_fill_prompt
        try:
            result = doubao_gen.send_confirmation(page)
        finally:
            doubao_gen.fill_prompt = orig

        assert result.startswith("button:"), f"应返回 button 前缀，实际 {result}"
        assert not fill_called[0], "有按钮时不应调 fill_prompt"

    def test_allow_text_false_never_sends_text(self):
        """allow_text=False 时按钮失败也应返回 'none'，绝不发文本。"""
        import doubao_gen
        page = FakeCDPPage(scripts=[
            # 按钮不存在，返回 None
            ("querySelector", None),
        ])
        fill_called = [False]

        def fake_fill_prompt(p, text):
            fill_called[0] = True
            return True

        orig = doubao_gen.fill_prompt
        doubao_gen.fill_prompt = fake_fill_prompt
        try:
            result = doubao_gen.send_confirmation(page, allow_text=False)
        finally:
            doubao_gen.fill_prompt = orig

        assert result == "none", f"allow_text=False 时应返回 'none'，实际 {result}"
        assert not fill_called[0], (
            "allow_text=False 时绝不应调 fill_prompt 发文本（重复发文本会重复扣额度）"
        )


# ──────────────────────────────────────────────────────────────
# ensure_video_mode 已禁用
# ──────────────────────────────────────────────────────────────

def test_ensure_video_mode_docstring_says_disabled():
    """ensure_video_mode 的 docstring 必须含「已禁用」字样。"""
    import doubao_gen
    doc = doubao_gen.ensure_video_mode.__doc__ or ""
    assert "已禁用" in doc
