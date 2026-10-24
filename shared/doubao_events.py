# -*- coding: utf-8 -*-
"""
豆包会话事件驱动内核 —— MutationObserver 推送 + Runtime.addBinding 通知。

背景（2026-10-24 任务G 重构，用户指正轮询方案卡顿）：
  旧 wait_render 以 5s 轮询读 document.body.innerText 瞬时快照做决策，
  存在四个已实证的失效根因：
    A. innerText 累计计数（cardTitles/askCount）在豆包虚拟化卸载/回载下漂移，
       轮次键碰撞 → 新卡被判为旧卡 → 永不确认 → 900s 空等
       （实证：镜05 会话完成后 innerText 仅 1114 字符，卡片计数=0）
    B. 确认消息发送后无送达验证，submit 失败时假成功 → 永不重试
    C. 5s 轮询读到的是流式更新的瞬态中间态
    D. 豆包确认形态多变（可能无卡直生成、可能二次回卡），瞬态快照判型不稳

本模块提供：
  - arm(page)          注入常驻观察器（MutationObserver 监听消息列表，
                       debounce 300ms，快照指纹去重后经 binding 推送）
  - wait_snapshot()    阻塞等待下一次快照事件（事件驱动主循环的数据源）
  - poll_snapshot()    主动提取一次快照（兜底巡检 / 送达验证）
  - is_alive()         观察器心跳检测（失活 → 调用方应降级轮询）
  - snapshot_msgs()    从快照中取消息锚点结构

不变量（state-machine-correctness，调用方必须遵守）：
  Must-never:
    N1 同一轮次绝不发送两次确认（锚点+指纹双判据，见 doubao_gen.py）
    N2 确认未验证送达前绝不锁定轮次
    N3 绝不确认本镜提交之前的旧卡
    N4 确认总数 <= 3 轮
  Must-eventually:
    L1 新卡 <=2s 响应（事件推送延迟 <300ms debounce + 处理）
    L2 确认 <=10s 完成送达验证（wait_user_msg_confirm）
    L4 无进展 180s 告警（调用方实现，基于快照事件时间戳）
"""

import json
import time

from .cdp_browser import CDPError

BINDING_NAME = "__doubaoNotify"
OBS_FLAG = "__doubaoObs"
BEAT_FLAG = "__doubaoBeat"
EVENT_METHOD = "Runtime.bindingCalled"
# 心跳超时：超过该时长无任何推送/心跳刷新，判定观察器失活
ALIVE_TIMEOUT_MS = 60_000

