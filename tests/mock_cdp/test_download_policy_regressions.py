# -*- coding: utf-8 -*-
"""
媒体下载策略回归套件（不耗额度，默认跑）。

回归点：
  - 下载链路只有「无水印原图/原片」与「带水印页面资源」两档：
    截图兜底、页面按钮点击兜底（会误下豆包 PC 安装包）均已删除
  - 图片下载无 unlock 语义（无 unlock 参数）
  - CDP 响应按 requestId→URL 映射匹配，不会被无关请求的 loadingFinished 抢走
  - 2026-10-24 缩略图事故：384x216 缩略图不得落盘为产物（分辨率守卫）
  - 2026-10-24 image_ori 活源：_ROUTER_DATA 提取必须覆盖 image_ori（image_raw 已恒 null）
  - 2026-10-24 视频无水印默认开启，--no-unlock 退回
"""
import base64
import inspect
import json
import os
import struct
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# skills/doubao-manju-video 目录含连字符，直接把 scripts 目录加入 sys.path 后 import 顶层模块
_SCRIPTS_DIR = os.path.join(_ROOT, "skills", "doubao-manju-video", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from shared import imagegen
from shared.cdp_browser import CDPPage, _url_base, _url_matches
import doubao_gen


pytestmark = [pytest.mark.mock]

_IMAGEGEN_PATH = os.path.join(_ROOT, "shared", "imagegen.py")
_DOUBAO_GEN_PATH = os.path.join(_SCRIPTS_DIR, "doubao_gen.py")
_CDP_BROWSER_PATH = os.path.join(_ROOT, "shared", "cdp_browser.py")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ──────────────────────────────────────────────────────────────
# 静态回归：已删除的兜底手段不得回流
# ──────────────────────────────────────────────────────────────

class TestDownloadPolicyStatic:
    """源码层面锁定两档下载策略，防止截图/按钮点击兜底被重新加回。"""

    @pytest.mark.parametrize("path", [_IMAGEGEN_PATH, _DOUBAO_GEN_PATH])
    @pytest.mark.parametrize("forbidden", [
        "screenshot_clip",
        "DOWNLOAD_TRIGGERED",
        "js_dl",
        "canvas.toDataURL",
        "Page.captureScreenshot",
    ])
    def test_no_removed_download_fallbacks(self, path, forbidden):
        src = _read(path)
        assert forbidden not in src, (
            f"{os.path.basename(path)} 中仍存在已删除的下载兜底标记 {forbidden!r}"
            "（截图糊人物 / 按钮点击误下安装包）"
        )

    def test_cdp_browser_has_no_screenshot_clip(self):
        """screenshot_clip 已无调用，定义也应删除；全页 screenshot 保留供诊断快照使用。"""
        src = _read(_CDP_BROWSER_PATH)
        assert "screenshot_clip" not in src
        assert "def screenshot(" in src, "诊断快照用的全页 screenshot 不应被删除"

    @pytest.mark.parametrize("func", [imagegen.download_image, imagegen._download_image_impl])
    def test_image_download_has_no_unlock_param(self, func):
        """图片下载无 unlock 语义：默认就优先走无水印原图。"""
        params = inspect.signature(func).parameters
        assert "unlock" not in params, f"{func.__name__} 不应再有 unlock 参数"

    def test_download_video_has_no_button_click_fallback(self):
        """download_video 只读取页面视频资源，不得点击页面下载按钮。"""
        src = inspect.getsource(doubao_gen.download_video)
        for pattern in ("[data-action", "querySelectorAll('button", ".click(", "innerText"):
            assert pattern not in src, (
                f"download_video 函数体仍含按钮点击特征 {pattern!r}"
                "（可能误下豆包 PC 安装包）"
            )

    def test_download_video_keeps_two_tier_logs(self):
        """两档策略日志保持不变：无水印原片 + 带水印页面资源。"""
        src = inspect.getsource(doubao_gen.download_video)
        assert "已下载无水印原片" in src
        assert "已下载带水印页面资源（https 直链）" in src
        assert "已下载带水印页面资源（blob fetch）" in src

    @pytest.mark.parametrize("func", [CDPPage.download_native, CDPPage.download_raw])
    def test_cdp_download_matches_url(self, func):
        """只接受目标 URL 的 loadingFinished，不取第一个到达的响应。"""
        src = inspect.getsource(func)
        assert "_url_matches" in src
        assert "if rid in matched or _url_matches" in src


# ──────────────────────────────────────────────────────────────
# _url_base / _url_matches 单元回归
# ──────────────────────────────────────────────────────────────

class TestUrlMatching:
    def test_url_base_strips_query_and_thumbnail_suffix(self):
        assert _url_base("https://cdn.example/a.png?x=1") == "https://cdn.example/a.png"
        assert _url_base("https://cdn.example/a.png~tplv-abc") == "https://cdn.example/a.png"
        assert _url_base("") == ""

    def test_url_matches_same_and_cdn_variant(self):
        target = "https://cdn.example/rc_gen_image/abc"
        assert _url_matches(target, target)
        assert _url_matches(target, target + "~tplv-resize:0:0")
        assert _url_matches(target, target + "?x-signature=1")

    def test_url_matches_rejects_unrelated(self):
        assert not _url_matches("https://cdn.example/a.png", "https://cdn.example/b.png")
        assert not _url_matches("", "https://cdn.example/a.png")
        assert not _url_matches("https://cdn.example/a.png", "")


# ──────────────────────────────────────────────────────────────
# 动态回归：无关请求先到，也不能被当作目标响应
# ──────────────────────────────────────────────────────────────

_TARGET_URL = "https://cdn.example/rc_gen_image/target"
_UNRELATED_URL = "https://cdn.example/static/unrelated.js"


class ScriptedEventPage:
    """极简 fake page：模拟 Network 事件流与 getResponseBody 响应。"""

    def __init__(self, events, bodies):
        self._events = []
        self._pending = list(events)
        self._bodies = dict(bodies)
        self.body_requests = []
        self.calls = []

    def call(self, method, recv_timeout=120, **params):
        self.calls.append((method, params))
        if method == "Network.getResponseBody":
            rid = params.get("requestId")
            self.body_requests.append(rid)
            raw = self._bodies.get(rid, b"")
            return {"body": base64.b64encode(raw).decode(), "base64Encoded": True}
        return {}

    def eval_js(self, js, await_promise=False, recv_timeout=120):
        self.calls.append(("Runtime.evaluate", {}))
        return None

    def pop_events(self, method=None):
        if method:
            matched = [e for e in self._events if e.get("method") == method]
            self._events = [e for e in self._events if e.get("method") != method]
            return matched
        events, self._events = self._events, []
        return events

    def drain_events(self, timeout=0.3):
        if self._pending:
            self._events.extend(self._pending)
            self._pending = []


def _event_flow():
    """先到无关请求 A（含 loadingFinished），再到目标请求 B。"""
    return [
        {"method": "Network.requestWillBeSent",
         "params": {"requestId": "A", "request": {"url": _UNRELATED_URL}}},
        {"method": "Network.loadingFinished", "params": {"requestId": "A"}},
        {"method": "Network.requestWillBeSent",
         "params": {"requestId": "B", "request": {"url": _TARGET_URL}}},
        {"method": "Network.loadingFinished", "params": {"requestId": "B"}},
    ]


def _png_bytes(size=120 * 1024):
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)


