# -*- coding: utf-8 -*-
"""
豆包页面操作原语 —— 所有 skill 共用的页面交互逻辑。

包含：
  - page_ready      等待 SPA 渲染出输入框
  - login_state     检测登录墙
  - is_logged_in    判断是否已登录
  - wait_login      登录墙轮询等待
  - fill_prompt     填入提示词（React 受控组件兼容）
  - submit_prompt   提交（Enter 优先，按钮兜底）
  - diag_snapshot   失败诊断（截图 + DOM 快照）
"""

import json
import os
import time

from . import cdp_browser
from .cdp_browser import CDPPage, CDPError, log


def page_ready(page: CDPPage, timeout: int = 60) -> bool:
    """等待 SPA 渲染出输入框等关键结构。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = page.eval_js(
                "(()=>{const n=document.querySelectorAll('textarea,input,[contenteditable=\"true\"]').length;"
                "return JSON.stringify({n,ready:document.readyState});})()")
            data = json.loads(info) if isinstance(info, str) else (info or {})
            if data.get("n", 0) > 0 and data.get("ready") == "complete":
                return True
        except CDPError:
            pass
        time.sleep(2)
    return False


LOGIN_STATE_JS = """
(()=>{
  const body=document.body.innerText||'';
  // 2026-10-24：新版豆包编辑器是 ProseMirror，contenteditable 值为
  // "plaintext-only"（不是 "true"），与 fill_prompt 的选择器保持一致，
  // 否则已登录页面 inputs=0 被误判为未登录。
  const inputs=document.querySelectorAll(
    'textarea,[contenteditable="true"],[contenteditable="plaintext-only"],.ProseMirror').length;
  const loginBtn=[...document.querySelectorAll('button,a,[role=button]')].some(e=>{
    if(e.offsetParent===null) return false;
    const t=(e.innerText||'').trim();
    return t==='登录' || t==='登 录' || /扫码登录|手机号登录/.test(t);
  });
  const wall=/(扫码|手机号|验证码).{0,6}登录|登录后(即可|使用|继续)/.test(body);
  return JSON.stringify({inputs, loginBtn, wall});
})()
"""


def login_state(page: CDPPage) -> dict:
    """返回 {inputs, loginBtn, wall}。登录墙 = 有可见登录按钮 或 登录引导文案。"""
    try:
        info = page.eval_js(LOGIN_STATE_JS)
        return json.loads(info) if isinstance(info, str) else (info or {})
    except CDPError:
        return {"inputs": 0, "loginBtn": False, "wall": False}


def is_logged_in(page: CDPPage, settle_s: int = 10) -> bool:
    """判断当前页面是否已登录豆包。

    2026-10-24 修复：page_ready 返回就绪时 ProseMirror 编辑器可能尚未挂载
    （React 水合中），一次性评估 inputs=0 会把已登录页面误判为未登录
    （chrome-profile 实测：Cookie 含 d_ticket/sessionid、编辑器已可见，
    probe 却报「未登录」，两次复现）。
    无登录墙信号但编辑器未挂载时，等水合稳定（最多 settle_s 秒）再下结论。
    """
    st = login_state(page)
    if st["loginBtn"] or st["wall"]:
        return False
    if st["inputs"] > 0:
        return True
    # 灰区：无登录墙但编辑器未挂载 —— 等待水合完成
    deadline = time.time() + settle_s
    while time.time() < deadline:
        time.sleep(1.5)
        st = login_state(page)
        if st["loginBtn"] or st["wall"]:
            return False
        if st["inputs"] > 0:
            return True
    return False


def wait_login(page: CDPPage, timeout_s: int, outdir: str, screenshot_tag: str) -> bool:
    """检测到登录墙时不退出：提示用户登录并轮询等待。"""
    os.makedirs(outdir, exist_ok=True)
    try:
        page.screenshot(os.path.join(outdir, f"{screenshot_tag}.png"))
    except Exception:
        pass
    log("")
    log("╔══════════════════════════════════════════════════════════════╗")
    log("║  检测到豆包登录墙（专用浏览器配置为独立登录态）                ║")
    log("║  请在弹出的浏览器窗口中登录豆包（扫码 / 手机号均可）           ║")
    log("║  登录一次后会持久保存，之后无需重复登录                        ║")
    log("╚══════════════════════════════════════════════════════════════╝")
    deadline = time.time() + timeout_s
    last_shot = 0
    while time.time() < deadline:
        time.sleep(5)
        if is_logged_in(page):
            log("  ✓ 登录成功，继续执行")
            return True
        if time.time() - last_shot > 60:
            try:
                page.screenshot(os.path.join(outdir, f"{screenshot_tag}.png"))
            except Exception:
                pass
            log(f"  · 等待登录中…（剩余 {int(deadline - time.time())}s）")
            last_shot = time.time()
    log(f"  ! 等待登录超时（{timeout_s}s）。登录后重跑即可，无需重启浏览器。")
    return False


def fill_prompt(page: CDPPage, text: str) -> bool:
    """
    把提示词填入豆包输入框。

    2026-10-24 实测（豆包新版 ProseMirror 编辑器）——四种方式全试过：

      A. execCommand('insertText')  ✅ 有效
         DOM 有文本 + 豆包 React 状态同步更新 → 合成 Enter 可提交。
      B. Input.insertText            ❌ 无效
         文本写进 DOM，但 React 状态未更新（按钮仍禁用/点击无反应），
         提交永远发不出去。看似"填入成功"，实则只是改了 DOM。
      C. Input.dispatchKeyEvent 逐字   ❌ 中文打不进去
      D. 点击发送按钮                  ❌ 右下角被「豆包助手 浏览器插件」
         启动器浮层完全覆盖（启动器 126x46 @1311,715 与按钮同位），
         点击落到浮层上，页面毫无反应。

    因此采用 A 填入 + 合成 Enter 提交（见 submit_prompt）。
    """
    payload = json.dumps(text, ensure_ascii=False)
    js = f"""
    (()=>{{
      const text = {payload};
      const vis = e => (e.offsetParent !== null || e.getClientRects().length > 0);
      const all = [...document.querySelectorAll(
        '.ProseMirror, [contenteditable="true"], [contenteditable="plaintext-only"], '
        + 'textarea, input[type=text]')].filter(vis);
      if (!all.length) return null;

      // 优先 ProseMirror / contenteditable（豆包真正的编辑器）
      const score = e => {{
        const cls = (e.className && e.className.toString()) || '';
        if (cls.includes('ProseMirror') || cls.includes('tiptap')) return 0;
        if (e.getAttribute && e.getAttribute('contenteditable') !== null
            && e.getAttribute('contenteditable') !== 'false') return 1;
        return 2;
      }};
      all.sort((a,b)=>score(a)-score(b));
      const el = all[0];

      el.focus();
      // 先清空既有内容，避免与残留文本拼接
      try {{
        const sel = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(el);
        sel.removeAllRanges();
        sel.addRange(range);
        document.execCommand('delete', false, null);
      }} catch(e) {{}}
      el.focus();
      document.execCommand('insertText', false, text);
      el.focus();
      const len = (el.innerText || el.textContent || el.value || '').trim().length;
      return {{tag: el.tagName, cls: (el.className&&el.className.toString())||'', len}};
    }})()
    """
    try:
        res = page.eval_js(js)
        if res is None:
            log("  ! 未找到可见的输入框")
            return False
        got = res.get("len", 0) if isinstance(res, dict) else 0
        if got <= 0:
            log("  ! 填入后回读长度为 0")
            return False
        return True
    except CDPError as e:
        log(f"  ! 填入提示词失败: {e}")
        return False


def _close_asset_panel(page: CDPPage) -> bool:
    """
    关闭豆包网页版的「素材工作台」右侧面板。

    2026-10-24 实测：该面板会覆盖输入区右下角，把发送按钮完全遮住 ——
    脚本按坐标点击时落在面板的「全选/打码下载」上，提示词永远发不出去
    （表现为"已点击发送按钮"但页面毫无反应）。
    关闭按钮：<button class="dba-icon-btn">关闭</button>（位于面板标题栏右侧）。
    """
    js = r"""
    (()=>{
      const btns=[...document.querySelectorAll('button.dba-icon-btn, button[class*=dba-icon-btn]')];
      const hit=btns.find(b=>((b.innerText||'')+(b.getAttribute('aria-label')||'')+(b.getAttribute('title')||'')).trim().indexOf('关闭')>=0);
      if(!hit) return null;
      const r=hit.getBoundingClientRect();
      if(r.width<6||r.height<6) return null;
      return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)});
    })()
    """
    try:
        loc = page.eval_js(js)
        if not loc:
            return False
        loc = json.loads(loc) if isinstance(loc, str) else loc
        for evt in ("mousePressed", "mouseReleased"):
            page.call("Input.dispatchMouseEvent", recv_timeout=15, type=evt,
                      x=loc["x"], y=loc["y"], button="left", clickCount=1)
        time.sleep(1.2)
        log("  · 已关闭「素材工作台」侧面板（它会遮挡发送按钮）")
        return True
    except CDPError:
        return False


def submit_prompt(page: CDPPage) -> bool:
    """
    提交提示词。

    2026-10-24 实测（豆包新版 ProseMirror）——有效路径只有一条：

      ✅ 合成 KeyboardEvent('Enter')（dispatchEvent）
         前提：内容是用 execCommand('insertText') 填入的（会同步 React 状态）。
         提交后豆包新建会话（URL 变为 /chat/<id>），编辑器清空。

      ❌ CDP Input.dispatchKeyEvent 的 Enter —— 页面无反应
      ❌ 点击发送按钮 —— 右下角被「豆包助手 浏览器插件」启动器浮层
         （126x46 @1311,715，与 send-btn-wrapper 完全同位）覆盖，
         点击落在浮层上，页面毫无反应。

    合成 Enter 失败时，回退到点击（并先隐藏遮挡浮层）。
    """
    # ── 主路径：合成 Enter ──
    js_enter = """
    (()=>{
      const sel='.ProseMirror, textarea, input[type=text], [contenteditable="true"]';
      const els=[...document.querySelectorAll(sel)]
        .filter(e=>e.offsetParent!==null || e.getClientRects().length);
      if(!els.length) return null;
      const el=els[0];
      el.focus();
      const opts={bubbles:true,cancelable:true,key:'Enter',code:'Enter',
                  keyCode:13,which:13,shiftKey:false};
      el.dispatchEvent(new KeyboardEvent('keydown',opts));
      el.dispatchEvent(new KeyboardEvent('keypress',opts));
      el.dispatchEvent(new KeyboardEvent('keyup',opts));
      return 'enter';
    })()
    """
    try:
        if page.eval_js(js_enter):
            time.sleep(2.5)
            st = page.eval_js("""
            (()=>{
              const e=document.querySelector('.ProseMirror');
              const ed = e ? ((e.innerText||e.textContent||'').trim()) : '';
              const t=document.body.innerText||'';
              return JSON.stringify({empty: ed.length===0,
                gen:/生成中|正在生成|创作中|绘制中|绘画中|思考中|正在处理/.test(t),
                len: ed.length, url: location.href});
            })()
            """)
            info = json.loads(st) if isinstance(st, str) else {}
            if info.get("empty") or info.get("gen"):
                return True
            log("  · 合成 Enter 后页面未响应，改用点击发送按钮")
    except CDPError:
        pass

    # ── 备选路径：点击发送按钮（先隐藏遮挡浮层与侧面板）──
    _close_asset_panel(page)
    _hide_launcher(page)
    js_btn = """
    (()=>{
      const wrap=document.querySelector('[class*=send-btn-wrapper]');
      const kws=['发送','提交','生成'];
      let hit=null, how='';
      if(wrap){
        const svg=wrap.querySelector('svg');
        hit=svg||wrap.querySelector('button')||wrap;
        how='send-btn-wrapper';
      }
      if(!hit){
        const btns=[...document.querySelectorAll('button,[role=button]')]
          .filter(b=>(b.offsetParent!==null||b.getClientRects().length)
                   && !b.disabled && b.getAttribute('aria-disabled')!=='true');
        hit=btns.find(b=>{
          const t=((b.innerText||'')+' '+(b.getAttribute('aria-label')||'')+' '+(b.getAttribute('title')||'')).trim();
          return t && kws.some(k=>t.includes(k));
        });
        if(hit) how='text';
      }
      if(!hit) return null;
      const r=hit.getBoundingClientRect();
      if(r.width<4||r.height<4) return null;
      return JSON.stringify({how, x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2),
        disabled: !!(hit.disabled || hit.getAttribute('aria-disabled')==='true')});
    })()
    """
    try:
        for attempt in range(3):
            loc = page.eval_js(js_btn)
            if not loc:
                break
            loc = json.loads(loc) if isinstance(loc, str) else loc
            if loc.get("disabled"):
                time.sleep(1.0)
                continue
            for evt in ("mouseMoved", "mousePressed", "mouseReleased"):
                page.call("Input.dispatchMouseEvent", recv_timeout=15,
                          type=evt, x=loc["x"], y=loc["y"],
                          button="left", clickCount=1)
            time.sleep(2.5)
            st = page.eval_js("""
            (()=>{
              const e=document.querySelector('.ProseMirror');
              const ed = e ? ((e.innerText||e.textContent||'').trim()) : '';
              const t=document.body.innerText||'';
              return JSON.stringify({empty: ed.length===0,
                gen:/生成中|正在生成|创作中|绘制中|绘画中|思考中|正在处理/.test(t),
                len: ed.length});
            })()
            """)
            info = json.loads(st) if isinstance(st, str) else {}
            if info.get("empty") or info.get("gen"):
                return True
            log(f"  · 点击已发送但页面未响应，重试 ({attempt+1}/3)")
            time.sleep(1.5)
        return True
    except CDPError:
        return False


def _hide_launcher(page: CDPPage) -> bool:
    """
    隐藏右下角「豆包助手 浏览器插件」启动器浮层。

    2026-10-24 实测：该浮层（126x46 @1311,715）与豆包自己的发送按钮
    `[class*=send-btn-wrapper]`（36x36 @1322,721）位置完全重叠，
    elementFromPoint 命中的是浮层而非按钮，点击必然无效。
    隐藏后按钮才可被点击。
    """
    js = r"""
    (()=>{
      let n=0;
      document.querySelectorAll('button,div,span,a').forEach(e=>{
        const t=(e.innerText||'').trim();
        if(t.indexOf('浏览器插件')>=0 && t.length<30){
          let el=e, best=e;
          for(let i=0;i<4 && el;i++){
            const c=(e.className||'').toString();
            if(/launcher|dba-/.test(c) || getComputedStyle(el).cursor==='pointer'){ best=el; break; }
            el=el.parentElement;
          }
          best.style.setProperty('display','none','important');
          n++;
        }
      });
      return n;
    })()
    """
    try:
        n = page.eval_js(js)
        if n:
            time.sleep(0.5)
            log("  · 已隐藏「豆包助手」启动器浮层（它覆盖发送按钮）")
        return bool(n)
    except CDPError:
        return False


# ──────────────────────────────────────────────────────────────
# 参考图上传与「上传完成」校验
# ──────────────────────────────────────────────────────────────

# 上传完成的 DOM 信号（2026-10-23 Chrome 实测）：
# 豆包把已上传图片渲染成输入区内的 blob: 缩略图（class 含 image-，naturalWidth 为原图宽度）；
# 未上传完成的文件不会出现在输入区，页面上传中会出现「上传中」提示。
UPLOAD_STATE_JS = """
(()=>{
  const box=document.querySelector('[data-testid="chat_input"]')||document.body;
  const chips=[...box.querySelectorAll('img')].filter(i=>{
    const s=i.currentSrc||i.src||'';
    return s.startsWith('blob:') && i.complete && i.naturalWidth>200;
  }).length;
  const t=document.body.innerText||'';
  return JSON.stringify({
    ready: chips,
    uploading: /上传中|正在上传/.test(t),
    failed: /上传失败|文件上传失败|无法上传/.test(t)
  });
})()
"""


def read_upload_state(page: CDPPage) -> dict:
    try:
        raw = page.eval_js(UPLOAD_STATE_JS)
        return json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (CDPError, json.JSONDecodeError):
        return {"ready": 0, "uploading": False, "failed": False}


def _wait_file_input_ready(page: CDPPage, timeout: int = 30) -> bool:
    """
    等待豆包页面的隐藏 file input 就绪。

    2026-10-24 实测：new_conversation() 导航到 /chat/ 后，React 仍在水合，
    此时 input[type=file] 可能尚未挂载，DOM.setFileInputFiles 会静默失败
    （files 仍为 0），上层只能看到"上传超时 0/N"。

    判定：存在 input[type=file] 且它挂在可见 DOM 中（offsetParent 非 null
    或有 client rects）—— 全 hidden 的 input 通常是模板残留，不是真正的入口。
    """
    js = """
    (()=>{
      const ins=[...document.querySelectorAll('input[type="file"]')];
      if(!ins.length) return 'NO_INPUT';
      const vis=ins.filter(i=>i.offsetParent!==null || i.getClientRects().length>0);
      const usable=vis.length?vis:ins;
      return JSON.stringify({count:ins.length, visible:vis.length,
        hasAccept: usable.some(i=>i.accept && i.accept.includes('image'))});
    })()
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = page.eval_js(js)
            if isinstance(raw, str):
                info = json.loads(raw)
                if info.get("count", 0) > 0:
                    return True
        except (CDPError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    return False


def upload_images(page: CDPPage, filepaths: list, timeout: int = 90,
                  retries: int = 2, log_prefix: str = "参考图") -> bool:
    """上传本地图片到豆包对话框，并确认每张图都已变成可提交的缩略图。

    返回 True 表示全部上传成功；False 表示重试后仍未成功——调用方不得提交，
    否则豆包会以纯文方式生成（参考图丢失，人物不一致、白白消耗额度）。

    判定信号：输入区内 blob: 缩略图数量 >= 文件数，且页面无「上传中」。
    重试只在「一张都没就绪」时重新注入：已有部分附件时再注入会产生重复附件，
    导致 prompt 里的 @图片N 编号错位，故直接返回失败交给上层人工处理。
    """
    paths = [p for p in (filepaths or []) if p and os.path.exists(p)]
    expected = len(paths)
    if expected == 0:
        return True

    # 输入区可能残留上一轮的缩略图，取基线做增量判定，避免误判为“已上传”
    base_ready = read_upload_state(page).get("ready", 0)

    for attempt in range(1, retries + 2):
        # 2026-10-24：new_conversation 导航后 React 仍在水合，
        # 此时 input[type=file] 可能尚未挂载或指向错误的 input，
        # setFileInputFiles 会静默失败（files 仍为 0）→ 表现为"上传超时 0/N"。
        # 先等 file input 真正就绪再注入。
        if not _wait_file_input_ready(page, timeout=30):
            log(f"  ! {log_prefix}注入未生效（第 {attempt} 次：文件输入框未就绪）")
            time.sleep(2)
            continue

        if not page.set_file_input(paths):
            log(f"  ! {log_prefix}注入未生效（第 {attempt} 次，文件未被浏览器接收）")
            time.sleep(2)
            continue

        deadline = time.time() + timeout
        state = read_upload_state(page)
        while time.time() < deadline:
            state = read_upload_state(page)
            ready = state.get("ready", 0) - base_ready
            if state.get("failed"):
                log(f"  ! 豆包提示{log_prefix}上传失败")
                return False
            if ready >= expected and not state.get("uploading"):
                log(f"  · {log_prefix}上传完成（{ready}/{expected} 张）")
                return True
            time.sleep(1.5)

        ready = state.get("ready", 0) - base_ready
        log(f"  ! {log_prefix}上传超时（{ready}/{expected} 张，{timeout}s）")
        if ready > 0:
            # 部分已就绪：再注入会重复附件并打乱 @图片N，直接失败
            return False
        time.sleep(2)
    return False


def new_conversation(page: CDPPage, timeout: int = 60) -> bool:
    """新建豆包对话，清空上一镜残留状态。

    豆包在"上一镜已完成"的旧对话中接收新提示词时，可能只流式输出参数卡
    内容片段而不返回「视频生成参数确认」标题，导致 read_reply_state 检测
    不到参数卡、脚本不自动确认、镜视频永远不开始生成（2026-10-20 测试发现）。

    修复：从第 2 镜起，提交前先 navigate 到 /chat/ 触发新建对话，让豆包
    在干净上下文中走完整的参数确认卡流程。

    2026-10-24 追加：导航后 ProseMirror 编辑器会比输入框更晚渲染，
    page_ready 只探测 textarea/input 数量，会过早返回"就绪"，
    随后 fill_prompt 找不到编辑器而报 fill_fail。故此处额外等待编辑器就绪。
    """
    try:
        page.call("Page.navigate", recv_timeout=30, url="https://www.doubao.com/chat/")
        # 等 SPA 渲染出输入框
        if not page_ready(page, timeout=timeout):
            return False
        # 等 ProseMirror 编辑器挂载（豆包新版富文本输入区）
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ok = page.eval_js(
                    "(!!document.querySelector('.ProseMirror, [contenteditable=\"true\"]'))")
                if ok:
                    return True
            except CDPError:
                pass
            time.sleep(1)
        return True   # 编辑器未挂载但页面已就绪，交由 fill_prompt 报错
    except CDPError as e:
        log(f"  ! 新建对话失败: {e}")
        return False


def diag_snapshot(page: CDPPage, outdir: str, tag: str):
    """失败诊断：截图 + DOM 快照。"""
    os.makedirs(outdir, exist_ok=True)
    try:
        page.screenshot(os.path.join(outdir, f"{tag}.png"))
        log(f"  · 已保存截图: {os.path.join(outdir, tag + '.png')}")
    except Exception:
        pass
    try:
        val = page.eval_js("""
        (()=>{
          const strip=e=>({
            tag:e.tagName, id:e.id||'',
            cls:(e.className&&e.className.toString().slice(0,60))||'',
            text:(e.innerText||'').trim().slice(0,50),
            aria:e.getAttribute('aria-label')||'',
            testid:e.getAttribute('data-testid')||''
          });
          return JSON.stringify([...document.querySelectorAll('button,input,textarea,[role=button],[contenteditable]')]
            .slice(0,120).map(strip), null, 1);
        })()
        """)
        with open(os.path.join(outdir, f"{tag}.dom.json"), "w", encoding="utf-8") as f:
            f.write(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, indent=2))
    except Exception:
        pass


