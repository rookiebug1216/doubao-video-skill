# -*- coding: utf-8 -*-
"""
media_unlock 已知 bug 回归套件（不耗额度，默认跑）。

回归点：
  - allow_history=False 时 _UNLOCK_JS 中 resolveLatest 默认不读 history 池
  - _UNLOCK_JS 含 chain/history 分类逻辑
  - _QAAB_SALT_HEX 长度/格式正确
  - _looks_like_mp4 文件头校验
"""
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import media_unlock


pytestmark = [pytest.mark.mock]


# ──────────────────────────────────────────────────────────────
# _UNLOCK_JS 内容回归
# ──────────────────────────────────────────────────────────────

class TestUnlockJs:
    def test_qaab_salt_hex_length(self):
        """_QAAB_SALT_HEX 长度必须为 128（64 字节的十六进制）。"""
        assert len(media_unlock._QAAB_SALT_HEX) == 128

    def test_qaab_salt_hex_is_hex(self):
        """_QAAB_SALT_HEX 必须为纯十六进制。"""
        int(media_unlock._QAAB_SALT_HEX, 16)   # 抛异常即非 hex

    def test_unlock_js_salt_replaced(self):
        """_UNLOCK_JS 中 __SALT__ 占位符已被替换为实际盐值。"""
        assert "__SALT__" not in media_unlock._UNLOCK_JS, "盐占位符未被替换"
        # 实际盐值字符串应在 _UNLOCK_JS 中
        assert media_unlock._QAAB_SALT_HEX in media_unlock._UNLOCK_JS

    def test_unlock_js_has_resolve_latest(self):
        """_UNLOCK_JS 含 resolveLatest 方法。"""
        assert "resolveLatest" in media_unlock._UNLOCK_JS

    def test_unlock_js_has_via_fallback_api(self):
        """_UNLOCK_JS 含 viaFallbackApi 方法。"""
        assert "viaFallbackApi" in media_unlock._UNLOCK_JS

    def test_unlock_js_has_classify_source(self):
        """_UNLOCK_JS 含 chain/history 分类逻辑。"""
        assert "/im/chain/" in media_unlock._UNLOCK_JS
        assert "/im/conversation/" in media_unlock._UNLOCK_JS

    def test_unlock_js_classify_default_excludes_history(self):
        """
        回归「history fallback_api 旧片污染」bug：
        resolveLatest 默认从 chain 池取，history 永不选用。
        """
        # _UNLOCK_JS 中应有 allowHistory 参数控制
        assert "allowHistory" in media_unlock._UNLOCK_JS, (
            "_UNLOCK_JS 缺 allowHistory 参数（history 池控制开关）"
        )
        # 默认（allowHistory=False）下 pool 应为 chain-only：
        # 检查 JS 中默认走 chain 池的逻辑
        assert "r.src === 'chain'" in media_unlock._UNLOCK_JS, (
            "_UNLOCK_JS 缺「records.filter(r => r.src === 'chain')」逻辑"
        )


# ──────────────────────────────────────────────────────────────
# Python 封装层
# ──────────────────────────────────────────────────────────────

class TestPythonApi:
    def test_resolve_unwatermarked_default_allow_history_false(self):
        """
        回归：resolve_unwatermarked 默认 allow_history=False，
        传入 JS 的参数应为 "{}"（不含 allowHistory: true）。
        """
        import inspect
        sig = inspect.signature(media_unlock.resolve_unwatermarked)
        ah_param = sig.parameters.get("allow_history")
        assert ah_param is not None, "resolve_unwatermarked 缺 allow_history 参数"
        assert ah_param.default is False, (
            "allow_history 默认值应为 False（永不选用 history 池的旧片）"
        )

    def test_download_unwatermarked_default_allow_history_false(self):
        import inspect
        sig = inspect.signature(media_unlock.download_unwatermarked_video)
        ah_param = sig.parameters.get("allow_history")
        assert ah_param is not None
        assert ah_param.default is False


# ──────────────────────────────────────────────────────────────
# _looks_like_mp4 文件头校验
# ──────────────────────────────────────────────────────────────

class TestLooksLikeMp4:
    def test_valid_ftyp_header(self):
        """含 ftyp 头且 > 10KB 的字节流应为 mp4。"""
        data = b"\x00\x00\x00 ftypisom" + b"\x00" * (11 * 1024)
        assert media_unlock._looks_like_mp4(data)

    def test_too_small_data(self):
        """< 10KB 不算 mp4。"""
        data = b"\x00\x00\x00 ftypisom" + b"\x00" * 100
        assert not media_unlock._looks_like_mp4(data)

    def test_wrong_header(self):
        """头不是 ftyp 不算 mp4。"""
        data = b"\x00\x00\x00 xxxx" + b"\x00" * (11 * 1024)
        assert not media_unlock._looks_like_mp4(data)

    def test_empty_data(self):
        assert not media_unlock._looks_like_mp4(b"")