# 观察器脚本（IIFE，幂等：重复注入时只刷新心跳）。
# 关键设计：
#  1. MutationObserver 挂 document.body（childList+subtree+characterData），
#     SPA 路由重建 message-list 也能感知；
#  2. debounce 300ms 合并流式更新风暴；
#  3. 快照指纹（消息 hash 序列 + 视频 src/dur）去重：无实质变化不推送，
#     事件流里只有真实状态迁移（.spinner 动画等噪声被指纹过滤）；
#  4. 心跳：每次推送尝试（无论指纹是否变化）都刷新 __doubaoBeat，
#     供 Python 端 is_alive() 判活。
_OBSERVER_JS = r"""
(() => {
  if (window.__doubaoObs) { window.__doubaoBeat = Date.now(); return 'already'; }
  const HASH = (s) => {
    let h = 5381;
    for (let i = 0; i < s.length && i < 2000; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return (h >>> 0).toString(36);
  };
  const snap = () => {
    const list = document.querySelector('[data-testid="message-list"]');
    const units = list ? [...list.querySelectorAll('[data-testid="union_message"]')] : [];
    const msgs = units.map((u, i) => {
      const txt = (u.innerText || '');
      return {
        i: i,
        r: u.querySelector('[data-testid="send_message"]') ? 'U' : 'B',
        h: HASH(txt),
        n: txt.length,
        card: txt.indexOf('视频生成参数确认') >= 0,
        ask: /确认后我再(开始|为你)/.test(txt),
        text: txt.slice(0, 900),
        // 尾部保留（>900 字符时）：错误/付费文案可能出现在长消息尾部
        // （如长 prompt 回显后的额度说明），text 截断会漏检
        tail: txt.length > 900 ? txt.slice(-300) : ''
      };
    });
    const videos = [...document.querySelectorAll('video')].map(v => ({
      src: (v.currentSrc || v.src || ''),
      dur: isFinite(v.duration) ? v.duration : 0
    }));
    return { t: Date.now(), msgs: msgs, videos: videos };
  };
  let timer = null;
  let lastSig = '';
  const push = () => {
    timer = null;
    window.__doubaoBeat = Date.now();
    try {
      const s = snap();
      const sig = s.msgs.map(m => m.i + m.r + m.h + (m.card ? 1 : 0) + (m.ask ? 1 : 0)).join(',')
        + '|' + s.videos.map(v => v.src.slice(-48) + ':' + Math.round(v.dur)).join(',');
      if (sig === lastSig) return;
      lastSig = sig;
      if (typeof window.__doubaoNotify === 'function') {
        window.__doubaoNotify(JSON.stringify(s));
      }
    } catch (e) { /* binding 未就绪/页面异常时静默，心跳已刷新 */ }
  };
  const mo = new MutationObserver(() => {
    if (timer) return;
    timer = setTimeout(push, 300);
  });
  mo.observe(document.body, { childList: true, subtree: true, characterData: true });
  window.__doubaoObs = { disconnect: () => mo.disconnect() };
  window.__doubaoBeat = Date.now();
  return 'armed';
})()
"""

# 新文档注入版（Page.addScriptToEvaluateOnNewDocument）：页面重载后自动重挂
_REARM_ON_LOAD_JS = _OBSERVER_JS


def arm(page) -> bool:
    """
    武装观察器：addBinding + 立即注入 + 注册新文档自动重挂。

    返回 True 表示事件链路可用；False 表示注入失败（调用方应走降级轮询）。
    幂等：重复调用只刷新心跳。
    """
    try:
        page.call("Runtime.addBinding", name=BINDING_NAME)
    except CDPError:
        return False
    try:
        # 已加载页面：立即注入
        r = page.eval_js(_OBSERVER_JS)
        if r not in ("armed", "already"):
            return False
    except CDPError:
        return False
    try:
        # 未来重载/新文档：自动重挂（binding 是 session 级，重载后仍有效）
        page.call("Page.addScriptToEvaluateOnNewDocument", source=_REARM_ON_LOAD_JS)
    except CDPError:
        pass  # 失败不影响当前页面的事件链路，仅损失重载恢复能力
    return True