def probe_connectivity(port: int, browser: str, profile_dir: str, outdir: str,
                       keep_tab: bool = False, login_wait: int = 600) -> bool:
    """
    连通性体检：拉起浏览器 -> 打开豆包 -> 报告页面与登录状态。

    所有 skill 的 --probe 模式共用此实现。
    返回 True 表示浏览器就绪且已登录。
    """
    cdp_browser.ensure_browser(port, browser, profile_dir)
    log("\n正在打开豆包…")
    tid = cdp_browser.new_tab(port, cdp_browser.DOUBAO_HOME)
    page = CDPPage(port, tid)
    try:
        ready = page_ready(page, timeout=90)
        st = login_state(page)
        title = page.eval_js("document.title") or ""
        log(f"\n──────── 探测结果 ────────")
        log(f"页面标题   : {title}")
        log(f"页面就绪   : {'是' if ready else '否（可能网络慢，稍后重试）'}")
        log(f"输入框数量 : {st.get('inputs', 0)}")
        log(f"登录状态   : {'已登录 ✓' if is_logged_in(page) else '未登录 ✗（脚本运行时会自动等待你扫码登录）'}")
        try:
            page.screenshot(os.path.join(outdir, "probe.png"))
            log(f"截图       : {os.path.join(os.path.abspath(outdir), 'probe.png')}")
        except Exception:
            pass
        log("──────────────────────────")
        if is_logged_in(page):
            if not keep_tab:
                page.close_tab()
                log("已关闭探测标签页（浏览器保留，登录态持续有效）")
            return True
        else:
            try:
                page.call("Page.bringToFront", recv_timeout=15)
            except Exception:
                pass
            log("→ 豆包标签页已保留并在前置，请在该窗口中扫码/手机号登录豆包（一次即可，之后免登录）")
            return False
    except Exception as e:
        log(f"探测异常: {e}")
        return False