def _mp4_bytes(size=12 * 1024):
    return b"\x00\x00\x00\x18ftyp" + b"\x00" * (size - 8)


class TestCdpDownloadUrlMatching:
    def test_download_native_uses_matched_request_only(self, tmp_path):
        """无关注册请求 A 的 loadingFinished 不能被采用。"""
        dest = tmp_path / "img.png"
        page = ScriptedEventPage(
            _event_flow(),
            {"A": b"WRONG" * 2048, "B": _png_bytes()},
        )

        assert CDPPage.download_native(page, _TARGET_URL, str(dest), timeout=5) is True

        assert dest.read_bytes() == _png_bytes()
        assert page.body_requests == ["B"], f"只应取目标请求的响应体，实际 {page.body_requests}"
        assert imagegen._is_image_file(str(dest))

    def test_download_raw_uses_matched_request_only(self, tmp_path):
        """new Image 路径同样按 requestId→URL 映射挑响应。"""
        dest = tmp_path / "clip.mp4"
        page = ScriptedEventPage(
            _event_flow(),
            {"A": b"WRONG" * 2048, "B": _mp4_bytes()},
        )

        assert CDPPage.download_raw(page, _TARGET_URL, str(dest), timeout=5) is True

        assert dest.read_bytes() == _mp4_bytes()
        assert page.body_requests == ["B"], f"只应取目标请求的响应体，实际 {page.body_requests}"
        assert doubao_gen._is_mp4_file(str(dest))


