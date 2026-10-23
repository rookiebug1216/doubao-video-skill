# -*- coding: utf-8 -*-
"""
参考图上传门禁回归套件（不耗额度，默认跑）。

回归点（2026-10-23 实测 bug）：
  - 图片没上传成功就提交 → 豆包自动改纯文生成，人物与参考图脱钩、照样扣额度
  - set_file_input 在 React 水合未完成时会静默失败（files 仍为 0），必须复核
  - upload_images 必须以「输入区 blob: 缩略图数量 == 文件数」为完成判据
  - 上传失败必须中止本镜/本次生成，不允许退回纯文本模式
"""
import inspect
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

from shared import doubao_page, imagegen
from shared.cdp_browser import CDPPage
import doubao_gen


pytestmark = [pytest.mark.mock]


# ──────────────────────────────────────────────────────────────
# set_file_input 必须复核 files.length
# ──────────────────────────────────────────────────────────────

class _FileInputPage:
    """模拟 DOM.setFileInputFiles 后 input.files.length 的取值。"""

    def __init__(self, files_len):
        self.files_len = files_len
        self.calls = []

    def call(self, method, recv_timeout=120, **params):
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "obj-1"}}
        return {}

    def eval_js(self, js, await_promise=False, recv_timeout=120):
        return self.files_len


