#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆包漫剧自动生成器 —— 直连 CDP 驱动本地 Edge/Chrome，逐镜生成并下载视频。

共享内核：CDP 客户端、浏览器管理、页面操作原语提取到 shared/ 模块，
本文件只保留视频生成专有逻辑（时长控制、参数确认卡、渲染等待、视频下载）。

设计要点（均为实测踩坑结论）：
  * Edge/Chrome 136+ 禁止在「默认用户配置目录」上开远程调试
    -> 必须用独立的 --user-data-dir 专用配置；首次运行需在该窗口登录一次豆包，
       登录态持久保存在专用配置里，之后无需再登。
  * 豆包免费版单次生成最长 15 秒 -> 每镜单独发送、等渲染完、再发下一条。
  * DOM 会变 -> 全部多策略语义探测，失败输出截图 + DOM 快照，不盲目点击。

用法：
  python doubao_gen.py shots.json -o ./videos                 # 全流程生成
  python doubao_gen.py shots.json -o ./videos --dry-run       # 只打印将发送的内容
  python doubao_gen.py shots.json -o ./videos --only 1,3,5    # 只生成指定镜头
  python doubao_gen.py --probe                                # 只测浏览器连通与登录态
  python doubao_gen.py shots.json -o ./videos --keep-tab      # 结束后保留标签页
