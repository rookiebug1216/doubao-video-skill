# -*- coding: utf-8 -*-
"""
任务G（2026-10-24）：事件驱动确认状态机回归锁。

背景（用户指正 + 镜05 会话 CDP 取证实锤）：
  - 旧 5s 轮询读 body.innerText 瞬时快照，确认失效后后台空等 900s
  - innerText 累计计数（cardTitles/askCount）在豆包虚拟化卸载/回载下漂移，
    轮次键碰撞 → 二次确认新卡被判旧卡 → 永不确认 → 卡死
    （实证：会话完成后 innerText 仅 1114 字符，卡片计数=0，确认消息气泡也被卸载）
  - 确认消息发送后无送达验证，submit 假成功 → 永不重试

本文件锁定：
  不变量（state-machine-correctness）：
    N1 同一轮次绝不重复确认（卡片 hash + 时序锚点双判据）
    N2 未验证送达绝不锁定轮次
    N3 绝不确认本镜提示词之前的历史旧卡（N3 基线预锁）
    N4 确认总数 <= 3 轮
  反例用例：
    R1 豆包二次回参数相同的新卡（锚点后新元素）→ 必须确认
    R2 同一张卡流式更新（同元素）→ 绝不重复确认
    R3 跨镜残留旧卡 + anchor=None → fallback 指纹判据 + N3 基线拦截
    R4 无卡直生成（镜05 实锤形态）→ 不误触发确认
    R5 anchor 被虚拟化卸载 → 参数指纹 fallback 仍工作
"""