def test_set_file_input_true_only_when_files_attached(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert CDPPage.set_file_input(_FileInputPage(1), [str(f)]) is True


def test_set_file_input_false_when_injection_silently_lost(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert CDPPage.set_file_input(_FileInputPage(0), [str(f)]) is False


# ──────────────────────────────────────────────────────────────
# upload_images：就绪判定 / 重试 / 中止
# ──────────────────────────────────────────────────────────────

class _UploadPage:
    def __init__(self, inject_ok=True, states=None):
        self.inject_ok = inject_ok
        self.states = list(states or [])
        self.injections = 0

    def set_file_input(self, paths, css_selector='input[type="file"]'):
        self.injections += 1
        return self.inject_ok

    def eval_js(self, js, await_promise=False, recv_timeout=120):
        if not self.states:
            return json.dumps({"ready": 0, "uploading": False, "failed": False})
        st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return json.dumps(st)


def _mk_files(tmp_path, n):
    paths = []
    for i in range(n):
        p = tmp_path / f"ref_{i}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        paths.append(str(p))
    return paths


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr(doubao_page.time, "sleep", lambda s: None)


def test_upload_images_returns_true_when_all_thumbnails_ready(tmp_path):
    files = _mk_files(tmp_path, 3)
    page = _UploadPage(states=[
        {"ready": 0, "uploading": True, "failed": False},
        {"ready": 3, "uploading": False, "failed": False},
    ])
    assert doubao_page.upload_images(page, files, timeout=5) is True
    assert page.injections == 1


def test_upload_images_false_when_injection_never_takes(tmp_path):
    files = _mk_files(tmp_path, 1)
    page = _UploadPage(inject_ok=False)
    assert doubao_page.upload_images(page, files, timeout=5, retries=1) is False
    assert page.injections == 2, "注入未生效时应重试"


def test_upload_images_aborts_on_partial_and_does_not_reinject(tmp_path):
    """部分图已就绪时不得再次注入：重复附件会让 @图片N 编号错位。"""
    files = _mk_files(tmp_path, 3)
    page = _UploadPage(states=[
        {"ready": 0, "uploading": False, "failed": False},
        {"ready": 1, "uploading": False, "failed": False},
    ])
    assert doubao_page.upload_images(page, files, timeout=0) is False
    assert page.injections == 1


def test_upload_images_false_when_no_thumbnail_ever_appears(tmp_path):
    files = _mk_files(tmp_path, 1)
    page = _UploadPage(states=[{"ready": 0, "uploading": False, "failed": False}])
    assert doubao_page.upload_images(page, files, timeout=0, retries=1) is False
    assert page.injections == 2


def test_upload_images_ignores_leftover_thumbnails(tmp_path):
    """输入区残留上一轮缩略图时，不得据此判定本次上传已就绪。"""
    files = _mk_files(tmp_path, 2)
    page = _UploadPage(states=[
        {"ready": 2, "uploading": False, "failed": False},   # 基线：残留 2 张
        {"ready": 2, "uploading": False, "failed": False},   # 注入后仍是 2 张
    ])
    assert doubao_page.upload_images(page, files, timeout=0) is False


def test_upload_images_false_on_page_failure_flag(tmp_path):
    files = _mk_files(tmp_path, 1)
    page = _UploadPage(states=[{"ready": 0, "uploading": False, "failed": True}])
    assert doubao_page.upload_images(page, files, timeout=5) is False


def test_upload_images_skips_when_no_files(tmp_path):
    page = _UploadPage()
    assert doubao_page.upload_images(page, []) is True
    assert page.injections == 0


# ──────────────────────────────────────────────────────────────
# 调用点：上传门禁必须在提交之前，且禁止退回纯文本模式
# ──────────────────────────────────────────────────────────────

def test_generate_shot_gates_submit_on_upload_success():
    src = inspect.getsource(doubao_gen.generate_shot)
    assert "upload_images(page, ref_paths" in src
    assert src.index("upload_images(page, ref_paths") < src.index("submit_prompt(page)")
    assert "改用纯文本模式" not in src
    assert "不生成无参考的纯文视频" in src


def test_imagegen_gates_submit_on_upload_success():
    src = inspect.getsource(imagegen.generate_one)
    assert "upload_images(page, valid_refs" in src
    assert src.index("upload_images(page, valid_refs") < src.index("submit_prompt(page)")
    assert "改用纯文本模式" not in src


def test_wait_render_rejects_reference_lost_reply():
    """豆包回复读不到参考图时，本镜必须判失败，不能当成成功出片。"""
    src = inspect.getsource(doubao_gen.wait_render)
    assert "参考图资源当前无法读取" in src
    assert '"ref_lost"' in src or "'ref_lost'" in src


def test_generate_shot_handles_ref_lost_tag():
    src = inspect.getsource(doubao_gen.generate_shot)
    assert "ref_lost" in src


# ──────────────────────────────────────────────────────────────
# 多轮参数确认：豆包收到确认后再回一张新卡，必须继续确认
# ──────────────────────────────────────────────────────────────

def test_confirm_round_key_same_card_streaming_is_stable():
    """同一张卡流式更新（标题计数不变）→ 同一轮，不得重复确认。"""
    s1 = {"hasCard": True, "cardTitles": 1, "askCount": 0, "cardIdx": 100}
    s2 = {"hasCard": True, "cardTitles": 1, "askCount": 0, "cardIdx": 100}
    assert doubao_gen._confirm_round_key(s1) == doubao_gen._confirm_round_key(s2)


def test_confirm_round_key_second_card_is_new_round():
    """豆包再次回复一张新卡（标题计数 +1）→ 新轮次，必须再确认。"""
    first = {"hasCard": True, "cardTitles": 1, "askCount": 0, "cardIdx": 100}
    second = {"hasCard": True, "cardTitles": 2, "askCount": 1, "cardIdx": 4000}
    assert doubao_gen._confirm_round_key(first) != doubao_gen._confirm_round_key(second)


def test_confirm_round_key_falls_back_to_ask_then_params():
    ask_only = {"hasCard": True, "cardTitles": 0, "askCount": 1, "cardIdx": -1}
    assert doubao_gen._confirm_round_key(ask_only) == ("ask", 1)
    params_only = {"hasCard": True, "cardTitles": 0, "askCount": 0, "cardIdx": -1,
                   "card": "模型： Seedance 2.0 Mini 时长： 14 秒 比例： 16:9 横屏"}
    assert doubao_gen._confirm_round_key(params_only)[0] == "fp"


def test_wait_render_has_no_one_shot_confirm_lock():
    """回归：首次确认后一锁到底会让第二张卡永远不被确认（2026-10-23 实锤）。"""
    src = inspect.getsource(doubao_gen.wait_render)
    assert "confirmed_rounds" in src
    assert "shot_confirmed" not in src
    assert "confirm_count < 3" in src


def test_reply_state_reports_round_counters():
    """REPLY_STATE_JS 必须上报 cardTitles / askCount，多轮确认才有依据。"""
    assert "cardTitles" in doubao_gen.REPLY_STATE_JS
    assert "askCount" in doubao_gen.REPLY_STATE_JS