"""

import argparse
import base64
import glob
import json
import os
import re
import sys
import time
import urllib.request

# ── 加载共享内核 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import cdp_browser, doubao_page, style_presets, media_unlock, doubao_events
from shared.cdp_browser import CDPPage, CDPError, DOUBAO_HOME, _OPENER, log, cookie_header
from shared.doubao_page import (
    page_ready, login_state, is_logged_in, wait_login,
    fill_prompt, submit_prompt, diag_snapshot, new_conversation, upload_images,
)
from shared.project_manager import ProjectManager, resolve_output_dir

# 图生图能力已下沉共享内核（角色参考图/关键帧尾帧首帧共用同一条链路）
from shared import imagegen as char_ref_gen

# 渲染总超时（秒）。
# 2026-10-24 任务G：900→600。实测免费档渲染约 5-6.5 分钟（豆包自报"预计等待 5 分钟"），
# 600s=10 分钟仍有充足缓冲；同时新增三层看门狗（无进展 180s 告警、3 轮确认无进展
# 显式退出、总超时兜底），不再依赖一刀切长超时空等。
RENDER_TIMEOUT = 600
POLL_INTERVAL = 5  # 仅降级模式（观察器失活）使用；事件驱动模式由 MutationObserver 推送驱动
# 看门狗阈值（秒）：无任何实质进展持续该时长 → 告警 + 诊断输出
WATCHDOG_IDLE = 180
# 事件等待窗口（秒）：wait_snapshot 单次阻塞上限，超时进入兜底巡检（检查观察器心跳）
EVENT_WINDOW = 30

# 跨镜已完成的视频 src 指纹集合。
# 用于 wait_render 的"基线 src 集合"：上一镜刚完成下载的视频 src 也算基线，
# 防止豆包对历史消息做 lazy-load 重插入 DOM 时被误判为本镜新出片
# （2026-10-17 实锤 bug：shot_01 与 shot_02 字节完全相同，因 wait_render 开头
# 抓基线时上一镜 video 已被虚拟化卸载出 DOM → 基线为空 → lazy-load 回来的
# 上一镜 video 被当成新镜出片立即下载）
_KNOWN_VIDEO_SRCS: set[str] = set()


# ──────────────────────────────────────────────────────────────
# 视频生成专有逻辑
# ──────────────────────────────────────────────────────────────

def ensure_video_mode(page: CDPPage):
    """
    ⚠ 已禁用，禁止在主流程中调用（2026-10-16 实测结论）。

    豆包对话框底部栏的「视频生成」专属模式会把单次时长限制在 10s 以内，
    无法生成 12s 以上的镜头；而直接在普通对话框发送提示词，豆包会自行
    走视频生成流程（先回参数确认卡），并支持最长 15s。
    因此正确做法是：不做任何模式切换，直接 fill_prompt + submit_prompt。

    本函数仅保留作历史排查参考，不要重新接回主流程。
    """
    strategies = [
        # 策略1：文本匹配的视频生成入口
        """
        (()=>{
          const kws=['视频生成','AI视频','生成视频'];
          const all=[...document.querySelectorAll('button,a,[role=button],div[class*=menu],div[class*=tab]')]
            .filter(e=>e.offsetParent!==null);
          const hit=all.find(e=>{
            const t=(e.innerText||'').trim();
            return t && t.length<12 && kws.some(k=>t.includes(k));
          });
          if(hit){hit.click();return 'clicked:'+(hit.innerText||'').trim();}
          return null;
        })()
        """,
        # 策略2：aria-label / title 属性
        """
        (()=>{
          const hit=[...document.querySelectorAll('[aria-label],[title]')]
            .find(e=>/视频|video/i.test((e.getAttribute('aria-label')||'')+(e.getAttribute('title')||''))
                  && e.offsetParent!==null);
          if(hit){hit.click();return 'clicked-attr:'+(hit.getAttribute('aria-label')||hit.getAttribute('title'));}
          return null;
        })()
        """,
    ]
    for js in strategies:
        try:
            res = page.eval_js(js)
            if res:
                log(f"  · 已切换视频模式（{res}）")
                time.sleep(3)
                return True
        except CDPError:
            continue
    log("  · 未找到显式视频入口，改用对话框直发指令模式")
    return False


def set_duration(page: CDPPage, seconds: int):
    """
    设置视频时长（尽力而为）。

    找不到时长控件不阻塞——提示词里已写明"时长N秒"，模型多数会遵从。
    """
    js = f"""
    (()=>{{
      const want = {seconds};
      const cands=[...document.querySelectorAll('button,[role=button],label,span,div')]
        .filter(e=>{{
          if(e.offsetParent===null) return false;
          const t=(e.innerText||'').trim();
          return t===want+'s' || t===want+'秒' || t===String(want);
        }});
      cands.sort((a,b)=>(a.innerText||'').length-(b.innerText||'').length);
      if(cands.length){{
        const el=cands[0];
        if(el.tagName==='BUTTON'||el.getAttribute('role')==='button'){{el.click();}}
        else {{
          const lab=el.closest('label');
          if(lab) lab.click(); else el.click();
        }}
        return 'clicked:'+(el.innerText||'').trim();
      }}
      return null;
    }})()
    """
    try:
        res = page.eval_js(js)
        if res:
            log(f"  · 时长已设为 {seconds}s（{res}）")
        else:
            log(f"  · 未找到时长控件，依赖提示词中的「时长{seconds}秒」")
        time.sleep(1)
    except CDPError as e:
        log(f"  · 设置时长跳过: {e}")


REPLY_STATE_JS = """
(()=>{
  const t=document.body.innerText||'';
  // 最新一张「视频生成参数确认」卡（豆包渲染前必先回复参数卡求确认）
  const i=t.lastIndexOf('视频生成参数确认');
  const hasCard = i>=0 || /确认后我再(开始|为你)/.test(t);
  // 确认轮次计数：豆包可能「确认后再次回复一张新参数卡」要求二次确认，
  // 用标题/催确认句的累计出现次数区分“新的一张卡”与“同一张卡流式更新”。
  const cardTitles=(t.match(/视频生成参数确认/g)||[]).length;
  const askCount=(t.match(/确认后我再(开始|为你)/g)||[]).length;
  const card = i>=0 ? t.slice(i, Math.min(t.length, i+700)) : '';
  // 真正的额度耗尽（无"是否继续生成"按钮的纯文案，应终止整批）
  // 注意：付费询问"今日免费额度已用完，本次将消耗付费额度。是否继续生成？"不算 error，
  // 它有"确认生成"按钮，应走 paid_quota_prompt 分支让用户决策，不能误判为耗尽
  const errRe=/(生成次数[^\\n]{0,12}(已用完|用完|不足)|额度[^\\n]{0,10}(不足|已用完|已耗尽)|次数[^\\n]{0,6}(已用完|用完|耗尽)|免费次数已用完|明天再来|明日再来|审核未通过|不通过审核|内容不(符合|合规|适合)|生成失败|无法生成)/g;
  // 付费询问检测：文案同时含"付费额度"+"是否继续生成"
  // 一旦判定为付费询问，所有"已用完"类文案都是询问的上半句，绝不能算 error（避免误判为耗尽）
  const paidPrompt=/付费额度/.test(t) && /是否继续生成|是否继续/.test(t);
  const errors=[]; let m;
  while((m=errRe.exec(t))!==null){
    if(paidPrompt) continue;  // 付费询问场景下整页跳过 errors 收集
    errors.push(m[0]); if(errors.length>10) break;
  }
  // 豆包回复尾部 600 字符，供主循环实时监控打印（按句去重，避免日志爆炸）
  const replyTail=t.slice(-600);
  return JSON.stringify({hasCard, card, cardIdx: i, cardTitles, askCount,
    errors, paidPrompt, replyTail});
})()
"""


def read_reply_state(page: CDPPage) -> dict:
    """读取豆包当前回复状态。

    返回 {hasCard, card, errors, paidPrompt}：
      - hasCard/card：参数确认卡
      - errors：真正耗尽/审核/失败类错误文案（付费询问已排除）
      - paidPrompt：是否在询问"用付费额度继续生成"（应让用户决策，不能误判为耗尽）
    """
    try:
        info = page.eval_js(REPLY_STATE_JS)
        return json.loads(info) if isinstance(info, str) else (info or {})
    except CDPError:
        return {"hasCard": False, "card": "", "errors": [], "paidPrompt": False}


# 真正的额度耗尽（无按钮、纯文案"已用完/明天再来"等，全局致命）
# 收紧：去掉宽泛的"额度"单独关键词，避免把"付费额度"误判为耗尽
QUOTA_RE = re.compile(
    r"免费次数已用完|生成次数[^。\n]{0,12}(已用完|用完|不足)|"
    r"次数[^。\n]{0,6}(已用完|用完|耗尽)|"
    r"额度[^。\n]{0,10}(不足|已用完|已耗尽)|"
    r"明天再来|明日再来"
)


def quota_exhausted(page: CDPPage) -> bool:
    """页面是否已存在额度耗尽文案（用于提交前预检，避免无谓提交）。

    只匹配"真耗尽"——纯文案无按钮的终止类错误。付费询问不算耗尽，
    走 paid_quota_prompt 分支让用户决策。Python 层二次兜底：
    即使 JS 过滤漏了，paidPrompt=True 时也绝不返回 True。
    """
    state = read_reply_state(page)
    if state.get("paidPrompt"):
        return False  # 付费询问不是耗尽，让上层走 paid_quota_prompt 分支
    return any(QUOTA_RE.search(e or "") for e in state.get("errors", []))


def read_doubao_reply(page: CDPPage, tail_chars: int = 600) -> str:
    """读取豆包页面最新回复正文（取 body innerText 尾部 tail_chars 字符）。

    用于关键决策点打印豆包实际说了什么，便于排查"脚本看不懂豆包回复"的问题。
    用户要求：根据实际情况决策，必须能读到豆包的完整回复，而不是只看关键词。
    """
    try:
        info = page.eval_js(
            f"(()=>{{const t=document.body.innerText||'';return JSON.stringify(t.slice(-{tail_chars}));}})()"
        )
        if isinstance(info, str):
            try:
                return json.loads(info)
            except (json.JSONDecodeError, TypeError):
                return info
    except CDPError:
        pass
    return ""


def paid_quota_prompt(page: CDPPage) -> tuple[bool, str]:
    """检测豆包是否在询问"用付费额度继续生成"。

    返回 (is_paid_prompt, button_text)：
      - is_paid_prompt=True 时，button_text 是"确认生成"按钮的文字（用于点击）
      - is_paid_prompt=False 时，button_text 为空

    这种情况不能误判为耗尽——豆包给了"确认生成 →"按钮，应该让用户决策：
      --allow-paid=True  → 自动点按钮继续
      --allow-paid=False → 跳过本镜，记录到待补列表
    """
    state = read_reply_state(page)
    if not state.get("paidPrompt"):
        return False, ""
    # 找页面上的"确认生成"按钮（点击它同意用付费额度继续）
    try:
        res = page.eval_js("""
        (()=>{
          const btns=[...document.querySelectorAll('button,[role=button]')]
            .filter(b=>b.offsetParent!==null && !b.disabled);
          // 优先精确匹配"确认生成"
          let hit=btns.find(b=>{
            const t=(b.innerText||'').trim();
            return t==='确认生成' || t.startsWith('确认生成');
          });
          if(!hit){
            hit=btns.find(b=>{
              const t=(b.innerText||'').trim();
              return t && t.length<15 && t.includes('确认生成');
            });
          }
          if(hit) return (hit.innerText||'').trim();
          return null;
        })()
        """)
        if res:
            return True, res
    except CDPError:
        pass
    # 付费询问文案出现但按钮还没渲染出来（流式中），返回 is_paid_prompt=True 让上层稍后重试
    return True, ""


def parse_confirm_card(card: str) -> dict:
    """从参数确认卡文本解析 模型/时长/比例/风格。"""
    model = re.search(r"模型[:：]\s*([^\n]{0,30})", card)
    dur = re.search(r"时长[:：]\s*(\d+)", card)
    ratio = re.search(r"比例[:：]\s*([\d:：]+)", card)
    style = re.search(r"风格[:：]\s*([^\n]{0,40})", card)
    return {
        "model": model.group(1).strip() if model else None,
        "seconds": int(dur.group(1)) if dur else None,
        "ratio": ratio.group(1).strip() if ratio else None,
        "style": style.group(1).strip() if style else None,
    }


def send_confirmation(page: CDPPage, allow_text: bool = True) -> str:
    """
    对「视频生成参数确认」卡做二次确认。
    策略：优先点卡片上的确认类按钮；没有按钮且 allow_text=True 时才回复"确认"消息。
    返回动作描述（'button:…' / 'text' / 'none'）。

    allow_text=False 用于"点完按钮后的二次按钮探测"：只找按钮，绝不再发文本，
    避免同一张卡被重复发送多条"确认"消息（可能触发重复生成、多扣额度）。

    2026-10-24 修复两点：
      1) 按钮点击改用 CDP 真实鼠标点击 —— 合成 hit.click() 不走 React onClick，
         点了没反应（与发送按钮同一个坑）。
      2) 新版豆包的参数卡**根本没有按钮**，只有一句"确认后我再开始生成视频"，
         必须走文本兜底（回复"确认，开始生成视频"）。
    """
    # ── 方案1：真实鼠标点击确认类按钮 ──
    js_locate = """
    (()=>{
      const kws=['确认生成','开始生成','立即生成'];
      const all=[...document.querySelectorAll('button,[role=button],div[class*=btn],div[class*=button],span[class*=btn],a')]
        .filter(e=>e.offsetParent!==null || e.getClientRects().length);
      for(const kw of kws){
        const hit=all.find(e=>{
          const t=(e.innerText||'').trim();
          return t && t.length<15 && t.includes(kw);
        });
        if(hit){
          const r=hit.getBoundingClientRect();
          if(r.width<4||r.height<4) return null;
          return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
            text:(hit.innerText||'').trim(), disabled: !!hit.disabled});
        }
      }
      const hit=all.find(e=>{
        const t=(e.innerText||'').trim();
        return t==='确认' || t==='确认生成';
      });
      if(hit){
        const r=hit.getBoundingClientRect();
        if(r.width<4||r.height<4) return null;
        return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
          text:(hit.innerText||'').trim(), disabled: !!hit.disabled});
      }
      return null;
    })()
    """
    try:
        loc = page.eval_js(js_locate)
        if loc:
            loc = json.loads(loc) if isinstance(loc, str) else loc
            if not loc.get("disabled"):
                for evt in ("mouseMoved", "mousePressed", "mouseReleased"):
                    page.call("Input.dispatchMouseEvent", recv_timeout=15,
                              type=evt, x=loc["x"], y=loc["y"],
                              button="left", clickCount=1)
                time.sleep(1.5)
                return "button:" + loc.get("text", "")
    except (CDPError, json.JSONDecodeError):
        pass

    # ── 方案2：文本回复兜底 ──
    # 新版豆包参数卡没有按钮，只有"确认后我再开始生成视频"文案 → 必须发文本。
    if allow_text:
        try:
            if fill_prompt(page, "确认，开始生成视频") and submit_prompt(page):
                return "text"
        except CDPError as e:
            log(f"  ! 文本确认失败: {e}")
    return "none"


def _confirm_round_key(state: dict) -> tuple:
    """把一次「要求确认」映射成轮次键，用于去重。

    同一张卡在流式更新时标题计数不变 → 同一轮（不重复发确认）；
    豆包再次回复一张新卡时标题计数 +1 → 新的一轮（必须再确认，否则镜卡死不出片）。

    ⚠ 仅降级模式（观察器失活走轮询）使用：innerText 累计计数在豆包虚拟化
    卸载/回载下会漂移（2026-10-24 实证：会话完成后 innerText 仅 1114 字符、
    卡片计数=0），存在轮次键碰撞 → 漏确认新卡的固有风险。事件驱动模式
    使用 _find_pending_card 的消息锚点判据，不受虚拟化影响。
    """
    titles = int(state.get("cardTitles", 0) or 0)
    asks = int(state.get("askCount", 0) or 0)
    if titles > 0:
        return ("title", titles)
    if asks > 0:
        return ("ask", asks)
    idx = int(state.get("cardIdx", -1) or -1)
    if idx >= 0:
        return ("idx", idx)
    params = parse_confirm_card(state.get("card", "") or "")
    return ("fp", f"{params.get('model')}|{params.get('seconds')}|"
                  f"{params.get('ratio')}|{params.get('style')}")


# 事件驱动模式：确认文本（送达验证按它匹配新用户消息）
_CONFIRM_TEXT = "确认，开始生成视频"


def _state_from_snapshot(snap: dict) -> dict:
    """
    把 doubao_events 快照构造成 read_reply_state 兼容 dict（事件驱动模式数据源）。

    兼容字段：hasCard / card / errors / paidPrompt / replyTail —— 状态 2.5/3 的
    处理逻辑不变。额外附 msgs（消息锚点列表）供确认分支做锚点判定。
    """
    msgs = doubao_events.snapshot_msgs(snap)
    # B 消息正文：text（前 900 字符）+ tail（>900 时的末 300 字符）。
    # 长消息（如 prompt 回显）尾部的错误/付费文案不因截断漏检
    # （审查问题1：降级模式读全量 innerText 无此问题，事件模式需补齐）
    bot_texts = []
    for m in msgs:
        if m.get("r") == "B":
            t = m.get("text") or ""
            tail = m.get("tail") or ""
            bot_texts.append(t + ("\n" + tail if tail else ""))
    cards = [m for m in msgs if m.get("card")]
    asks = [m for m in msgs if m.get("ask")]
    full = "\n".join(bot_texts)
    # 与 REPLY_STATE_JS 相同的错误规则（Python 版）
    err_re = re.compile(
        r"(生成次数[^\n]{0,12}(已用完|用完|不足)|额度[^\n]{0,10}(不足|已用完|已耗尽)|"
        r"次数[^\n]{0,6}(已用完|用完|耗尽)|免费次数已用完|明天再来|明日再来|"
        r"审核未通过|不通过审核|内容不(符合|合规|适合)|生成失败|无法生成)")
    paid_prompt = ("付费额度" in full) and bool(re.search(r"是否继续生成|是否继续", full))
    errors = [] if paid_prompt else err_re.findall(full)[:10]
    # 错误收集时 paidPrompt 优先（与 JS 版一致：付费询问场景整页跳过 errors）
    flat_errors = ["".join(tup) if isinstance(tup, tuple) else str(tup) for tup in errors]
    return {
        "hasCard": bool(cards) or bool(asks),
        "card": (cards[-1].get("text") or "") if cards else "",
        "msgs": msgs,
        "errors": flat_errors,
        "paidPrompt": paid_prompt,
        "replyTail": bot_texts[-1][-600:] if bot_texts else "",
    }


def _card_fingerprint(card_text: str) -> str:
    """参数确认卡的内容指纹（模型|时长|比例|风格 四元组）。"""
    p = parse_confirm_card(card_text or "")
    return f"{p.get('model')}|{p.get('seconds')}|{p.get('ratio')}|{p.get('style')}"


def _find_pending_card(state: dict, anchor_hash, confirmed_card_hashes: set,
                       confirmed_params: set, prompt_idx=None):
    """
    事件驱动模式的新卡判定（不变量 N1/N3 的实现）。

    判据（按优先级）：
      1. 时序锚点：anchor_hash 是最近一次**已送达**确认的用户消息 hash。
         卡片消息必须位于 anchor 之后（i > anchor 的当前 idx）才算新卡——
         同一张卡流式更新（i 不变）与历史旧卡（在 anchor 之前）都不触发。
      2. prompt 锚点（anchor 缺席时，即本镜首轮确认前）：卡片必须位于
         本镜提示词消息之后（i > prompt_idx）——上一镜残留卡（回放卡/
         二次卡，i < prompt_idx）绝不确认（2026-10-25 用户实锤「豆包
         还没回复就提前发确认」的根因：prompt 未渲染时 _prompt_idx 取到
         上一镜确认消息，残留卡漏锁 + fallback 纯指纹判据误判新卡）。
      3. 双锚点都缺席（极端兜底）：参数指纹未见过才算新卡。
    返回待确认卡消息 dict 或 None。卡片自身的 hash 去重由调用方记录
    （confirmed_card_hashes：确认成功送达后加入，同一元素绝不重复确认）。
    """
    msgs = state.get("msgs") or []
    cards = [m for m in msgs if m.get("r") == "B" and (m.get("card") or m.get("ask"))]
    if not cards:
        return None
    anchor_idx = None
    if anchor_hash:
        for m in msgs:
            if m.get("h") == anchor_hash:
                anchor_idx = m.get("i")
                break
    for c in cards:
        if c.get("h") in confirmed_card_hashes:
            continue  # 这张卡已确认过（同元素流式更新也不重复）
        if anchor_idx is not None:
            if c.get("i", -1) > anchor_idx:
                return c  # anchor 之后的新卡 → 待确认
            # anchor 之前的卡 = 历史旧卡（N3：绝不确认）
        elif prompt_idx is not None:
            # prompt 锚点：只有本镜提示词**之后**豆包回的卡才算新卡；
            # 残留旧卡（i <= prompt_idx）一律拦截（N3 补全）
            if c.get("i", -1) > prompt_idx \
                    and _card_fingerprint(c.get("text") or "") not in confirmed_params:
                return c
        else:
            # 双锚点缺席的 fallback：参数指纹判据
            if _card_fingerprint(c.get("text") or "") not in confirmed_params:
                return c
    return None


def send_confirmation_verified(page: CDPPage, timeout: float = 10.0):
    """
    确认 + 送达验证（不变量 N2/L2 的实现，2026-10-24 任务G）。

    「确认动作完成」的定义从「我发了」升级为「豆包收到了」：
      - 文本路径：发送后等待一条**新的**用户消息（DOM 中出现含确认文本的
        send_message）——证明消息真的提交进了会话，而非 Enter 换行的假成功。
      - 按钮路径：CDP 真实鼠标点击成功后，等待页面发生一次实质状态迁移
        （观察器指纹去重后的事件：卡片变化/新消息/视频注入）。

    返回 (action, delivered, anchor_hash)：
      action      'button:…' / 'text' / 'none'
      delivered   送达验证是否通过（False 时调用方不得锁定轮次，应重试）
      anchor_hash 文本路径送达时的新用户消息 hash（后续新卡判定的时序锚点）；
                  按钮路径为 None（按卡片 hash 锁定）
    """
    # 送达基线：发送前已存在的用户消息 hash 集合 + 含确认文本的 U 消息条数
    # （计数判新：同文本确认消息 hash 必碰撞，2026-10-24 E2E 镜7 实锤加固）
    base_snap = doubao_events.poll_snapshot(page)
    base_msgs = doubao_events.snapshot_msgs(base_snap)
    known_hashes = {m.get("h") for m in base_msgs if m.get("r") == "U"}
    known_count = sum(1 for m in base_msgs
                      if m.get("r") == "U" and _CONFIRM_TEXT in (m.get("text") or ""))
    action = send_confirmation(page)
    if action == "none":
        return "none", False, None
    if action == "text":
        h = doubao_events.wait_user_msg_confirm(page, _CONFIRM_TEXT, known_hashes,
                                                timeout, known_count=known_count)
        return action, (h is not None), h
    # 按钮路径：等待一次实质状态迁移事件（指纹去重过的事件才算）
    snap = doubao_events.wait_snapshot(page, timeout)
    return action, (snap is not None), None


def wait_render(page: CDPPage, before_count: int, expect_seconds: int | None = None,
                expect_style_key: str | None = None,
                timeout: int = RENDER_TIMEOUT,
                prev_known_srcs: set[str] | None = None,
                allow_paid: bool = False,
                prompt_baseline_hashes: set | None = None):
    """
    事件驱动的渲染等待状态机（2026-10-24 任务G 重构；用户指正：旧 5s 轮询
    在确认失效后后台空等到 900s 超时，且 innerText 计数判据在豆包虚拟化
    卸载下漂移导致二次确认漏确认）。

    数据源（双模式）：
      事件模式（主）：页面内 MutationObserver 观察 message-list，快照指纹
        去重后经 Runtime.addBinding 推送（debounce 300ms，L1：<2s 响应）；
        wait_snapshot 阻塞等待；30s 事件窗口静默时做兜底巡检 + 观察器心跳
        判活（失活自动降级轮询）。
      降级模式：观察器注入失败/心跳超时 → 回退旧 read_reply_state 轮询
        （POLL_INTERVAL=5s，轮次键判据），日志明确标注「降级」。

    确认状态机（事件模式用消息锚点制，替代 innerText 累计计数）：
      新卡判定（_find_pending_card）：卡片消息位于「最近一次已送达确认」
        锚点之后 且 该卡 hash 未确认过 → 不变量 N1（同卡绝不重复确认）、
        N3（旧卡绝不确认）；anchor 被卸载时退参数指纹判据。
      确认完成（send_confirmation_verified）：送达验证通过才算 → 不变量
        N2（未验证送达绝不锁定轮次）、L2（<=10s 完成验证）。
      上限：N4 确认 <=3 轮；看门狗：L4 无进展 180s 告警、L5 3 轮确认且
        页面无响应显式退出、L6 总超时 RENDER_TIMEOUT 兜底。

      1. 出现新的 <video>（时长>=3s）      -> (视频信息, None)
      2. 「视频生成参数确认」卡             -> 解析核对参数，自动确认（最多 3 轮）
      3. 额度耗尽文案（不论新旧）           -> (None, "quota")，全局致命，调用方应中断整批
         其他新增错误（审核/失败）          -> (None, None)，本镜失败
      4. 超时                               -> (None, None)
    """
    deadline = time.time() + timeout
    last_report = 0
    confirm_count = 0
    # ── 降级模式的轮次键判据（事件模式不用；见 _confirm_round_key 警示）──
    confirmed_rounds: set = set()              # 已确认过的轮次键
    clicked_buttons: set[str] = set()
    confirm_fail = 0                           # 连续确认失败次数（防止无限重试）
    # ── 事件模式的锚点判据状态 ──
    anchor_hash = None                         # 最近一次已送达确认的用户消息 hash
    confirmed_card_hashes: set = set()         # 已确认卡的元素 hash（N1）
    confirmed_params: set = set()              # 已确认卡的参数指纹（anchor 卸载 fallback）
    # ── 看门狗（L4/L5）──
    last_progress = time.time()                # 最近一次实质进展（事件/心跳/状态迁移）
    progress_warned = False
    # 提交时刻的回复基线：
    #  - 历史错误不算"新增"；
    #  - 上一镜残留的参数卡不许被当成新卡重复确认（连续生成时同一标签同一会话）。
    pre_errors: set[str] = set()
    # 实时监控豆包回复：按句去重打印，避免流式输出导致日志爆炸
    seen_sentences: set[str] = set()
    last_reply_tail = ""
    try:
        _pre = read_reply_state(page)
        pre_errors = set(_pre.get("errors", []))
        # 2026-10-24 修复：这里**不能**把页面已有的参数卡计入 confirmed_rounds。
        # 原逻辑把起始时页面上出现的卡片当作"已确认过"，于是 round_key 预先被锁定，
        # is_new_round 恒为 False → 本镜永远不会触发确认，镜卡死在
        # "确认后我再开始生成视频"处直到超时（用户截图实锤）。
        # 起始时的卡片只应作为"错误文案基线"使用，卡片本身要留给本轮确认。
        _pre_titles = int(_pre.get("cardTitles", 0) or 0)
        _pre_asks = int(_pre.get("askCount", 0) or 0)
        if _pre_titles > 0 or _pre_asks > 0:
            log(f"  · 页面已存在参数卡（标题×{_pre_titles} 催确认×{_pre_asks}），"
                f"本镜将重新确认")
    except Exception:
        pass
    # 基线视频指纹：豆包会对历史消息里的旧视频做虚拟化卸载（DOM 里时有时无），
    # 用计数对比会失效；改为记录提交时已存在视频的 src，任何基线外的新 src 即本镜出片。
    # 关键：上一镜已完成的 src 也注入基线（prev_known_srcs），防止上一镜 video
    # 被虚拟化卸载出 DOM 后又被 lazy-load 重新插入时被误判为新镜出片。
    baseline_srcs = set(prev_known_srcs or [])
    try:
        # 2026-10-24：不再截断到 150 字符 —— 豆包 CDN 的视频 URL 带签名参数，
        # 截断后下载会 403；且本镜 src 要原样传给 download_video。
        raw = page.eval_js("""
        JSON.stringify([...document.querySelectorAll('video')]
          .map(v=>(v.currentSrc||v.src||'')).filter(Boolean))
        """)
        baseline_srcs.update(json.loads(raw) if isinstance(raw, str) else [])
    except Exception:
        pass

    # 豆包新版（2026-10-17 实锤）视频渲染改为虚拟列表 + 缩略图 + 播放图标：
    # 初始只有 <img cover> + 空 .video-player div，必须用真实鼠标点击 .play-icon-wrapper
    # 才会注入 <video> 元素并加载视频。原生 element.click() 不触发 React 合成事件，
    # 必须用 CDP Input.dispatchMouseEvent 模拟真实鼠标点击。
    play_clicked = False
    # duration 未就绪激发节流（状态1.6）：video 注入后 v.duration 可能长时间停在 0/NaN
    # （readyState<HAVE_METADATA），fresh/done_cnt 检查全失败；主动 load()+play() 触发
    # 元数据加载。5s 节流避免每轮空打。
    last_kick_ts = 0.0

    # 事件驱动模式武装（任务G）：注入失败自动降级轮询
    event_armed = doubao_events.arm(page)
    if event_armed:
        log("  · 事件驱动模式已启用（MutationObserver 推送，响应 <2s，替代 5s 轮询）")
    else:
        log("  ! 观察器注入失败，自动降级为轮询模式（5s）")

    # ── prompt 送达锚定（2026-10-25 用户实锤修复：「豆包还没回复就提前发确认」）──
    # submit 后豆包渲染 prompt 的 U 消息有延迟。若不等它出现就做 N3 基线锁定，
    # 「最后一条 U 消息」会取到上一镜的确认消息 → 上一镜残留卡（回放卡/二次卡）
    # 漏锁 → fallback 指纹判据误判新卡 → 提前对残留卡发确认。
    # 修复：用 submit 前采集的 U 消息基线（prompt_baseline_hashes），等待
    # 「基线外的新 U 消息」（= 本镜 prompt 已进入会话）出现后才进入确认判定；
    # 豆包的任何卡片必须位于 prompt 之后才算新卡（i > prompt_idx）。
    prompt_idx = None
    if prompt_baseline_hashes is not None:
        _p_deadline = time.time() + 8  # 最多等 8s（正常 <1s）
        _prompt_msg = None
        while time.time() < _p_deadline:
            _snap = doubao_events.poll_snapshot(page)
            for _m in doubao_events.snapshot_msgs(_snap):
                if _m.get("r") == "U" and _m.get("h") not in prompt_baseline_hashes:
                    _prompt_msg = _m
                    break
            if _prompt_msg is not None:
                break
            time.sleep(0.5)
        if _prompt_msg is not None:
            prompt_idx = _prompt_msg.get("i", -1)
            log(f"  · 提示词已送达会话（消息 #{prompt_idx}），开始等待豆包回复")
        else:
            log("  ! 8s 内未见提示词消息出现在会话中（提交可能失败），"
                "按旧基线继续（谨慎模式）")

    # N3 基线锁定（双模式）：prompt_idx（或最后一条 U 消息）**之前**的所有卡片
    # 都算历史旧卡，预先加入 confirmed 集合，绝不重复确认。
    # 双重防误锁：只锁 i < 锚点 idx 的卡——即使豆包已秒回本镜新卡
    # （i > 锚点 idx，物理上新消息元素）也不会被误锁进基线。
    # 2026-10-25：从事件模式分支提出到双模式——降级模式（arm 失败全程轮询）
    # 此前无任何旧卡防护，残留卡轮次键必然不在新 set → 立即提前确认。
    try:
        _base_snap = doubao_events.poll_snapshot(page)
        _base_msgs = doubao_events.snapshot_msgs(_base_snap)
        if prompt_idx is None:
            prompt_idx = max((m.get("i", -1) for m in _base_msgs if m.get("r") == "U"),
                             default=-1)
        for _m in _base_msgs:
            if _m.get("r") == "B" and (_m.get("card") or _m.get("ask")) \
                    and _m.get("i", -1) < prompt_idx:
                confirmed_card_hashes.add(_m.get("h"))
                confirmed_params.add(_card_fingerprint(_m.get("text") or ""))
        if confirmed_card_hashes:
            log(f"  · 已锁定 {len(confirmed_card_hashes)} 张历史卡片（N3 基线，"
                f"绝不重复确认）")
    except Exception:
        pass
    last_poll_sig = None  # 兜底巡检的快照指纹（用于判断是否实质变化）

    while time.time() < deadline:
        try:
            # ── 数据源层（双模式）──
            if event_armed:
                _win = min(EVENT_WINDOW, max(1.0, deadline - time.time()))
                snap = doubao_events.wait_snapshot(page, _win)
                if snap is not None:
                    # 指纹去重后的推送事件 = 实质状态迁移（新消息/卡片变化/视频注入）
                    last_progress = time.time()
                    progress_warned = False
                else:
                    # 事件窗口静默：兜底巡检 + 观察器心跳判活
                    if doubao_events.is_alive(page):
                        # 心跳新鲜 = DOM 仍在变化（渲染 spinner 等），不算空等
                        last_progress = time.time()
                        progress_warned = False
                    else:
                        event_armed = False
                        log("  ! 观察器心跳超时（60s 无活动），自动降级为轮询模式")
                    snap = doubao_events.poll_snapshot(page)
                    _sig = json.dumps([(m.get("h"), m.get("n")) for m in
                                       doubao_events.snapshot_msgs(snap)], ensure_ascii=False)
                    if _sig != last_poll_sig:
                        last_poll_sig = _sig
                        last_progress = time.time()  # 巡检发现实质变化
                state = _state_from_snapshot(snap) if snap else read_reply_state(page)
                _videos = doubao_events.snapshot_videos(snap)
                data = {
                    "n": len(_videos),
                    "vids": _videos,
                    "hasSpinner": bool(re.search(
                        r"生成中|正在生成|渲染中|创作中", state.get("replyTail", ""))),
                }
            else:
                # 降级模式：旧轮询路径（read_reply_state + 5s 间隔）
                snap = None
                data = page.eval_js("""
                (()=>{
                  const vids=[...document.querySelectorAll('video')];
                  const info=vids.map(v=>({
                    dur: v.duration && isFinite(v.duration) ? v.duration : 0,
                    ready: v.readyState,
                    src: (v.currentSrc||v.src||'')
                  }));
                  return JSON.stringify({
                    n: vids.length,
                    vids: info,
                    hasSpinner: /生成中|正在生成|渲染中|创作中/.test(document.body.innerText||'')
                  });
                })()
                """)
                data = json.loads(data) if isinstance(data, str) else (data or {})
                state = read_reply_state(page)

            # ── 看门狗（L4/L5）：无进展告警 + 3 轮确认且页面无响应显式退出 ──
            if time.time() - last_progress > WATCHDOG_IDLE:
                if not progress_warned:
                    progress_warned = True
                    log(f"  ! 看门狗：{WATCHDOG_IDLE}s 无任何进展（无事件/心跳/状态变化）")
                    log(f"      确认轮次 {confirm_count}/3，模式：{'事件驱动' if event_armed else '轮询降级'}")
                    log(f"      豆包回复尾部: {read_doubao_reply(page)[:200]}")
                if confirm_count >= 3:
                    log("  ! 已确认 3 轮且页面无响应，显式退出本镜（不等总超时，L5）")
                    return None, None

            # 状态1：基线外的新视频（时长>=3s 且 src 不在提交时基线里）即本镜完成
            vids = data.get("vids", [])
            fresh = [v for v in vids
                     if v.get("dur", 0) >= 3 and v.get("src")
                     and v["src"] not in baseline_srcs]
            if fresh:
                # 把基线随出片一起返回：generate_shot 下载时要用它做 exclude
                # （2026-10-24 实跑 NameError 实锤：此前 generate_shot 直接引用
                #  wait_render 的局部变量 baseline_srcs，属未定义名称，出片后
                #  下载阶段必崩 —— 镜02 渲染成功却崩在下载，白扣 1 次额度）
                out = fresh[-1]
                out["baseline_srcs"] = baseline_srcs
                return out, None
            # 兜底：拿不到 src 时退回计数对比
            done_cnt = len([v for v in vids if v.get("dur", 0) >= 3])
            if done_cnt > before_count or (before_count == 0 and done_cnt > 0):
                out = vids[-1] if vids else None
                if out:
                    out["baseline_srcs"] = baseline_srcs
                return out, None

            # 状态1.5：豆包新版需点击播放图标才会注入 <video> 元素
            # 检测到 .play-icon-wrapper 存在且本镜尚未点过 → CDP 真实鼠标点击触发 video 加载
            # 注意：条件必须是 not fresh 而不是 not vids —— 多镜会话中上一镜的 video 元素
            # 可能仍在 DOM 里（其 src 在 baseline_srcs 中会被 fresh 排除），但本镜的新 video
            # 还没创建。若用 not vids 会短路，永远不点 play 图标，导致本镜卡死。
            if not fresh and not play_clicked:
                try:
                    play_rect = page.eval_js(r"""
                    (()=>{
                      const el=document.querySelector('.play-icon-wrapper-wkMy04, [class*=play-icon-wrapper]');
                      if(!el || el.offsetParent===null) return null;
                      const r=el.getBoundingClientRect();
                      if(r.width===0 || r.height===0) return null;
                      return JSON.stringify({x:r.x+r.width/2, y:r.y+r.height/2});
                    })()
                    """)
                    if play_rect:
                        rect = json.loads(play_rect)
                        px, py = rect['x'], rect['y']
                        # CDP Input.dispatchMouseEvent 模拟真实鼠标点击（React 合成事件必需）
                        page.call("Input.dispatchMouseEvent", type="mouseMoved", x=px, y=py)
                        page.call("Input.dispatchMouseEvent", type="mousePressed",
                                  x=px, y=py, button="left", clickCount=1, buttons=1)
                        page.call("Input.dispatchMouseEvent", type="mouseReleased",
                                  x=px, y=py, button="left", clickCount=1, buttons=0)
                        play_clicked = True
                        log("  · 检测到视频缩略图，已点击播放触发 <video> 元素加载")
                        time.sleep(2)
                        continue  # 立即进入下一轮，等 video 元素加载
                except (CDPError, json.JSONDecodeError):
                    pass

            # 状态1.6：<video> 已注入但 duration 未就绪（=0/NaN，readyState 没上来）时
            # 主动 load()+play() 激发元数据加载。豆包某些场景 video 元素被懒加载，不主动
            # 激发会一直停在 dur=0，fresh/done_cnt 检查全失败最终 900s 超时（2026-10-19
            # 实锤：豆包回复"你的视频生成好了"但脚本空等 15 分钟）。
            if not fresh and vids and (time.time() - last_kick_ts) >= 5.0:
                needs_kick = any(
                    v.get("src") and v.get("dur", 0) < 3 for v in vids
                )
                if needs_kick:
                    try:
                        page.eval_js("""
                        (()=>{
                          document.querySelectorAll('video').forEach(v=>{
                            const src = v.currentSrc || v.src || '';
                            const dur = v.duration;
                            const durBad = !dur || !isFinite(dur) || dur < 3;
                            if (src && durBad) {
                              try { v.load(); } catch(e){}
                              try {
                                v.muted = true;  // 避免自动播放被浏览器拦截
                                const p = v.play();
                                if (p && p.catch) p.catch(()=>{});
                              } catch(e){}
                            }
                          });
                          return 'kicked';
                        })()
                        """)
                        last_kick_ts = time.time()
                        log("  · 检测到 video 元素但 duration 未就绪，已激发 load/play")
                    except CDPError:
                        pass

            # 实时监控豆包回复：按句号/换行切分尾部 600 字符，只打印新句子
            # 流式输出期间每个新句子打印一次，避免日志爆炸；seen_sentences 上限 100 防内存涨
            # 注：state 已在数据源层构造（事件模式来自快照、降级模式来自 read_reply_state）
            reply_tail = state.get("replyTail", "") or ""
            if reply_tail and reply_tail != last_reply_tail:
                last_reply_tail = reply_tail
                # 按句号/换行/问号/感叹号切分，过滤过短片段
                for sent in re.split(r"[。\n！？!?]", reply_tail):
                    s = sent.strip()
                    if len(s) < 4:
                        continue
                    # 截断过长句子（避免一条占满屏幕）
                    if len(s) > 80:
                        s = s[:80] + "…"
                    if s not in seen_sentences:
                        seen_sentences.add(s)
                        if len(seen_sentences) > 100:
                            # 超过 100 句清空一半，防止无限增长
                            seen_sentences.clear()
                        log(f"  · 豆包: {s}")

            # 豆包读不到参考图时会自行降级成纯文生视频，成片人物与参考图脱钩，
            # 必须判本镜失败（此时尚未渲染，尽早终止可少浪费等待时间）
            if any(k in reply_tail for k in (
                    "参考图资源当前无法读取", "改为纯文生视频", "不再依赖图片引用")):
                log("  ! 豆包读不到参考图，已改为纯文生视频，本镜作废")
                return None, "ref_lost"

            # 状态3：错误文案 → 立即失败
            for e in state.get("errors", []):
                if QUOTA_RE.search(e):
                    log(f"  ! 豆包回复：{e}")
                    return None, "quota"
                if e not in pre_errors:
                    log(f"  ! 豆包回复报错：{e}")
                    return None, None

            # 状态2.5：付费额度询问 → 按 --allow-paid 决策
            # 豆包"今日免费额度已用完，本次将消耗付费额度。是否继续生成？"+ "确认生成 →"按钮
            # 不能误判为耗尽——截图实锤：按钮就在那里，应该让用户决定点不点
            is_paid, paid_btn = paid_quota_prompt(page)
            if is_paid:
                if allow_paid and paid_btn:
                    log(f"  · 豆包询问付费生成，--allow-paid 已启用，点击「{paid_btn}」继续")
                    # 点"确认生成"按钮（用 send_confirmation 复用按钮点击逻辑）
                    action = send_confirmation(page, allow_text=False)
                    if action != "none":
                        log(f"  · 已点击付费确认按钮: {action}")
                        time.sleep(3)
                    else:
                        log("  ! 付费确认按钮识别失败，跳过本镜")
                        return None, "paid"
                elif allow_paid and not paid_btn:
                    # 付费询问文案出现但按钮还没渲染（流式中），等下一轮再检测
                    if not hasattr(wait_render, "_paid_wait_log"):
                        log("  · 豆包询问付费生成（按钮渲染中），等待 --allow-paid 触发…")
                        wait_render._paid_wait_log = True
                else:
                    # --allow-paid=False（默认）：跳过本镜，记录到待补列表
                    log("  · 豆包询问付费生成，--allow-paid 未启用，跳过本镜（次日免费额度刷新后重试）")
                    log(f"  · 豆包回复: {read_doubao_reply(page)[:200]}")
                    return None, "paid"

            # 状态2：参数确认卡 → 解析核对 → 自动确认（豆包可能要求多轮确认）
            # 双判据：事件模式用消息锚点制（_find_pending_card，不受虚拟化卸载影响，
            # 不变量 N1/N3）；降级模式沿用轮次键制（innerText 计数，有已知漂移风险）。
            if event_armed:
                pending = _find_pending_card(state, anchor_hash,
                                             confirmed_card_hashes, confirmed_params,
                                             prompt_idx=prompt_idx)
                card_text = (pending.get("text") or "") if pending else ""
                round_key = None
                is_new_round = pending is not None
            else:
                pending = None
                card_text = state.get("card", "") or ""
                round_key = _confirm_round_key(state)
                # 卡片指纹去重（N1 在降级模式的补全，2026-10-24 E2E 镜6 实锤：
                # innerText 计数漂移把豆包回放的同一张卡判为新轮次 → 多发一条
                # 确认，靠豆包幂等才没多扣额度）。已确认过同参数指纹的卡不再确认；
                # 残余风险（豆包要求确认参数完全相同的第二张卡，极罕见）由
                # 180s 看门狗告警 + 600s 总超时兜底。
                is_new_round = bool(state.get("hasCard")) \
                    and round_key not in confirmed_rounds \
                    and _card_fingerprint(card_text) not in confirmed_params
            if is_new_round and confirm_count < 3:
                params = parse_confirm_card(card_text)
                log("  · 豆包回复「视频生成参数确认」卡：")
                if params.get("model"):
                    log(f"      模型 {params['model']}")
                if params.get("ratio"):
                    log(f"      比例 {params['ratio']}")
                if params.get("seconds") is not None:
                    d = params["seconds"]
                    note = "" if expect_seconds is None or d == expect_seconds \
                        else f"（与目标 {expect_seconds}s 不一致，豆包已自行调整）"
                    log(f"      时长 {d}s{note}")
                if params.get("style"):
                    s = params["style"]
                    warn = ""
                    if expect_style_key == "3d-realistic" and \
                            re.search(r"卡通|动漫|Q版|皮克斯|赛璐璐|3D动画", s):
                        warn = "  ⚠ 与你设定的写实仿真人风格不一致，豆包可能沿用了卡通风格"
                    log(f"      风格 {s}{warn}")
                # 确认 + 送达验证（N2/L2）：完成定义从「我发了」升级为「豆包收到了」
                action, delivered, new_anchor = send_confirmation_verified(page)
                if action != "none" and delivered:
                    confirm_count += 1
                    confirm_fail = 0
                    last_progress = time.time()
                    log(f"  · 已自动确认第 {confirm_count} 轮（{action}，送达已验证），"
                        f"等待渲染开始")
                    # 锁定本轮：事件模式锁卡片 hash + 时序锚点；降级模式锁轮次键
                    if event_armed:
                        if pending is not None:
                            confirmed_card_hashes.add(pending.get("h"))
                            confirmed_params.add(_card_fingerprint(card_text))
                        if new_anchor:
                            anchor_hash = new_anchor
                    else:
                        confirmed_rounds.add(round_key)
                        # 降级模式同步锁指纹（回放卡去重的另一半，见上方注释）
                        confirmed_params.add(_card_fingerprint(card_text))
                    # 卡片确认后再找一次"确认生成"按钮（可能需要二次按钮点击）；
                    # 只做按钮探测，禁止再发文本消息
                    action2 = send_confirmation(page, allow_text=False)
                    if action2 != "none" and action2.startswith("button"):
                        confirm_count += 1
                        log(f"  · 二次确认按钮: {action2}")
                        last_progress = time.time()
                        time.sleep(3)
                elif action != "none" and not delivered:
                    # 已发出但送达验证未通过：不锁定轮次（N2），将重试。
                    # 重试有理论上的重复发送风险，但 wait_user_msg_confirm 是
                    # 事件+poll 双通道检测，漏检概率远低于 submit 假成功概率
                    # （2026-10-23 实锤卡死根因），且 5 次上限防无限重发。
                    confirm_fail += 1
                    if action.startswith("button") and event_armed and pending is not None:
                        # 按钮路径防重复点击：点击已执行但页面无反应，锁定该卡
                        # （重复点击确认按钮的重复提交风险 > 等待风险），靠看门狗兜底
                        confirmed_card_hashes.add(pending.get("h"))
                        confirmed_params.add(_card_fingerprint(card_text))
                        log(f"  ! 按钮已点击但页面无实质响应（第 {confirm_fail} 次），"
                            f"已锁定该卡防止重复点击，等待看门狗判定")
                    else:
                        log(f"  ! 确认已发出但送达验证未通过（第 {confirm_fail} 次），"
                            f"不锁定轮次，将重试")
                    if confirm_fail >= 5:
                        log("  ! 送达验证连续 5 次失败，放弃本镜（防重复发送多扣额度）")
                        return None, None
                else:
                    confirm_fail += 1
                    log(f"  ! 本轮确认未成功，将重试（不锁定轮次）"
                        f"[连续失败 {confirm_fail}]")
                    if confirm_fail >= 8:
                        log("  ! 连续确认失败 8 次，放弃本镜")
                        return None, None
            elif not state.get("hasCard") and confirm_count < 3:
                # 非卡片状态下也检查"确认生成"按钮（豆包可能有多个确认步骤），
                # 同一文本的按钮只点一次（clicked_buttons 去重）。
                # 实测 bug（2026-10-17）：若此处也要求“已确认过就跳过”，文本确认后
                # 主循环永不进此分支，二次确认按钮若未在 action2 检测窗口期内出现
                # （2s 太短），后续永远不会被点，镜卡死在"确认生成"按钮处。
                try:
                    # 2026-10-24：合成 hit.click() 不走 React onClick → 改真实鼠标点击
                    btn_loc = page.eval_js("""
                    (()=>{
                      const kws=['确认生成','开始生成','立即生成'];
                      const btns=[...document.querySelectorAll('button,[role=button]')]
                        .filter(b=>(b.offsetParent!==null||b.getClientRects().length) && !b.disabled);
                      const hit=btns.find(b=>{
                        const t=(b.innerText||'').trim();
                        return t && t.length<12 && kws.some(k=>t.includes(k));
                      });
                      if(!hit) return null;
                      const r=hit.getBoundingClientRect();
                      if(r.width<4||r.height<4) return null;
                      return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
                        text:(hit.innerText||'').trim()});
                    })()
                    """)
                    if btn_loc:
                        btn_loc = json.loads(btn_loc) if isinstance(btn_loc, str) else btn_loc
                        btn_res = "button:" + btn_loc.get("text", "")
                        if btn_res not in clicked_buttons:
                            clicked_buttons.add(btn_res)
                            for evt in ("mouseMoved", "mousePressed", "mouseReleased"):
                                page.call("Input.dispatchMouseEvent", recv_timeout=15,
                                          type=evt, x=btn_loc["x"], y=btn_loc["y"],
                                          button="left", clickCount=1)
                            confirm_count += 1
                            log(f"  · 点击确认按钮: {btn_res}")
                            time.sleep(3)
                except (CDPError, json.JSONDecodeError):
                    pass

            # 进度提示
            if time.time() - last_report > 30:
                status = "渲染中" if data.get("hasSpinner") else "排队中"
                log(f"    · 视频数 {data.get('n',0)}，{status}…（已等待 {int(time.time()-(deadline-timeout))}s）")
                last_report = time.time()
        except CDPError:
            pass
        # 节奏控制：事件模式由 wait_snapshot 阻塞等事件（不 sleep，响应 <2s）；
        # 降级模式保持 5s 轮询间隔
        if not event_armed:
            time.sleep(POLL_INTERVAL)

    return None, None


def get_video_src(page: CDPPage, exclude: set | None = None) -> str | None:
    """取页面中视频元素的 src。

    2026-10-24 修复：豆包 SPA 会在 DOM 中保留上一镜的 <video> 元素。
    原实现取"最后一个 duration>=3 的 video"，当本镜新视频 duration 尚未
    就绪（0/NaN）时，会错误地返回上一镜的 src —— 实测镜03 下载到了镜02 的
    mp4（两者 MD5 完全相同）。
    修法：exclude 参数传入已知的旧 src（基线 + 跨镜已知），只取不在其中的。
    """
    exclude = exclude or set()
    js = """
    (()=>{
      const EX = %s;
      const vids=[...document.querySelectorAll('video')];
      const pick = v => {
        const s=(v.currentSrc||v.src||'');
        if(!s) return null;
        for(const e of EX){ if(s===e || s.startsWith(e) || e.startsWith(s)) return null; }
        return s;
      };
      // 优先：duration 已就绪且不在排除集里的
      const ready=vids.filter(v=>v.duration>=3).map(pick).filter(Boolean);
      if(ready.length) return ready[ready.length-1];
      // 其次：任意不在排除集里的
      const any=vids.map(pick).filter(Boolean);
      return any.length?any[any.length-1]:null;
    })()
    """ % json.dumps(sorted(exclude))
    try:
        return page.eval_js(js)
    except CDPError:
        return None


def _is_mp4_bytes(data: bytes) -> bool:
    """校验字节流是否为 mp4：大小 > 10KB 且文件头第 4~8 字节为 ftyp。"""
    return len(data) > 10 * 1024 and data[4:8] == b"ftyp"


def _is_mp4_file(filepath: str) -> bool:
    """校验已落盘文件是否为 mp4（读取文件头，不存在返回 False）。"""
    if not os.path.exists(filepath):
        return False
    try:
        with open(filepath, "rb") as f:
            data = f.read(10 * 1024 + 8)
    except OSError:
        return False
    return _is_mp4_bytes(data)


def download_video(page: CDPPage, outdir: str, index: int, seconds: int,
                   preferred_src: str = "", exclude_srcs: set = None) -> str | None:
    """
    下载视频，只保留两档策略（按优先级）：
      1) 无水印原片：捕获 fallback_api → logo_type=unwatermarked → qAAB 解密
         （失败自动降级到带水印页面资源，不影响后续流程）
      2) 带水印页面资源：
         a. https 直链：带 Cookie/Referer 直接下载（CDN 资源通常需要登录态）
         b. blob: 源：页面内 fetch -> base64 取回（CDN 直链拿不到时的兜底）
         两种方式都必须通过 mp4 文件头校验才落盘，非 mp4 内容直接丢弃。

    preferred_src：wait_render 已确认的本镜视频 src —— 优先使用，
        避免重新在 DOM 里搜索时取到上一镜残留的 <video>（实测会下载到
        上一镜的 mp4，两者 MD5 相同）。
    exclude_srcs：已知的旧 src 集合（基线 + 跨镜已知），搜索时排除。
    """
    os.makedirs(outdir, exist_ok=True)
    filename = f"shot_{index:02d}_{seconds}s.mp4"
    filepath = os.path.join(outdir, filename)

    # 方案0：无水印原片（media_unlock 内部已校验 mp4）
    try:
        if media_unlock.is_armed(page):
            path = media_unlock.download_unwatermarked_video(page, filepath)
            if path:
                log("  · 已下载无水印原片")
                return path
    except CDPError as e:
        log(f"  · 无水印原片获取异常，降级下载带水印页面资源: {e}")

    # 优先用 wait_render 确认的本镜 src；否则在 DOM 里搜（排除旧 src）
    src = preferred_src or get_video_src(page, exclude=exclude_srcs)

    # 方案1：https 直链 + Cookie（落盘前校验 mp4）
    if src and src.startswith(("http://", "https://")) and not src.startswith("blob:"):
        try:
            headers = {
                "User-Agent": page.eval_js("navigator.userAgent") or "Mozilla/5.0",
                "Referer": DOUBAO_HOME + "/",
            }
            ck = cookie_header(page.cookies(), src)
            if ck:
                headers["Cookie"] = ck
            with _OPENER.open(urllib.request.Request(src, headers=headers), timeout=180) as r:
                data = r.read()
            if not _is_mp4_bytes(data):
                log("  · https 直链返回的不是 mp4（可能是 HTML/鉴权页），降级 blob 取回")
            else:
                with open(filepath, "wb") as f:
                    f.write(data)
                if _is_mp4_file(filepath):
                    log("  · 已下载带水印页面资源（https 直链）")
                    return filepath
                os.remove(filepath)
                log("  · https 直链落盘文件校验失败，降级 blob 取回")
        except Exception:
            pass

    # 方案2：blob 源 → 页面内 fetch 取回 base64（解码后校验 mp4 才落盘）
    try:
        res = page.eval_js("""
        (async ()=>{
          const vids=[...document.querySelectorAll('video')];
          const v=vids.filter(x=>x.duration>=3).pop() || vids.pop();
          if(!v) return null;
          const src=v.currentSrc||v.src||'';
          if(!src) return null;
          const b=await (await fetch(src)).blob();
          if(b.size > 500*1024*1024) return {tooBig:true, size:b.size};
          const buf=await b.arrayBuffer();
          const bytes=new Uint8Array(buf);
          let binary=''; const chunk=0x8000;
          for(let i=0;i<bytes.length;i+=chunk){
            binary+=String.fromCharCode.apply(null, bytes.subarray(i,i+chunk));
          }
          return {b64: btoa(binary), size: b.size};
        })()
        """, await_promise=True, recv_timeout=300)
        if isinstance(res, dict) and res.get("b64"):
            data = base64.b64decode(res["b64"])
            if not _is_mp4_bytes(data):
                log("  · blob 取回内容不是 mp4，放弃该资源")
            else:
                with open(filepath, "wb") as f:
                    f.write(data)
                if _is_mp4_file(filepath):
                    log("  · 已下载带水印页面资源（blob fetch）")
                    return filepath
                os.remove(filepath)
                log("  · blob 落盘文件校验失败，放弃该资源")
    except (CDPError, ValueError):
        pass

    return None


# ──────────────────────────────────────────────────────────────
# v3：关键帧图生成阶段（尾帧图 + 首帧图）
# ──────────────────────────────────────────────────────────────

def _collect_ref_images(shot: dict, char_refs_map: dict, props_map: dict) -> list:
    """收集某镜头的参考图路径列表：出场角色图 + 出场道具图。"""
    refs = []
    for name in shot.get("characters", []):
        p = char_refs_map.get(name)
        if p and os.path.exists(p):
            refs.append(p)
    for prop_name in shot.get("props", []):
        p = props_map.get(prop_name)
        if p and os.path.exists(p):
            refs.append(p)
    return refs


def generate_keyframes(page: CDPPage, shots: list, outdir: str,
                       char_refs_map: dict, props_map: dict,
                       keyframes_state: dict = None,
                       expect_portrait: bool = None) -> tuple[dict, dict]:
    """
    v3：逐镜生成尾帧图（首镜还生成首帧图），用角色图+道具图作参考（图生图）。

    返回 (keyframes_map, state)：
      keyframes_map: {shot_index: {"start_frame": path|None, "end_frame": path}}
      state: 更新后的 keyframes_state（持久化用）

    第一镜（index==1）：生成首帧图（frame_prompt）+ 尾帧图（end_frame_prompt）
    后续镜头：只生成尾帧图（end_frame_prompt），首帧由视频生成阶段把上一镜尾帧图
    作为参考图上传并标注「视频第一帧必须与该图一致」实现衔接

    断点续跑：已生成的关键帧图从 keyframes_state 读取，跳过重复生成。
    """
    os.makedirs(outdir, exist_ok=True)
    keyframes_map: dict[int, dict] = {}
    state = keyframes_state or {}

    total = len(shots)
    for i, shot in enumerate(shots, 1):
        idx = shot["index"]
        log(f"\n{'─'*62}")
        log(f"[关键帧] 进度 {i}/{total} · 镜 {idx}")

        # 收集参考图（角色图+道具图）
        ref_paths = _collect_ref_images(shot, char_refs_map, props_map)
        if not ref_paths:
            log(f"  · 镜 {idx} 无角色/道具参考图，跳过关键帧生成")
            # 不覆盖 state 已有的关键帧：保留 end_frame 供视频生成阶段上传，
            # 避免"无参考图"时把已有尾帧图清成 None 导致视频生成拿不到本镜尾帧参考
            if str(idx) in state and idx not in keyframes_map:
                keyframes_map[idx] = state[str(idx)]
            elif idx not in keyframes_map:
                keyframes_map[idx] = {"start_frame": None, "end_frame": None}
            continue

        # 保留 state 中已有的本镜条目（--only 模式下跳过已生成项时）
        if str(idx) in state and idx not in keyframes_map:
            keyframes_map[idx] = state[str(idx)]

        # ── 尾帧图 ──
        end_tag = f"shot_{idx:02d}_end"
        end_frame_path = state.get(str(idx), {}).get("end_frame")
        if end_frame_path and os.path.exists(end_frame_path):
            log(f"  · 尾帧图已存在，跳过: {os.path.basename(end_frame_path)}")
        else:
            end_prompt = shot.get("end_frame_prompt", "")
            if not end_prompt:
                log(f"  ! 镜 {idx} 无 end_frame_prompt，跳过尾帧生成")
            else:
                status, end_frame_path = char_ref_gen.generate_one(
                    page, end_prompt, outdir, f"shot_{idx:02d}", "尾帧",
                    ref_image_paths=ref_paths, expect_portrait=expect_portrait,
                )
                if status == "quota":
                    log("  ⛔ 图片额度耗尽，终止关键帧生成")
                    return keyframes_map, state
                if status != "ok" or not end_frame_path:
                    log(f"  ! 镜 {idx} 尾帧图生成失败")
                    end_frame_path = None

        # ── 首帧图（仅第一镜）──
        start_frame_path = None
        if idx == 1:
            start_tag = f"shot_{idx:02d}_start"
            start_frame_path = state.get(str(idx), {}).get("start_frame")
            if start_frame_path and os.path.exists(start_frame_path):
                log(f"  · 首帧图已存在，跳过: {os.path.basename(start_frame_path)}")
            else:
                start_prompt = shot.get("frame_prompt", "")
                if not start_prompt:
                    log(f"  ! 镜 {idx} 无 frame_prompt，跳过首帧生成")
                else:
                    status, start_frame_path = char_ref_gen.generate_one(
                        page, start_prompt, outdir, f"shot_{idx:02d}", "首帧",
                        ref_image_paths=ref_paths, expect_portrait=expect_portrait,
                    )
                    if status == "quota":
                        log("  ⛔ 图片额度耗尽，终止关键帧生成")
                        # 尾帧已生成，先保存再退出
                        keyframes_map[idx] = {
                            "start_frame": None,
                            "end_frame": end_frame_path,
                        }
                        state[str(idx)] = keyframes_map[idx]
                        return keyframes_map, state
                    if status != "ok" or not start_frame_path:
                        log(f"  ! 镜 {idx} 首帧图生成失败")
                        start_frame_path = None

        keyframes_map[idx] = {
            "start_frame": start_frame_path,
            "end_frame": end_frame_path,
        }
        state[str(idx)] = keyframes_map[idx]
        log(f"  · 镜 {idx} 关键帧完成: 首帧={'有' if start_frame_path else '无'}, "
            f"尾帧={'有' if end_frame_path else '无'}")

    return keyframes_map, state


# ──────────────────────────────────────────────────────────────
# 单镜生成
# ──────────────────────────────────────────────────────────────

def generate_shot(page: CDPPage, shot: dict, outdir: str, style_key: str | None = None,
                  char_refs_map: dict | None = None, props_map: dict | None = None,
                  keyframes_map: dict | None = None,
                  allow_paid: bool = False):
    """生成单个镜头。返回 (状态, 路径)。状态∈ {ok, failed, quota, paid}。
    paid：豆包询问付费额度且 --allow-paid=False，本镜跳过，记录到待补列表。

    跨镜衔接：从第 2 镜起，把上一镜尾帧图作为本镜首帧参考图上传，
    并明确标注「视频第一帧必须与该图一致（承接上一镜结尾）」。
    """
    idx = shot["index"]
    secs = int(shot["seconds"])
    prompt = shot["prompt"]

    log(f"\n[{idx}] {secs}s · {shot.get('beat_label','')} · {shot.get('scene','')}")
    if secs > 15:
        log(f"  ! 时长 {secs}s 超过豆包免费版 15s 上限，已强制截断为 15s")
        secs = 15

    # 提交前预检：上一镜刚耗尽额度时，页面已有文案，直接终止
    try:
        if quota_exhausted(page):
            log("  ! 页面已显示免费额度耗尽，跳过本镜")
            return "quota", None
    except CDPError:
        pass

    # 从第 2 镜起，提交前新建对话，清空上一镜残留状态。
    # 豆包在"上一镜已完成"的旧对话中接收新提示词时，可能只流式输出参数卡
    # 内容片段而不返回「视频生成参数确认」标题，导致 read_reply_state 检测
    # 不到参数卡、脚本不自动确认、本镜视频永远不开始生成（2026-10-20 测试发现）。
    if idx >= 2:
        log(f"  · 镜 {idx}：新建对话清空上一镜残留状态…")
        if not new_conversation(page, timeout=60):
            log("  ! 新建对话失败，沿用当前页面继续提交")
        else:
            # 无需重新 arm()：Page.addScriptToEvaluateOnNewDocument 注入的钩子
            # 对所有新 document 都生效，不会因 SPA 导航而失效。
            # 但需重新预检额度（新对话可能显示不同状态）
            try:
                if quota_exhausted(page):
                    log("  ! 新对话后页面显示免费额度耗尽，跳过本镜")
                    return "quota", None
            except CDPError:
                pass

    try:
        before = int(page.eval_js("String(document.querySelectorAll('video').length)") or 0)
    except Exception:
        before = 0

    # 清空上一镜捕获的 fallback_api，确保下载时 latest 对应当前镜头
    media_unlock.clear_captured(page)

    # ── v3：收集所有参考图（角色图 + 道具图 + 关键帧图），记录每张图用途 ──
    ref_items = []  # [(path, label), ...] label 用于 prompt 标注
    # 角色图
    if char_refs_map and shot.get("characters"):
        for char_name in shot["characters"]:
            p = char_refs_map.get(char_name)
            if p and os.path.exists(p):
                ref_items.append((p, f"角色「{char_name}」形象参考"))
            else:
                log(f"  · 角色「{char_name}」无参考图，跳过")
    # 道具图
    if props_map and shot.get("props"):
        for prop_name in shot["props"]:
            p = props_map.get(prop_name)
            if p and os.path.exists(p):
                ref_items.append((p, f"道具「{prop_name}」参考"))
            else:
                log(f"  · 道具「{prop_name}」无参考图，跳过")
    # 关键帧图：首帧（镜1 用自己的首帧图；镜≥2 继承上一镜尾帧图）+ 尾帧图
    kf = (keyframes_map or {}).get(idx, {})
    if idx >= 2:
        prev_kf = (keyframes_map or {}).get(idx - 1, {})
        if prev_kf.get("end_frame") and os.path.exists(prev_kf["end_frame"]):
            ref_items.append((prev_kf["end_frame"],
                              "本镜首帧画面，视频第一帧必须与该图一致（承接上一镜结尾）"))
        else:
            log(f"  · 上一镜无尾帧图，镜 {idx} 缺首帧锚点")
    if kf.get("end_frame") and os.path.exists(kf["end_frame"]):
        ref_items.append((kf["end_frame"],
                          "本镜尾帧画面，视频最后一帧应过渡到该画面"))
    if idx == 1 and kf.get("start_frame") and os.path.exists(kf["start_frame"]):
        ref_items.append((kf["start_frame"],
                          "本镜首帧画面，视频第一帧必须与该图一致"))

    ref_paths = [item[0] for item in ref_items]

    # ── 上传参考图（图生视频模式） ──
    # 必须先确认每张图都上传成功再提交：豆包读不到参考图时会自动改成纯文生视频，
    # 人物形象与参考图脱钩且照样扣 1 次额度（2026-10-23 实测）。
    if ref_paths:
        log(f"  · 上传参考图: {len(ref_paths)} 张（{', '.join(os.path.basename(p) for p in ref_paths)}）")
        if not upload_images(page, ref_paths, log_prefix="参考图"):
            diag_snapshot(page, outdir, f"shot_{idx:02d}_upload_fail")
            log("  ! 参考图未全部上传成功，已中止本镜（不生成无参考的纯文视频）")
            return "failed", None

    # ── prompt 开头标注每张参考图用途（@图片N 语法）──
    if ref_items:
        ref_labels = "\n".join(
            f"@图片{i+1}：{label}" for i, (_, label) in enumerate(ref_items)
        )
        prompt = f"参考图说明：\n{ref_labels}\n\n{prompt}"

    if not fill_prompt(page, prompt):
        diag_snapshot(page, outdir, f"shot_{idx:02d}_fill_fail")
        return "failed", None

    time.sleep(0.8)
    set_duration(page, secs)

    # prompt 送达锚定基线（2026-10-25）：提交前已存在的用户消息 hash 集合。
    # wait_render 据此等待「基线外新 U 消息」（= 本镜 prompt）出现后才进入
    # 确认判定，杜绝豆包还没回复就对上一镜残留卡提前发确认。
    try:
        _pre_snap = doubao_events.poll_snapshot(page)
        prompt_baseline = {m.get("h") for m in doubao_events.snapshot_msgs(_pre_snap)
                           if m.get("r") == "U"}
    except Exception:
        prompt_baseline = set()

    if not submit_prompt(page):
        log("  ! 提交失败（可能额度用尽或需要人工确认）")
        diag_snapshot(page, outdir, f"shot_{idx:02d}_submit_fail")
        return "failed", None

    log("  · 已提交，等待渲染…")
    video, error_tag = wait_render(page, before, expect_seconds=secs, expect_style_key=style_key,
                                    prev_known_srcs=_KNOWN_VIDEO_SRCS,
                                    allow_paid=allow_paid,
                                    prompt_baseline_hashes=prompt_baseline)

    if error_tag == "quota":
        return "quota", None

    if error_tag == "paid":
        return "paid", None

    if error_tag == "ref_lost":
        diag_snapshot(page, outdir, f"shot_{idx:02d}_ref_lost")
        log("  ! 参考图丢失（豆包改为纯文生成），请检查上传链路后重跑本镜")
        return "failed", None

    if not video:
        log(f"  ! 本镜未出片（报错或超时 {RENDER_TIMEOUT}s）")
        diag_snapshot(page, outdir, f"shot_{idx:02d}_render_timeout")
        return "failed", None

    log(f"  · 渲染完成，时长 {video.get('dur',0):.1f}s")
    # 把本镜新出片 src 记入跨镜基线，供下一镜 wait_render 防误判
    if video.get("src"):
        _KNOWN_VIDEO_SRCS.add(video["src"])
    path = download_video(page, outdir, idx, secs,
                          preferred_src=video.get("src"),
                          exclude_srcs=video.get("baseline_srcs", set()) | set(_KNOWN_VIDEO_SRCS))
    if path:
        size = os.path.getsize(path) / 1024 / 1024
        log(f"  · 已下载: {path} ({size:.1f} MB)")
        return "ok", path
    log("  ! 未取到视频文件")
    diag_snapshot(page, outdir, f"shot_{idx:02d}_download_fail")
    return "failed", None


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────

def probe_main(args):
    """连通性体检：拉起浏览器 -> 打开豆包 -> 报告页面与登录状态。"""
    doubao_page.probe_connectivity(
        args.port, args.browser, args.profile_dir, args.out,
        keep_tab=args.keep_tab, login_wait=args.login_wait
    )


def main():
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="豆包漫剧自动生成器（直连 CDP，逐镜生成，单镜 <=15s）")
    ap.add_argument("shots_json", nargs="?", default=None,
                    help="split_novel.py 产出的 *.shots.json（省略则自动查找最新一个）")
    ap.add_argument("-o", "--out", default="./manju_videos", help="输出目录（视频/诊断文件）")
    ap.add_argument("-p", "--project-root", default=None,
                    help="项目根目录（启用统一项目管理，自动从 storyboard/characters/ 读取输入，产物归档到 keyframes/videos/ 等子目录）")
    ap.add_argument("--probe", action="store_true", help="只测浏览器连通与豆包登录态，不生成")
    ap.add_argument("--browser", default="edge",
                    choices=["edge", "chrome", "brave", "vivaldi", "opera", "chromium"],
                    help="可执行浏览器类型（默认 edge，仅支持 Chromium 内核）")
    ap.add_argument("--port", type=int, default=9222, help="CDP 调试端口（默认 9222）")
    ap.add_argument("--profile-dir", default=None,
                    help="专用浏览器配置根目录（默认 %%LOCALAPPDATA%%/doubao-manju）")
    ap.add_argument("--login-wait", type=int, default=600,
                    help="检测到登录墙时等待用户登录的秒数（默认 600）")
    ap.add_argument("--only", default=None, help="只生成指定镜号，逗号分隔，如 1,3,5")
    ap.add_argument("--start", type=int, default=None, help="从第N镜开始（含）")
    ap.add_argument("--end", type=int, default=None, help="生成到第N镜止（含）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不实际生成")
    ap.add_argument("--keep-tab", action="store_true", help="结束后保留浏览器标签页")
    ap.add_argument("--continue-on-error", action="store_true", help="单镜失败后继续下一镜")
    ap.add_argument("--allow-paid", action="store_true",
                    help="豆包询问付费额度（免费额度已用完）时，自动点「确认生成」按钮继续（默认 False：跳过本镜记录待补）")
    ap.add_argument("--no-unlock", action="store_true",
                    help="禁用视频无水印下载（默认开启；图片始终优先下载无水印原图，不受此开关影响）")
    ap.add_argument("--char-refs", default=None,
                    help="角色参考图索引 JSON（char_refs_index.json），用于图生视频时上传参考图")
    ap.add_argument("--props-refs", default=None,
                    help="道具参考图索引 JSON（char_refs_index.json 中的 props 部分），v3 图生图参考")
    ap.add_argument("--keyframes-only", action="store_true",
                    help="v3：只生成关键帧图（尾帧+首镜首帧），不生成视频")
    ap.add_argument("--skip-keyframes", action="store_true",
                    help="v3：跳过关键帧图生成阶段（直接用已有 keyframes_state.json）")
    args = ap.parse_args()

    # 跨智能体防踩坑（2026-10-24 E2E 实锤）：直接传 shots.json 路径而未传
    # --project-root 时，输出会落到 ./manju_videos 默认目录（参考图/关键帧
    # 状态全部丢失，镜间首帧锚点断裂）。从标准项目结构自动推导：shots.json
    # 位于 <项目根>/storyboard/*.shots.json 时，项目根 = 其父目录的父目录。
    if not args.project_root and args.shots_json:
        _sj_parent = os.path.dirname(os.path.abspath(args.shots_json))
        # 大小写不敏感（Windows 路径不区分大小写，跨智能体可能传大写变体）
        if os.path.basename(_sj_parent).lower() == "storyboard":
            args.project_root = os.path.dirname(_sj_parent)
            log(f"· 从分镜路径推导项目根: {args.project_root}")

    # 解析输出目录和项目管理器
    out_dir, pm = resolve_output_dir(args.project_root, args.out)
    if pm:
        # 使用项目结构：从 storyboard/ 读取输入，产物写入对应子目录
        storyboard_dir = pm.get_path("storyboard")
        keyframes_dir = pm.get_path("keyframes")
        videos_dir = pm.get_path("videos")
        os.makedirs(keyframes_dir, exist_ok=True)
        os.makedirs(videos_dir, exist_ok=True)

    if args.profile_dir is None:
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/.local/share"))
        args.profile_dir = os.path.join(base, "doubao-manju")

    # ── probe 模式 ──
    if args.probe:
        probe_main(args)
        return

    # ── 定位分镜文件 ──
    # 优先从 project_root/storyboard/ 读取 shots.json
    shots_json = args.shots_json
    if not shots_json and pm:
        storyboard_dir = pm.get_path("storyboard")
        cands = glob.glob(os.path.join(storyboard_dir, "*.shots.json"))
        if cands:
            shots_json = max(cands, key=os.path.getmtime)
            log(f"· 从项目读取分镜文件: {shots_json}")
    if not shots_json:
        cands = glob.glob(os.path.join(out_dir, "**", "*.shots.json"), recursive=True)
        cands += glob.glob("./**/*.shots.json", recursive=True)
        if not cands:
            log("✗ 未找到 *.shots.json。请先运行 split_novel.py，或显式传入路径。")
            sys.exit(1)
        shots_json = max(cands, key=os.path.getmtime)
        log(f"· 自动选用分镜文件: {shots_json}")

    with open(shots_json, encoding="utf-8") as f:
        data = json.load(f)

    shots = data["shots"]
    total = len(shots)

    if args.only:
        want = {int(x) for x in re.split(r"[,\s]+", args.only) if x.strip()}
        shots = [s for s in shots if s["index"] in want]
    if args.start:
        shots = [s for s in shots if s["index"] >= args.start]
    if args.end:
        shots = [s for s in shots if s["index"] <= args.end]

    log("=" * 62)
    log(f"《{data['title']}》漫剧自动生成")
    log(f"风格: {data['style_desc'][:40]}… | 比例: {data['ratio']} | 模型: {data['model']}")
    log(f"镜头: {len(shots)}/{total} | 总时长: {sum(s['seconds'] for s in shots)}s "
        f"| 预计额度: {len(shots)} 次（1 生成 = 1 额度，与时长无关）")
    log(f"单镜上限: 15s（豆包免费版硬限制）")
    log("=" * 62)

    if args.dry_run:
        for s in shots:
            log(f"\n[{s['index']}] {s['seconds']}s · {s.get('beat_label','')}")
            if s.get("characters"):
                log(f"  出场角色: {', '.join(s['characters'])}")
            log(f"  {s['prompt'][:110]}…")
        log("\n(dry-run) 未实际生成。去掉 --dry-run 即开始。")
        return

    # ── 加载角色/道具参考图索引（支持自动发现） ──
    # 若用户未显式指定 --char-refs / --props-refs，按以下顺序兜底查找
    #   char_refs_index.json（同一份索引同时含角色 type=character 和道具 type=prop）：
    #   1) project_root/characters/char_refs_index.json（project-root 模式）
    #   2) {outdir}/char_refs/char_refs_index.json
    #   3) {parent}/char_refs/char_refs_index.json
    #   4) {parent}/*/char_refs/char_refs_index.json（同级目录按修改时间倒序，取最新）
    char_refs_path = args.char_refs
    props_refs_path = args.props_refs
    if not char_refs_path or not props_refs_path:
        import glob as _glob
        auto_candidates = []
        # 优先从 project_root/characters/ 查找
        if pm:
            chars_dir = pm.get_path("characters")
            auto_candidates.append(os.path.join(chars_dir, "char_refs_index.json"))
        parent_dir = os.path.dirname(os.path.abspath(out_dir))
        auto_candidates.extend([
            os.path.join(out_dir, "char_refs", "char_refs_index.json"),
            os.path.join(parent_dir, "char_refs", "char_refs_index.json"),
        ])
        auto_candidates.extend(sorted(
            _glob.glob(os.path.join(parent_dir, "*", "char_refs", "char_refs_index.json")),
            key=os.path.getmtime, reverse=True))
        for cand in auto_candidates:
            if os.path.exists(cand):
                if not char_refs_path:
                    char_refs_path = cand
                if not props_refs_path:
                    props_refs_path = cand
                break
        if char_refs_path and not args.char_refs and not args.props_refs:
            log(f"· 自动发现参考图索引: {char_refs_path}")

    # ── 加载角色参考图索引 ──
    char_refs_map = {}
    if char_refs_path:
        try:
            with open(char_refs_path, encoding="utf-8") as f:
                idx = json.load(f)
            for r in idx.get("results", []):
                if r.get("type") == "prop":
                    continue  # 角色图索引只取 type=character
                if r.get("status") == "ok" and r.get("path"):
                    p = r["path"]
                    # path 通常相对项目根；不在项目根跑时按 json 所在目录兜底拼 basename
                    if not os.path.exists(p):
                        p = os.path.normpath(
                            os.path.join(os.path.dirname(char_refs_path), os.path.basename(r["path"])))
                    if os.path.exists(p):
                        char_refs_map[r["name"]] = p
            log(f"· 角色参考图: {len(char_refs_map)} 张（{', '.join(char_refs_map.keys())}）")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log(f"⚠️  加载角色参考图索引失败({e})，将不使用参考图")

    # ── 加载道具参考图索引 ──
    props_map = {}
    if props_refs_path:
        try:
            with open(props_refs_path, encoding="utf-8") as f:
                idx = json.load(f)
            # 兼容两种结构：props_results 数组 或 results 中按 type="prop" 过滤
            prop_entries = idx.get("props_results")
            if not prop_entries:
                prop_entries = [r for r in idx.get("results", []) if r.get("type") == "prop"]
            for r in prop_entries:
                if r.get("status") == "ok" and r.get("path"):
                    p = r["path"]
                    if not os.path.exists(p):
                        p = os.path.normpath(
                            os.path.join(os.path.dirname(props_refs_path), os.path.basename(r["path"])))
                    if os.path.exists(p):
                        props_map[r["name"]] = p
            if props_map:
                log(f"· 道具参考图: {len(props_map)} 张（{', '.join(props_map.keys())}）")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log(f"⚠️  加载道具参考图索引失败({e})，将不使用道具参考图")

    # ── 关键帧状态文件路径（断点续跑）──
    if pm:
        keyframes_state_path = os.path.join(pm.get_path("keyframes"), "keyframes_state.json")
    else:
        keyframes_state_path = os.path.join(out_dir, "keyframes_state.json")

    # ── 打开浏览器 + 登录 ──
    cdp_browser.ensure_browser(args.port, args.browser, args.profile_dir)
    log("\n正在打开豆包…")
    # 独立新标签页：复用已有标签页时豆包可能把新提示词当成旧会话的追问，
    # 或把生成结果渲染到别的标签页，导致本页等不到图。
    tid = cdp_browser.new_tab(args.port, DOUBAO_HOME)
    created_tid = tid
    page = CDPPage(args.port, tid)

    # 在页面任何请求发出前安装无水印媒体捕获钩子（内部会重导航一次）
    if not args.no_unlock:
        media_unlock.arm(page, DOUBAO_HOME)

    try:
        if not page_ready(page, timeout=90):
            log("  ! 页面未就绪。诊断快照已保存。")
            diag_snapshot(page, out_dir, "page-not-ready")
            sys.exit(3)

        # 登录检查
        if not is_logged_in(page):
            if not wait_login(page, args.login_wait, out_dir, "login-wall"):
                sys.exit(3)

        # 注意：不要点击底部栏「视频生成」切换专属模式（该模式限时长 ≤10s）。
        # 直接在普通对话框发送，豆包会自行走视频流程并先回参数确认卡，支持最长 15s。

        # ── v3：关键帧图生成阶段（尾帧图 + 首镜首帧图）──
        keyframes_map: dict = {}
        if not args.skip_keyframes:
            log("\n" + "=" * 62)
            log("阶段 1/2：生成关键帧图（尾帧 + 首镜首帧）")
            log("=" * 62)
            # 加载已有状态（断点续跑）
            keyframes_state = {}
            if os.path.exists(keyframes_state_path):
                try:
                    with open(keyframes_state_path, encoding="utf-8") as f:
                        keyframes_state = json.load(f)
                    log(f"· 加载关键帧状态: {len(keyframes_state)} 镜已生成")
                except (json.JSONDecodeError, OSError):
                    keyframes_state = {}

            keyframes_map, keyframes_state = generate_keyframes(
                page, shots, keyframes_dir if pm else out_dir, char_refs_map, props_map, keyframes_state,
                expect_portrait=(data.get("ratio", "9:16") == "9:16"),
            )
            # --only 模式下 generate_keyframes 只处理子集，但视频生成阶段
            # 仍需要前一镜的关键帧图作为首帧锚点 → 从 state 补全
            for k, v in keyframes_state.items():
                ki = int(k)
                if ki not in keyframes_map:
                    keyframes_map[ki] = v
            # 持久化关键帧状态
            with open(keyframes_state_path, "w", encoding="utf-8") as f:
                json.dump(keyframes_state, f, ensure_ascii=False, indent=2)
            log(f"· 关键帧状态已保存: {keyframes_state_path}")

            if args.keyframes_only:
                log("\n✅ 关键帧图生成完成（--keyframes-only 模式，不生成视频）")
                return
        else:
            # 跳过生成，直接加载已有状态
            if os.path.exists(keyframes_state_path):
                try:
                    with open(keyframes_state_path, encoding="utf-8") as f:
                        keyframes_state = json.load(f)
                    keyframes_map = {int(k): v for k, v in keyframes_state.items()}
                    log(f"· 跳过关键帧生成，加载已有状态: {len(keyframes_map)} 镜")
                except (json.JSONDecodeError, OSError):
                    log("· 关键帧状态文件不存在或损坏，将不使用关键帧图")

        # ── 阶段 2：视频生成 ──
        log("\n" + "=" * 62)
        log("阶段 2/2：生成视频")
        log("=" * 62)

        results, failed = [], []
        paid_pending = []
        quota_hit = False
        for i, shot in enumerate(shots, 1):
            log(f"\n{'─'*62}")
            log(f"进度 {i}/{len(shots)}")
            status, path = generate_shot(
                page, shot, videos_dir if pm else out_dir, data.get("style_key"),
                char_refs_map=char_refs_map, props_map=props_map,
                keyframes_map=keyframes_map,
                allow_paid=args.allow_paid,
            )
            results.append((shot["index"], status, path))
            if status == "quota":
                quota_hit = True
                log("\n⛔ 今日免费生成额度已耗尽。后续镜头不再尝试，次日额度刷新后用下方命令续跑。")
                break
            if status == "paid":
                # 豆包询问付费额度且 --allow-paid 未启用，跳过本镜，记录待补
                paid_pending.append(shot["index"])
                log(f"\n💰 镜 {shot['index']} 需付费额度（--allow-paid 未启用，已跳过）。")
                log("   重试：次日免费额度刷新后用 --only N 续跑，或加 --allow-paid 用付费额度继续。")
                if not args.continue_on_error:
                    log("已停止。可加 --continue-on-error 跳过付费镜头继续后续镜。")
                    break
            if status == "failed":
                failed.append(shot["index"])
                if not args.continue_on_error:
                    log("\n已停止。可加 --continue-on-error 跳过失败镜头继续。")
                    break

        # 汇总
        log(f"\n{'='*62}")
        ok = [r for r in results if r[1] == "ok"]
        pending = [s["index"] for s in shots if s["index"] not in {r[0] for r in ok}]
        log(f"完成 {len(ok)}/{len(shots)} 个镜头")
        if ok:
            log(f"输出目录: {os.path.abspath(out_dir)}")

        # 更新 project.json
        if pm:
            # 更新关键帧阶段状态
            if not args.skip_keyframes:
                rel_keyframes = os.path.relpath(keyframes_state_path, pm.root)
                pm.update_stage("keyframes", "completed", output=rel_keyframes)
            # 更新每镜状态
            for idx, status, path in results:
                if status == "ok" and path:
                    rel_video = os.path.relpath(path, pm.root)
                    pm.update_shot(idx, "completed", video=rel_video)
                elif status == "failed":
                    pm.update_shot(idx, "failed", error="生成失败")
                elif status == "quota":
                    pm.update_shot(idx, "failed", error="额度耗尽")
                elif status == "paid":
                    pm.update_shot(idx, "failed", error="需付费额度")
            # 更新视频阶段状态
            completed = pm.get_completed_shots()
            if len(completed) == total:
                pm.update_stage("videos", "completed")
            elif completed:
                pm.update_stage("videos", "in_progress", extra={
                    "completed_shots": completed,
                    "pending_shots": pm.get_pending_shots(total),
                })

        if failed:
            log(f"失败镜号: {', '.join(map(str, failed))}")
        if paid_pending:
            log(f"需付费镜号: {', '.join(map(str, paid_pending))}（免费额度已用完，未启用 --allow-paid）")
        if quota_hit:
            log("中止原因: 免费额度耗尽（明天刷新后续跑）")
        if pending:
            log(f"待补镜号: {','.join(map(str, pending))}")
            if pm:
                log(f"续跑命令: python scripts/doubao_gen.py -p {pm.root} "
                    f"--only {','.join(map(str, pending))} --continue-on-error")
            else:
                log(f"续跑命令: python scripts/doubao_gen.py <shots.json> -o {out_dir} "
                    f"--only {','.join(map(str, pending))} --continue-on-error")
        log("=" * 62)

    finally:
        # 只关闭本次自己新建的标签页；复用来的属于用户会话，保留。
        if not args.keep_tab and tid and tid == created_tid:
            page.close_tab()
            log("\n已关闭本次创建的标签页。")
        elif tid:
            log("\n已保留复用的豆包标签页（未关闭）。")


if __name__ == "__main__":
    main()
