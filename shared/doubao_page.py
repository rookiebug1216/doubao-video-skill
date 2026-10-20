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
  const inputs=document.querySelectorAll('textarea,[contenteditable="true"]').length;
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


def is_logged_in(page: CDPPage) -> bool:
    """判断当前页面是否已登录豆包。"""
    st = login_state(page)
    return st["inputs"] > 0 and not st["loginBtn"] and not st["wall"]


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
    把提示词填入输入框。

    覆盖三类载体：textarea / input / contenteditable（富文本）。
    - 表单控件：原生 setter 赋值触发 React 受控更新，再派发 input 事件
    - contenteditable：execCommand insertText（等价真实键入，React 一定收到）
    """
    payload = json.dumps(text, ensure_ascii=False)
    js = f"""
    (()=>{{
      const text = {payload};
      const sel = 'textarea, input[type=text], [contenteditable="true"], [contenteditable=""]';
      const candidates = [...document.querySelectorAll(sel)]
        .filter(e => e.offsetParent !== null || e.getClientRects().length);
      if (!candidates.length) return null;

      candidates.sort((a,b)=>{{
        const ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
        return (rb.width*rb.height)-(ra.width*ra.height);
      }});
      const el = candidates[0];
      const isEditable = el.tagName==='TEXTAREA' || el.tagName==='INPUT';

      if (isEditable) {{
        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value');
        if (setter && setter.set) setter.set.call(el, text);
        else el.value = text;
        el.dispatchEvent(new Event('input', {{bubbles:true}}));
        el.dispatchEvent(new Event('change', {{bubbles:true}}));
      }} else {{
        el.focus();
        document.execCommand('selectAll', false, null);
        document.execCommand('insertText', false, text);
      }}
      el.focus();
      return {{tag: el.tagName, len: (el.value!==undefined?el.value:el.innerText||'').length}};
    }})()
    """
    try:
        res = page.eval_js(js)
        return res is not None
    except CDPError as e:
        log(f"  ! 填入提示词失败: {e}")
        return False


def submit_prompt(page: CDPPage) -> bool:
    """
    提交提示词：Enter 键优先（真实用户行为，兼容性最好），
    失败回退到「发送」按钮点击。
    """
    js_enter = """
    (()=>{
      const sel='textarea,input[type=text],[contenteditable="true"]';
      const els=[...document.querySelectorAll(sel)].filter(e=>e.offsetParent!==null);
      if(!els.length) return null;
      const el=els[els.length-1];
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
            time.sleep(2)
            return True
    except CDPError:
        pass

    js_btn = """
    (()=>{
      const kws=['发送','提交','生成'];
      const btns=[...document.querySelectorAll('button,[role=button]')]
        .filter(b=>b.offsetParent!==null && !b.disabled);
      const hit=btns.find(b=>{
        const t=(b.innerText||b.getAttribute('aria-label')||'').trim();
        return t && kws.some(k=>t.includes(k));
      });
      if(hit){hit.click();return 'button:'+(hit.innerText||'').trim();}
      return null;
    })()
    """
    try:
        return bool(page.eval_js(js_btn))
    except CDPError:
        return False


def new_conversation(page: CDPPage, timeout: int = 60) -> bool:
    """新建豆包对话，清空上一镜残留状态。

    豆包在"上一镜已完成"的旧对话中接收新提示词时，可能只流式输出参数卡
    内容片段而不返回「视频生成参数确认」标题，导致 read_reply_state 检测
    不到参数卡、脚本不自动确认、镜视频永远不开始生成（2026-10-20 测试发现）。

    修复：从第 2 镜起，提交前先 navigate 到 /chat/ 触发新建对话，让豆包
    在干净上下文中走完整的参数确认卡流程。
    """
    try:
        page.call("Page.navigate", recv_timeout=30, url="https://www.doubao.com/chat/")
        # 等 SPA 渲染出输入框
        return page_ready(page, timeout=timeout)
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