def _parse_binding_payload(event: dict):
    """从 Runtime.bindingCalled 事件中解出本观察器的快照 payload。"""
    if not event or event.get("method") != EVENT_METHOD:
        return None
    params = event.get("params") or {}
    if params.get("name") != BINDING_NAME:
        return None
    try:
        snap = json.loads(params.get("payload") or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(snap, dict) or "msgs" not in snap:
        return None
    return snap


def wait_snapshot(page, timeout: float):
    """
    阻塞等待下一次快照事件，返回快照 dict；超时返回 None。

    快照结构：{t: 页面时间戳, msgs: [{i, r, h, n, card, ask, text}],
              videos: [{src, dur}]}
    等待期间收到的其他 CDP 事件入缓存，不丢失。
    """
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        ev = page.wait_event(EVENT_METHOD, remaining)
        snap = _parse_binding_payload(ev)
        if snap is not None:
            return snap
        # 非 targeting 事件（或别的 binding）：继续等剩余时间


def poll_snapshot(page):
    """主动提取一次快照（不依赖观察器推送）。用于兜底巡检与送达验证。"""
    try:
        raw = page.eval_js(r"""
        (() => {
          const HASH = (s) => {
            let h = 5381;
            for (let i = 0; i < s.length && i < 2000; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
            return (h >>> 0).toString(36);
          };
          const list = document.querySelector('[data-testid="message-list"]');
          const units = list ? [...list.querySelectorAll('[data-testid="union_message"]')] : [];
          const msgs = units.map((u, i) => {
            const txt = (u.innerText || '');
            return {
              i: i,
              r: u.querySelector('[data-testid="send_message"]') ? 'U' : 'B',
              h: HASH(txt),
              n: txt.length,
              card: txt.indexOf('视频生成参数确认') >= 0,
              ask: /确认后我再(开始|为你)/.test(txt),
              text: txt.slice(0, 900),
              // 与 _OBSERVER_JS 保持一致：尾部保留防长消息尾部文案漏检
              tail: txt.length > 900 ? txt.slice(-300) : ''
            };
          });
          const videos = [...document.querySelectorAll('video')].map(v => ({
            src: (v.currentSrc || v.src || ''),
            dur: isFinite(v.duration) ? v.duration : 0
          }));
          return JSON.stringify({ t: Date.now(), msgs: msgs, videos: videos });
        })()
        """)
        return json.loads(raw) if isinstance(raw, str) else None
    except (CDPError, ValueError, TypeError):
        return None


def is_alive(page) -> bool:
    """观察器心跳判活：__doubaoBeat 距今 < ALIVE_TIMEOUT_MS。

    观察器每次 MutationObserver 回调都会刷心跳（无论指纹是否变化），
    长会话静默期（无 DOM 变化）也可能超时——调用方判定失活后可先
    poll_snapshot 复核一次再降级。
    """
    try:
        beat = page.eval_js(f"window.{BEAT_FLAG} || 0")
        return bool(beat) and (time.time() * 1000 - int(beat)) < ALIVE_TIMEOUT_MS
    except CDPError:
        return False


def snapshot_msgs(snap):
    """取快照的消息锚点列表（容错空快照）。"""
    return (snap or {}).get("msgs") or []


def snapshot_videos(snap):
    """取快照的视频状态列表。"""
    return (snap or {}).get("videos") or []


def wait_user_msg_confirm(page, text_key: str, known_hashes, timeout: float = 10.0,
                          known_count: int = -1):
    """
    确认送达验证（文本路径，L2 不变量的实现）：

    发送确认文本后，等待一条**新的**用户消息出现且正文包含 text_key。
    事件驱动优先（wait_snapshot），事件静默时 poll_snapshot 复核，
    兼顾观察器推送未触达的窗口。

    判新双判据（2026-10-24 E2E 镜7 实锤 hash 碰撞后加固）：
      - known_count >= 0 时用**计数判新**：基线记录「含 text_key 的 U 消息」
        条数，当前条数 > 基线即送达。同文本消息（每轮确认文本完全相同）
        的 djb2 hash 必然碰撞，hash 排除法会把第二条同文本消息误判为旧
        消息 → 送达验证假失败 → 多发一条确认（豆包幂等兜住但违反 N1 精神）。
        计数判新对虚拟化卸载安全：卸载只会减少计数（不误报送达），
        新消息 append 必然增加计数。
      - known_count < 0 时退回 hash 排除法（向后兼容）。
    返回新用户消息的 hash（= 时序锚点，调用方用于后续新卡判定；同文本
    碰撞时返回的是最后一条匹配消息的 hash）；未送达返回 None。
    """
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            return None
        snap = wait_snapshot(page, remaining)
        if snap is None:
            # 事件静默兜底：主动提取一次（覆盖推送竞态）
            snap = poll_snapshot(page)
            if snap is None:
                return None
        msgs = snapshot_msgs(snap)
        if known_count >= 0:
            matches = [m for m in msgs
                       if m.get("r") == "U" and text_key in (m.get("text") or "")]
            if len(matches) > known_count:
                return matches[-1].get("h")
        else:
            for m in msgs:
                if m.get("r") == "U" and m.get("h") not in known_hashes \
                        and text_key in (m.get("text") or ""):
                    return m.get("h")
    return None