# ──────────────────────────────────────────────────────────────
# 2026-10-24 缩略图事故回归：分辨率守卫
# ──────────────────────────────────────────────────────────────

def _png_with_dims(w: int, h: int, size: int = 200 * 1024) -> bytes:
    """构造指定宽高的 PNG 字节（签名 + IHDR + padding）。"""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x09" + b"IHDR"
            + struct.pack(">II", w, h) + b"\x00" * max(0, size - 24))


class TestImageHiResGuard:
    """断刃黄昏 shot_04/05 事故：384x216 缩略图（105KB）通过旧版体积校验
    落盘并被误报为「无水印原图下载成功」。分辨率守卫必须拦住。"""

    def test_guard_rejects_thumbnail(self, tmp_path):
        p = tmp_path / "thumb.png"
        p.write_bytes(_png_with_dims(384, 216))
        assert imagegen._is_image_file(str(p)), "回归背景：旧校验确实拦不住缩略图"
        assert not imagegen._is_image_file_hires(str(p)), "384x216 缩略图必须被守卫拦截"

    def test_guard_rejects_square_icon(self, tmp_path):
        p = tmp_path / "icon.png"
        p.write_bytes(_png_with_dims(320, 320))
        assert not imagegen._is_image_file_hires(str(p)), "320x320 图标必须被守卫拦截"

    def test_guard_accepts_landscape_original(self, tmp_path):
        p = tmp_path / "ori.png"
        p.write_bytes(_png_with_dims(2720, 1520))
        assert imagegen._is_image_file_hires(str(p))

    def test_guard_accepts_portrait_original(self, tmp_path):
        """最长边判据不歧视竖版原图（1520x2720）。"""
        p = tmp_path / "ori_v.png"
        p.write_bytes(_png_with_dims(1520, 2720))
        assert imagegen._is_image_file_hires(str(p))

    def test_guard_rejects_non_image(self, tmp_path):
        p = tmp_path / "fake.png"
        p.write_bytes(b"\x00" * 200 * 1024)
        assert not imagegen._is_image_file_hires(str(p))

    def test_img_dimensions_png(self, tmp_path):
        p = tmp_path / "d.png"
        p.write_bytes(_png_with_dims(2848, 1600))
        assert imagegen._img_dimensions(str(p)) == (2848, 1600)