import inspect
import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for p in (_ROOT, os.path.join(_ROOT, "skills", "doubao-manju-video", "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import doubao_gen
from shared import doubao_events


# ──────────────────────────────────────────────────────────────
# 测试辅助：构造快照消息
# ──────────────────────────────────────────────────────────────

def _msg(i, role, text, h=None, card=False, ask=False):
    return {"i": i, "r": role, "h": h or f"h{i}", "n": len(text),
            "card": card, "ask": ask, "text": text}


CARD_TEXT = "视频生成参数确认\n模型: Seedance 2.0 Mini\n时长: 10\n比例: 16:9\n风格: CG写实\n确认后我再开始生成视频"
CARD_TEXT_2 = "视频生成参数确认\n模型: Seedance 2.0 Mini\n时长: 10\n比例: 16:9\n风格: CG写实3D\n确认后我再开始生成视频"


def _card_fingerprint_of(text):
    return doubao_gen._card_fingerprint(text)


# ──────────────────────────────────────────────────────────────
# 常量与架构锁
# ──────────────────────────────────────────────────────────────

class TestEventDrivenArchitecture:
    """事件驱动架构必须就位（替代轮询的机制保证）。"""

    def test_watchdog_constants(self):
        """三层看门狗常量：180s 无进展告警 / 30s 事件窗口 / 600s 总超时。"""
        assert doubao_gen.WATCHDOG_IDLE == 180
        assert doubao_gen.EVENT_WINDOW == 30
        assert doubao_gen.RENDER_TIMEOUT == 600

    def test_poll_interval_only_for_fallback(self):
        """POLL_INTERVAL 仅降级模式使用：主循环尾的事件模式不 sleep。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "if not event_armed:" in src and "time.sleep(POLL_INTERVAL)" in src
        # 事件模式的节奏 = wait_snapshot 阻塞等待
        assert "doubao_events.wait_snapshot" in src

    def test_observer_kernel_wired(self):
        """wait_render 必须接入事件内核：arm / wait_snapshot / poll_snapshot / is_alive。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "doubao_events.arm(page)" in src
        assert "doubao_events.poll_snapshot" in src
        assert "doubao_events.is_alive" in src

    def test_fallback_to_polling_on_dead_observer(self):
        """观察器失活必须自动降级轮询（不允许全盲）。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "event_armed = False" in src
        assert "降级" in src

    def test_wait_event_primitive_exists(self):
        """CDPPage.wait_event 事件等待原语必须存在（阻塞等指定事件）。"""
        assert hasattr(doubao_gen.CDPPage, "wait_event")

    def test_observer_js_core_mechanisms(self):
        """观察器脚本必须含 MutationObserver / debounce / 指纹去重 / 心跳。"""
        js = doubao_events._OBSERVER_JS
        assert "MutationObserver" in js
        assert "setTimeout" in js, "debounce 必须有"
        assert "lastSig" in js, "指纹去重必须有（无实质变化不推送）"
        assert "__doubaoBeat" in js, "心跳必须有（判活依据）"
        assert "message-list" in js and "union_message" in js, "必须锚定消息列表 DOM"

    def test_state_from_snapshot_exists(self):
        """快照→兼容 state 的构造器必须存在。"""
        assert callable(doubao_gen._state_from_snapshot)


# ──────────────────────────────────────────────────────────────
# _state_from_snapshot 构造正确性
# ──────────────────────────────────────────────────────────────

class TestStateFromSnapshot:

    def test_card_detected(self):
        snap = {"msgs": [_msg(0, "U", "生成视频"), _msg(1, "B", CARD_TEXT, card=True, ask=True)],
                "videos": []}
        st = doubao_gen._state_from_snapshot(snap)
        assert st["hasCard"] is True
        assert "视频生成参数确认" in st["card"]
        assert st["msgs"] == snap["msgs"]

    def test_no_card_direct_generation(self):
        """R4 无卡直生成（镜05 实锤形态）：不误报 hasCard。"""
        snap = {"msgs": [_msg(0, "U", "提示词"),
                         _msg(1, "B", "本次使用 Seedance 2.0 Mini 生成，预计等待 5 分钟。")],
                "videos": []}
        st = doubao_gen._state_from_snapshot(snap)
        assert st["hasCard"] is False
        assert st["card"] == ""
        assert st["replyTail"].startswith("本次使用")

    def test_quota_error_collected(self):
        snap = {"msgs": [_msg(0, "B", "今天的免费次数已用完，明天再来吧")], "videos": []}
        st = doubao_gen._state_from_snapshot(snap)
        assert any("免费次数已用完" in e for e in st["errors"])

    def test_paid_prompt_not_error(self):
        """付费询问（有按钮）绝不能进 errors —— 与 REPLY_STATE_JS 规则一致。"""
        paid = "今日免费额度已用完，本次将消耗付费额度。是否继续生成？"
        snap = {"msgs": [_msg(0, "B", paid)], "videos": []}
        st = doubao_gen._state_from_snapshot(snap)
        assert st["paidPrompt"] is True
        assert st["errors"] == []


# ──────────────────────────────────────────────────────────────
# _find_pending_card 锚点判据（不变量 N1/N3 + 反例 R1/R2/R3/R5）
# ──────────────────────────────────────────────────────────────

class TestFindPendingCard:

    def test_new_card_after_anchor_confirmed(self):
        """R1 二次确认：anchor（确认消息 i=2）之后的新卡（i=3）必须待确认。"""
        state = {"msgs": [
            _msg(0, "U", "提示词"),
            _msg(1, "B", CARD_TEXT, h="card1", card=True, ask=True),
            _msg(2, "U", "确认，开始生成视频", h="anchor"),
            _msg(3, "B", CARD_TEXT_2, h="card2", card=True, ask=True),
        ]}
        pending = doubao_gen._find_pending_card(state, "anchor", {"card1"}, {"fp1"})
        assert pending is not None and pending["h"] == "card2"

    def test_same_card_streaming_update_not_reconfirmed(self):
        """R2 同一张卡流式更新（i 不变、仍在 anchor 前）→ 绝不重复确认（N1）。"""
        state = {"msgs": [
            _msg(0, "U", "提示词"),
            _msg(1, "B", CARD_TEXT, h="card1", card=True, ask=True),
            _msg(2, "U", "确认，开始生成视频", h="anchor"),
        ]}
        # card1 已确认过：即使它继续流式更新（文本变化导致 hash 变）也不该再确认
        state2 = {"msgs": [
            _msg(0, "U", "提示词"),
            _msg(1, "B", CARD_TEXT + "（补充说明）", h="card1b", card=True, ask=True),
            _msg(2, "U", "确认，开始生成视频", h="anchor"),
        ]}
        # 已确认 hash 集合含 card1；card1b 是同元素流式更新（i=1 < anchor i=2）
        pending = doubao_gen._find_pending_card(state2, "anchor", {"card1"}, {"fp1"})
        assert pending is None, "anchor 之前的卡（含流式更新）绝不重复确认"

    def test_old_card_before_anchor_never_confirmed(self):
        """N3 核心：anchor 之前的卡（历史残留）绝不确认 —— 即使从未见过其 hash。"""
        state = {"msgs": [
            _msg(0, "B", CARD_TEXT, h="old_card", card=True, ask=True),  # 上一镜残留
            _msg(1, "U", "本镜提示词"),
            _msg(2, "U", "确认，开始生成视频", h="anchor"),  # 上一镜的确认
        ]}
        pending = doubao_gen._find_pending_card(state, "anchor", set(), set())
        assert pending is None

    def test_n3_baseline_prelock_semantics(self):
        """R3 跨镜场景：N3 基线把提示词之前的旧卡预先锁进 confirmed_card_hashes，
        锚点判据直接跳过 —— wait_render 源码必须实现该预锁。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "N3 基线锁定" in src
        assert "_prompt_idx" in src
        assert "confirmed_card_hashes.add" in src

    def test_anchor_missing_fallback_fingerprint(self):
        """R5 anchor 被虚拟化卸载：参数指纹 fallback —— 未见过的指纹算新卡。"""
        state = {"msgs": [
            _msg(0, "U", "提示词"),
            _msg(1, "B", CARD_TEXT_2, h="card2", card=True, ask=True),
        ]}
        # anchor_hash 不在当前 msgs（被卸载）→ fallback
        pending = doubao_gen._find_pending_card(state, "gone_anchor", set(), {"fp_seen"})
        # card2 指纹与已见指纹不同 → 待确认
        assert pending is not None

    def test_anchor_missing_fallback_same_fingerprint_skipped(self):
        """R5b anchor 卸载 + 参数指纹已见过（同参数旧卡）→ 不确认（保守正确）。"""
        state = {"msgs": [
            _msg(0, "U", "提示词"),
            _msg(1, "B", CARD_TEXT, h="card_old", card=True, ask=True),
        ]}
        fp = _card_fingerprint_of(CARD_TEXT)
        pending = doubao_gen._find_pending_card(state, "gone_anchor", set(), {fp})
        assert pending is None

    def test_no_card_returns_none(self):
        state = {"msgs": [_msg(0, "U", "提示词"), _msg(1, "B", "开始生成")]}
        assert doubao_gen._find_pending_card(state, None, set(), set()) is None

    def test_confirm_count_cap_in_source(self):
        """N4：确认上限 3 轮必须在主流程源码里。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "confirm_count < 3" in src


# ──────────────────────────────────────────────────────────────
# 送达验证（不变量 N2/L2）
# ──────────────────────────────────────────────────────────────

class TestDeliveryVerification:

    def test_verified_send_exists(self):
        """send_confirmation_verified 必须存在且实现送达验证。"""
        assert callable(doubao_gen.send_confirmation_verified)
        src = inspect.getsource(doubao_gen.send_confirmation_verified)
        assert "wait_user_msg_confirm" in src, "文本路径必须验证新 U 消息出现"
        assert "wait_snapshot" in src, "按钮路径必须等待实质状态迁移"

    def test_lock_only_after_delivered(self):
        """N2：锁定（confirmed_card_hashes.add / confirmed_rounds.add）必须且只能在
        delivered 之后 —— 源码顺序锁。"""
        src = inspect.getsource(doubao_gen.wait_render)
        # 事件模式锁定点
        lock_pos = src.find("confirmed_card_hashes.add(pending.get")
        delivered_pos = src.find("action != \"none\" and delivered")
        assert lock_pos > 0 and delivered_pos > 0
        assert lock_pos > delivered_pos, "锁定必须在送达验证通过之后（N2）"
        # 未送达分支绝不锁定（按钮防重锁除外——按钮已实际点击）
        not_delivered_pos = src.find("not delivered")
        assert not_delivered_pos > 0
        # 降级模式锁定点同样在 delivered 之后
        round_add = src.find("confirmed_rounds.add(round_key)")
        assert round_add > delivered_pos

    def test_delivery_fail_retry_cap(self):
        """送达验证失败必须有独立重试上限（5 次），防无限重发多扣额度。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "confirm_fail >= 5" in src
        assert "防重复发送" in src or "多扣额度" in src

    def test_wait_user_msg_confirm_impl(self):
        """wait_user_msg_confirm：事件+poll 双通道，10s 默认超时（L2）。"""
        sig = inspect.signature(doubao_events.wait_user_msg_confirm)
        assert sig.parameters["timeout"].default == 10.0
        src = inspect.getsource(doubao_events.wait_user_msg_confirm)
        assert "wait_snapshot" in src and "poll_snapshot" in src


# ──────────────────────────────────────────────────────────────
# 看门狗（L4/L5）
# ──────────────────────────────────────────────────────────────

class TestWatchdog:

    def test_watchdog_warn_and_exit_in_source(self):
        """L4 无进展告警 + L5 3 轮确认显式退出必须在主循环源码里。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "WATCHDOG_IDLE" in src
        assert "看门狗" in src
        assert "显式退出本镜" in src

    def test_heartbeat_refreshes_progress(self):
        """心跳新鲜（DOM 在动，如渲染 spinner）必须刷新 last_progress，
        避免正常渲染期误告警。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "心跳新鲜" in src
        assert "last_progress = time.time()" in src


# ──────────────────────────────────────────────────────────────
# E2E 实测加固锁（2026-10-24 镜6/7 实机验证暴露的三处缺陷）
# ──────────────────────────────────────────────────────────────

class TestE2eHardening:
    """镜6/7 E2E（双镜无水印成片、5 轮确认全部自动处理）暴露的边缘缺陷。"""

    def test_wait_user_msg_confirm_count_mode(self):
        """同文本 hash 碰撞修复：wait_user_msg_confirm 必须支持计数判新
        （known_count 参数）。镜7 实锤：两轮确认文本完全相同（「确认，开始
        生成视频」），djb2 按文本哈希必碰撞，hash 排除法把第二条同文本
        消息误判为旧消息 → 送达验证假失败 → 多发确认。"""
        sig = inspect.signature(doubao_events.wait_user_msg_confirm)
        assert "known_count" in sig.parameters, (
            "wait_user_msg_confirm 缺 known_count 参数（计数判新，"
            "2026-10-24 E2E 镜7 同文本 hash 碰撞实锤）"
        )
        src = inspect.getsource(doubao_events.wait_user_msg_confirm)
        assert "known_count >= 0" in src, "计数判新分支缺失"
        assert "len(matches) > known_count" in src, "计数比较逻辑缺失"

    def test_send_confirmation_verified_passes_known_count(self):
        """send_confirmation_verified 必须采集计数基线并传入
        wait_user_msg_confirm（否则计数判新不生效）。"""
        src = inspect.getsource(doubao_gen.send_confirmation_verified)
        assert "known_count = sum(" in src, "缺计数基线采集"
        assert "known_count=known_count" in src, "未把 known_count 传入验证函数"

    def test_degraded_mode_fingerprint_dedup(self):
        """降级模式同卡指纹去重（N1 补全）：镜6 实锤 innerText 计数漂移把
        豆包回放的同一张卡判为新轮次 → 多发一条确认（豆包幂等兜住才没
        多扣额度）。降级分支 is_new_round 必须排除已确认参数指纹，
        确认成功后必须同步锁指纹。"""
        src = inspect.getsource(doubao_gen.wait_render)
        assert "_card_fingerprint(card_text) not in confirmed_params" in src, (
            "降级分支 is_new_round 必须排除已确认指纹"
        )
        # 降级确认成功后锁指纹（去重的另一半）
        assert "confirmed_rounds.add(round_key)" in src
        lock_pos = src.find("confirmed_rounds.add(round_key)")
        fp_pos = src.find("降级模式同步锁指纹")
        assert fp_pos > lock_pos > 0, "降级确认成功后必须同步锁 confirmed_params"

    def test_project_root_autodetect_from_storyboard(self):
        """project-root 自动推导：直接传 shots.json 不传 -p 时，输出会落到
        ./manju_videos 默认目录（E2E 实锤：参考图/关键帧状态丢失、镜间首帧
        锚点断裂）。shots.json 位于 <项目根>/storyboard/ 下时必须自动推导
        项目根（跨智能体防踩坑）。"""
        src = inspect.getsource(doubao_gen.main)
        assert "storyboard" in src and "args.project_root = os.path.dirname" in src, (
            "main 缺少从 shots.json 路径自动推导 project_root 的逻辑"
        )
        # 推导必须在 resolve_output_dir 之前
        derive_pos = src.find("从分镜路径推导项目根")
        resolve_pos = src.find("resolve_output_dir(args.project_root")
        assert 0 < derive_pos < resolve_pos, "推导必须先于 resolve_output_dir 执行"

    def test_snapshot_tail_field_for_error_detection(self):
        """审查问题1（major）：快照消息 text 截断 900 字符，长消息尾部的
        错误/付费文案（如长 prompt 回显后的额度说明）会漏检 → 空等到
        超时。观察器与 poll 两处 JS 必须带 tail 尾部保留字段，Python 端
        _state_from_snapshot 拼接时必须并入 tail。"""
        obs_src = doubao_events._OBSERVER_JS
        poll_src = inspect.getsource(doubao_events.poll_snapshot)
        for js in (obs_src, poll_src):
            assert "tail" in js and "txt.slice(-300)" in js, (
                "快照 JS 缺 tail 尾部保留字段（长消息尾部文案漏检）"
            )
        py_src = inspect.getsource(doubao_gen._state_from_snapshot)
        assert 'm.get("tail")' in py_src, "_state_from_snapshot 未并入 tail 字段"

    def test_storyboard_match_case_insensitive(self):
        """审查问题2（minor）：Windows 路径大小写不敏感，Storyboard 大写
        变体也必须触发 project_root 推导。"""
        src = inspect.getsource(doubao_gen.main)
        assert '.lower() == "storyboard"' in src, "storyboard 目录匹配必须大小写不敏感"