class TestImageDownloadSourceLocks:
    """源码锁：下载主流程必须全程使用分辨率守卫与 image_ori 活源。"""

    def test_impl_uses_hires_guard_only(self):
        src = inspect.getsource(imagegen._download_image_impl)
        assert "_is_image_file_hires" in src
        assert "_is_image_file(filepath)" not in src, \
            "下载主流程不得再使用无分辨率守卫的旧校验"

    def test_router_extracts_image_ori(self):
        """_ROUTER_DATA 提取必须覆盖 image_ori 活源（image_raw 已恒 null）。"""
        src = inspect.getsource(imagegen._router_ori_urls)
        assert "image_ori" in src
        assert "qvalue" in src, "必须排除 image-qvalue UI 图标"

    def test_reload_session_uses_navigate(self):
        """重导航必须用 Page.navigate 到原 URL（Page.reload 会丢 SPA 会话路由）。"""
        import re as _re
        src = inspect.getsource(imagegen._reload_session)
        src = _re.sub(r'"""(?:.|\n)*?"""', "", src, count=1)  # 去 docstring 防误伤
        assert "Page.navigate" in src
        assert "Page.reload" not in src

    def test_own_src_not_first_candidate(self):
        """wait_image 返回的 src（可能是缩略图）不得排候选首位——
        缩略图直链下载成功率最高，排首位会抢在 image_ori 之前落盘。"""
        src = inspect.getsource(imagegen._download_image_impl)
        first_block = src.split("# ②")[0]
        assert "own_fid" in first_block, "候选构建必须先按文件 ID 提取 image_ori"
        assert "candidates.append(url)" in first_block
        assert "candidates.append(own_src)" not in first_block


class _FakeRouterPage:
    """fake page：只响应 _ROUTER_DATA 与页面最大图两类 eval_js。"""

    def __init__(self, router_obj, largest_src=""):
        self._router = router_obj
        self._largest = largest_src

    def eval_js(self, js, **kw):
        if "window._ROUTER_DATA||null" in js:
            return json.dumps(self._router) if self._router is not None else "null"
        if "rc_gen_image" in js:
            return self._largest
        return None


class TestRouterOriUrls:
    """image_ori 提取单测（2026-10-24 实测的豆包响应形态）。"""

    _ORI_URL = ("https://p6-flow-imagex-sign.byteimg.com/tos-cn-i-a9rns2rl98/"
                "rc_gen_image/abc123def456.png~tplv-a9rns2rl98-image.png?x-signature=s1")

    def test_extracts_image_ori_object_form(self):
        """image_ori 为 {url: ...} 对象形态。"""
        page = _FakeRouterPage(
            {"loaderData": {"chat": {"creations": [{"image": {
                "image_ori": {"url": self._ORI_URL},
                "url": "https://cdn.example/thumb.jpeg~tplv-cthumb_wm0",
            }}]}}})
        assert imagegen._router_ori_urls(page, "abc123def456") == [self._ORI_URL]

    def test_excludes_icon_and_avatar(self):
        """同 ID 命中 qvalue 图标 / 头像路径必须排除。"""
        page = _FakeRouterPage(
            {"a": {"image_ori": {"url": "https://cdn.example/image-qvalue/abc123def456.png"}},
             "b": {"image_ori": {"url": "https://cdn.example/user-avatar/abc123def456.png"}},
             "c": {"image_ori": {"url": self._ORI_URL}}})
        assert imagegen._router_ori_urls(page, "abc123def456") == [self._ORI_URL]

    def test_no_file_id_match_returns_empty(self):
        page = _FakeRouterPage({"a": {"image_ori": {"url": self._ORI_URL}}})
        assert imagegen._router_ori_urls(page, "otherid99999") == []

    def test_null_router_returns_empty(self):
        assert imagegen._router_ori_urls(_FakeRouterPage(None), "abc") == []


class TestVideoUnlockDefaultOn:
    """2026-10-24 决策：视频无水印默认开启，--no-unlock 退回（翻转旧 opt-in）。"""

    def test_no_unlock_flag_replaces_unlock(self):
        src = _read(_DOUBAO_GEN_PATH)
        assert '"--no-unlock"' in src
        assert "args.no_unlock" in src
        assert '"--unlock"' not in src, "旧 opt-in 开关 --unlock 不得回流"

    def test_arm_not_gated_by_optin(self):
        src = _read(_DOUBAO_GEN_PATH)
        assert "if not args.no_unlock:" in src, "钩子安装必须默认执行"
