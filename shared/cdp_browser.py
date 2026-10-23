# -*- coding: utf-8 -*-
"""
CDP 直连基础设施 —— 零第三方依赖的 Chrome DevTools Protocol 客户端。

包含：
  - 极简 WebSocket 客户端（RFC6455 子集：文本帧/分片/ping-pong/关闭）
  - HTTP 端点访问（/json/version, /json/new, /json/close）
  - CDPPage：一个标签页对应的 CDP 会话（Runtime.evaluate / 截图 / Cookie）
  - 浏览器发现与拉起（probe_port / find_browser_exe / ensure_browser / new_tab）

从 doubao_gen.py 与 char_ref_gen.py 中提取的公共代码，所有 skill 共用。
"""

import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

DOUBAO_HOME = "https://www.doubao.com"

# 本地 CDP 一律不走系统代理（否则装了代理的机器连不上 127.0.0.1）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg: str):
    print(msg, flush=True)


# ──────────────────────────────────────────────────────────────
# 网络响应 URL 匹配（用于从并发请求里挑出目标资源的响应）
# ──────────────────────────────────────────────────────────────

def _url_base(url: str) -> str:
    """去掉 query 与 `~` 缩略图后缀后的 URL 基名。"""
    if not url:
        return ""
    base = url.split("?", 1)[0]
    if "~" in base:
        base = base.split("~", 1)[0]
    return base


def _url_matches(target: str, candidate: str) -> bool:
    """
    判断响应 URL 是否就是目标 URL 指向的资源。

    规则：双方去掉 query 与 `~` 缩略图后缀后，任一 base 是另一 base 的子串即视为匹配
    （CDN 会在原图 URL 后追加 `~tplv-xxx` 等处理参数，故不能要求完全相等）。
    """
    a = _url_base(target)
    b = _url_base(candidate)
    if not a or not b:
        return False
    return a == b or a in b or b in a


# ──────────────────────────────────────────────────────────────
# 极简 WebSocket 客户端（RFC6455 子集）
# ──────────────────────────────────────────────────────────────

class CDPError(RuntimeError):
    pass


class _WS:
    """极简 WebSocket 客户端，仅实现 CDP 所需的文本帧收发。"""

    def __init__(self, host: str, port: int, path: str, recv_timeout: float = 120):
        self.sock = socket.create_connection((host, port), timeout=15)
        self.sock.settimeout(recv_timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("WS 握手失败：连接被关闭")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n")[0]
        if b" 101 " not in status:
            raise CDPError(f"WS 握手被拒: {status[:120]!r}")
        self.buf = rest
        self._frag = None

    def _recv_exact(self, n: int) -> bytes:
        data = self.buf
        while len(data) < n:
            chunk = self.sock.recv(max(65536, n - len(data)))
            if not chunk:
                raise CDPError("WS 连接中断")
            data += chunk
        self.buf, data = data[n:], data[:n]
        return data

    def _read_frame(self):
        b1, b2 = self._recv_exact(2)
        fin, op = b1 & 0x80, b1 & 0x0F
        masked = b2 & 0x80
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._recv_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(ln)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, op, payload

    def _send_frame(self, op: int, payload: bytes):
        mask = os.urandom(4)
        header = bytearray([0x80 | op])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_message(self, timeout: float) -> bytes:
        self.sock.settimeout(timeout)
        while True:
            fin, op, payload = self._read_frame()
            if op == 0x8:
                raise CDPError("WS 被服务端关闭")
            if op == 0x9:          # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if op == 0xA:          # pong
                continue
            if op in (0x1, 0x2):
                if not fin:
                    self._frag = bytearray(payload)
                    continue
                return bytes(payload)
            if op == 0x0:          # continuation
                if self._frag is None:
                    continue
                self._frag += payload
                if fin:
                    data = bytes(self._frag)
                    self._frag = None
                    return data

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# HTTP 端点访问
# ──────────────────────────────────────────────────────────────

def _http(method: str, url: str, data=None, timeout: int = 30, headers=None) -> bytes:
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def _http_json(method: str, url: str, timeout: int = 30):
    return json.loads(_http(method, url, timeout=timeout).decode("utf-8", "replace"))


# ──────────────────────────────────────────────────────────────
# CDPPage —— 标签页级 CDP 会话
# ──────────────────────────────────────────────────────────────

class CDPPage:
    """一个浏览器标签页对应的 CDP 会话。"""

    def __init__(self, port: int, target_id: str):
        self.port = port
        self.target = target_id
        self._id = 0
        self._events = []          # CDP 事件缓存队列
        self.ws = self._connect()

    def _connect(self) -> _WS:
        entries = _http_json("GET", f"http://127.0.0.1:{self.port}/json/list")
        entry = next((t for t in entries if t.get("id") == self.target), None)
        if not entry or not entry.get("webSocketDebuggerUrl"):
            raise CDPError(f"未找到 target {self.target} 的调试端点")
        m = re.match(r"ws://([^:/]+):(\d+)(/.+)", entry["webSocketDebuggerUrl"])
        return _WS(m.group(1), int(m.group(2)), m.group(3), recv_timeout=120)

    def call(self, method: str, recv_timeout: float = 120, **params):
        """调用 CDP 方法，返回 result dict。事件（无 id 的消息）会被缓存到 _events。"""
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            raw = self.ws.recv_message(recv_timeout)
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            # 事件（无 id，有 method）缓存到队列
            if "id" not in msg and "method" in msg:
                self._events.append(msg)
                continue
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise CDPError(f"CDP {method}: {msg['error'].get('message')}")
            return msg.get("result", {})

    def pop_events(self, method: str = None) -> list:
        """取出缓存的事件。method=None 取全部，method 过滤指定类型。"""
        if method:
            matched = [e for e in self._events if e.get("method") == method]
            self._events = [e for e in self._events if e.get("method") != method]
            return matched
        events = self._events
        self._events = []
        return events

    def drain_events(self, timeout: float = 0.3):
        """从 WebSocket 读取所有待处理的事件（不发送命令）。"""
        try:
            while True:
                raw = self.ws.recv_message(timeout)
                msg = json.loads(raw.decode("utf-8"))
                if "id" not in msg and "method" in msg:
                    self._events.append(msg)
        except Exception:
            pass  # timeout or connection error — 正常

    def eval_js(self, js: str, await_promise: bool = False, recv_timeout: float = 120):
        """执行 JS 并返回值。JS 需为单表达式/IIFE。"""
        res = self.call("Runtime.evaluate", recv_timeout=recv_timeout,
                        expression=js, returnByValue=True,
                        awaitPromise=await_promise, userGesture=True)
        if res.get("exceptionDetails"):
            d = res["exceptionDetails"]
            desc = (d.get("exception", {}) or {}).get("description") or d.get("text", "")
            raise CDPError(f"JS 执行错误: {desc[:200]}")
        r = res.get("result", {})
        if r.get("type") == "undefined" or "value" not in r:
            return None
        return r.get("value")

    def inject_fetch_hook(self):
        """
        注入 fetch 拦截器，捕获豆包 API 响应中的 image_ori_raw 无水印原图 URL。

        原理：
          1. 重写 window.fetch，对 /im/chain/single 和 /chat/completion 响应做拦截
          2. 在响应体中搜索 image_ori_raw / image_ori 字段
          3. 捕获到的 URL 存入 window.__ori_images 数组
        """
        self.eval_js(r"""
        (() => {
          if (window.__fetch_hooked) return 'already_hooked';
          window.__ori_images = [];
          const origFetch = window.fetch;
          window.fetch = async function(...args) {
            const resp = await origFetch.apply(this, args);
            try {
              const url = (typeof args[0] === 'string') ? args[0] : (args[0]?.url || '');
              if (url.includes('/im/chain/single') || url.includes('/chat/completion')) {
                const clone = resp.clone();
                const ct = resp.headers.get('content-type') || '';
                if (ct.includes('text/event-stream') || ct.includes('stream')) {
                  // 流式响应：用 reader 读取
                  const reader = clone.body.getReader();
                  const decoder = new TextDecoder();
                  let buf = '';
                  while (true) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    buf += decoder.decode(value, {stream: true});
                    // 按 \n 切分，处理 data: 行
                    const lines = buf.split('\n');
                    buf = lines.pop();
                    for (const line of lines) {
                      const trimmed = line.trim();
                      if (!trimmed.startsWith('data:')) continue;
                      const jsonStr = trimmed.slice(5).trim();
                      if (!jsonStr || jsonStr === '[DONE]') continue;
                      try {
                        const data = JSON.parse(jsonStr);
                        // 搜索 image_ori_raw
                        const str = JSON.stringify(data);
                        if (str.includes('image_ori_raw') || str.includes('image_ori')) {
                          extractOriImages(data);
                        }
                      } catch(e) {}
                    }
                  }
                } else {
                  // 非流式响应：直接读 text
                  const text = await clone.text();
                  try {
                    const data = JSON.parse(text);
                    extractOriImages(data);
                  } catch(e) {
                    // 可能是多行 JSON
                    if (text.includes('image_ori_raw') || text.includes('image_ori')) {
                      extractOriFromText(text);
                    }
                  }
                }
              }
            } catch(e) {}
            return resp;
          };

          function extractOriImages(data) {
            const str = JSON.stringify(data);
            // 直接搜索 image_ori_raw 字段
            const re = /"image_ori_raw"\s*:\s*(?:"([^"]+)"|(\{[^}]+\}))/g;
            let m;
            while ((m = re.exec(str)) !== null) {
              if (m[1]) {
                window.__ori_images.push({url: m[1], type: 'image_ori_raw'});
              } else if (m[2]) {
                try {
                  const obj = JSON.parse(m[2]);
                  if (obj.url) window.__ori_images.push({url: obj.url, type: 'image_ori_raw', w: obj.width, h: obj.height});
                } catch(e) {}
              }
            }
            // 也搜索 image_ori 作为降级
            const re2 = /"image_ori"\s*:\s*(?:"([^"]+)"|(\{[^}]+\}))/g;
            while ((m = re2.exec(str)) !== null) {
              if (m[1]) {
                window.__ori_images.push({url: m[1], type: 'image_ori'});
              } else if (m[2]) {
                try {
                  const obj = JSON.parse(m[2]);
                  if (obj.url) window.__ori_images.push({url: obj.url, type: 'image_ori', w: obj.width, h: obj.height});
                } catch(e) {}
              }
            }
          }

          function extractOriFromText(text) {
            extractOriImages(text);
          }

          window.__fetch_hooked = true;
          return 'hooked';
        })()
        """)

    def get_captured_images(self):
        """获取 fetch 拦截器捕获的原图 URL 列表。"""
        import json as _json
        result = self.eval_js("JSON.stringify(window.__doubao_ori_images || window.__ori_images || [])")
        if result:
            try:
                return _json.loads(result)
            except Exception:
                return []
        return []

    def clear_captured_images(self):
        """清空捕获的原图 URL。"""
        self.eval_js("window.__doubao_ori_images = []; window.__ori_images = []")

    def download_via_fetch(self, url: str, filepath: str, timeout: float = 30) -> bool:
        """
        在页面内用 fetch + blob 下载图片（绕过跨域，因为 origin 相同或 CDN 允许）。
        返回 base64 数据由 Python 保存为文件。
        """
        import time as _time
        import base64 as _b64
        result = self.eval_js(f"""
        (async () => {{
          try {{
            const resp = await fetch({json.dumps(url)}, {{credentials: 'include'}});
            if (!resp.ok) return JSON.stringify({{error: 'HTTP ' + resp.status}});
            const blob = await resp.blob();
            const ab = await blob.arrayBuffer();
            const u8 = new Uint8Array(ab);
            let bin = '';
            const chunk = 8192;
            for (let i = 0; i < u8.length; i += chunk) {{
              bin += String.fromCharCode.apply(null, u8.subarray(i, i + chunk));
            }}
            const b64 = btoa(bin);
            return JSON.stringify({{b64: b64, size: u8.length, type: blob.type}});
          }} catch(e) {{
            return JSON.stringify({{error: e.message}});
          }}
        }})()
        """, await_promise=True, recv_timeout=timeout)
        if not result:
            return False
        import json as _json
        try:
            data = _json.loads(result)
            if "error" in data:
                return False
            b64 = data.get("b64", "")
            if b64:
                raw = _b64.b64decode(b64)
                if len(raw) > 1024:
                    with open(filepath, "wb") as f:
                        f.write(raw)
                    return True
        except Exception:
            pass
        return False

    def screenshot(self, filepath: str):
        """截取页面截图，保存为 PNG。"""
        res = self.call("Page.captureScreenshot", recv_timeout=60, format="png")
        with open(filepath, "wb") as f:
            f.write(base64.b64decode(res["data"]))

    def cookies(self) -> list:
        """获取浏览器所有 Cookie。"""
        return self.call("Network.getAllCookies", recv_timeout=30).get("cookies", [])

    def set_file_input(self, filepaths: list, css_selector: str = 'input[type="file"]') -> bool:
        """
        向隐藏的 <input type="file"> 注入文件，触发 change 事件。

        使用 CDP DOM.setFileInputFiles，无需点击上传按钮或处理系统文件选择器。
        适用于豆包等聊天式界面中隐藏的文件上传入口。

        filepaths: 文件路径列表（会自动转绝对路径）
        css_selector: 文件输入元素的 CSS 选择器
        返回 True 表示注入成功。
        """
        abs_paths = [os.path.abspath(p) for p in filepaths]
        try:
            self.call("DOM.enable", recv_timeout=30)
        except CDPError:
            pass

        # 获取 input 元素的 objectId
        res = self.call("Runtime.evaluate", recv_timeout=30,
                        expression=f"document.querySelector('{css_selector}')",
                        returnByValue=False, userGesture=True)
        oid = res.get("result", {}).get("objectId")
        if not oid:
            return False

        # 注入文件
        self.call("DOM.setFileInputFiles", recv_timeout=60,
                  objectId=oid, files=abs_paths)

        # DOM.setFileInputFiles 已原生触发 change（及 input）事件，
        # 不再手动 dispatch 任何事件——否则豆包组件会处理两遍，
        # 导致每张图上传两次/多扣额度（2026-10-17 用户实测确认）。

        # 复核注入是否真正生效：React 水合未完成时 setFileInputFiles 会静默失败
        # （files 仍为 0），若不校验，上层会误以为已上传而提交纯文请求。
        try:
            cnt = self.eval_js(
                "(()=>{const i=document.querySelector('%s');return i?i.files.length:-1;})()"
                % css_selector.replace("'", "\\'"))
        except CDPError:
            cnt = -1
        return cnt == len(abs_paths)

    def download_raw(self, url: str, filepath: str, timeout: float = 30) -> bool:
        """
        用 CDP Network.getResponseBody 直接从浏览器缓存拿原始文件字节。
        绕过 CORS 和 403——因为请求是浏览器自己发的（带 cookie/referer）。

        流程：
          1. 启用 Network 域，清空事件队列
          2. 用 JS 在页面内新建 img 加载图片（浏览器带 cookie 发请求）
          3. 等待 Network.loadingFinished 事件，只采用 URL 与目标匹配的 requestId
          4. 用 Network.getResponseBody 获取原始 base64 数据
          5. 解码保存到文件
        """
        import time as _time
        try:
            self.call("Network.enable", recv_timeout=15)
        except CDPError:
            pass
        # 清空旧事件
        self.pop_events()

        # 在页面内新建 img 触发加载（浏览器会从缓存或重新请求）
        # 不加 cache-buster——CDN 签名 URL 改参数会导致 403
        # 浏览器从缓存加载也会触发 Network 事件
        self.eval_js(f"""
        (()=>{{
          const img=new Image();
          img.src={json.dumps(url)};
          img.style.cssText='position:absolute;top:-9999px;left:-9999px;width:1px;height:1px;opacity:0;';
          img.id='__dl_raw';
          document.body.appendChild(img);
        }})()
        """)

        # 等待网络事件：只接受与目标 URL 匹配的请求
        request_id = None
        req_urls = {}          # requestId -> 请求 URL
        matched = set()        # 命中目标 URL 的 requestId
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            _time.sleep(1)
            # 从 WebSocket 读取待处理事件
            self.drain_events(timeout=1.0)
            # 一次性取出所有相关事件，避免逐个 pop 时丢事件/顺序颠倒
            sent = self.pop_events("Network.requestWillBeSent")
            responses = self.pop_events("Network.responseReceived")
            finished = self.pop_events("Network.loadingFinished")
            # 建立 requestId -> URL 映射
            for evt in sent:
                p = evt.get("params", {})
                rid = p.get("requestId", "")
                if rid:
                    req_urls[rid] = (p.get("request") or {}).get("url", "")
            # responseReceived 中 URL 匹配的 requestId 加入匹配集合
            for evt in responses:
                p = evt.get("params", {})
                rid = p.get("requestId", "")
                rurl = (p.get("response") or {}).get("url", "")
                if not rid:
                    continue
                if rurl and not req_urls.get(rid):
                    req_urls[rid] = rurl
                if _url_matches(url, rurl):
                    matched.add(rid)
            # 只有属于匹配集合（或 URL 已知且匹配）的 loadingFinished 才采用
            for evt in finished:
                rid = evt.get("params", {}).get("requestId", "")
                if not rid:
                    continue
                if rid in matched or _url_matches(url, req_urls.get(rid, "")):
                    matched.add(rid)
                    request_id = rid
            if request_id:
                break

        # 清理页面内的临时 img
        self.eval_js("(()=>{const el=document.getElementById('__dl_raw');if(el)el.remove();})()")

        if not request_id:
            log("  · download_raw: 未捕获到目标 URL 的网络响应")
            return False

        # 用 requestId 拿响应体
        try:
            res = self.call("Network.getResponseBody", recv_timeout=30, requestId=request_id)
            data_b64 = res.get("base64Encoded", False)
            body = res.get("body", "")
            if data_b64 and body:
                raw = base64.b64decode(body)
                if len(raw) > 1024:
                    with open(filepath, "wb") as f:
                        f.write(raw)
                    return True
        except CDPError:
            pass
        return False

    def close_tab(self):
        """关闭当前标签页。"""
        try:
            _http("GET", f"http://127.0.0.1:{self.port}/json/close/{self.target}", timeout=15)
        except Exception:
            pass
        try:
            self.ws.close()
        except Exception:
            pass

    def download_native(self, url: str, filepath: str, timeout: float = 60) -> bool:
        """
        用 CDP Network.getResponseBody 从浏览器获取原始图片数据。
        在豆包页面中用 fetch(url, {mode:'no-cors', cache:'no-store'}) 触发网络请求，
        浏览器带 cookie/referer 发请求（避免 403），CDP 捕获响应体。
        CDP 不受 CORS 限制，可直接读取 opaque response 的响应体。
        """
        import time as _time

        if os.path.exists(filepath):
            os.remove(filepath)

        # 启用 Network 域，清空旧事件
        try:
            self.call("Network.enable", recv_timeout=15)
        except CDPError:
            pass
        self.pop_events()

        # 用 fetch(no-cors, no-store) 触发新的网络请求
        # no-cors 允许跨域，no-store 强制发送新请求（不用缓存）
        try:
            self.eval_js(f"""
            fetch({json.dumps(url)}, {{mode:'no-cors', cache:'no-store', credentials:'include'}}).catch(()=>{{}})
            """)
        except CDPError as e:
            log(f"  · download_native: fetch 触发失败: {e}")
            return False

        # 等待 Network 事件，只接受与目标 URL 匹配的请求
        request_id = None
        req_urls = {}          # requestId -> 请求 URL
        matched = set()        # 命中目标 URL 的 requestId
        deadline = _time.time() + timeout

        while _time.time() < deadline:
            _time.sleep(1)
            self.drain_events(timeout=1.0)

            # 一次性取出所有相关事件，避免逐个 pop 时丢事件/顺序颠倒
            sent = self.pop_events("Network.requestWillBeSent")
            responses = self.pop_events("Network.responseReceived")
            finished = self.pop_events("Network.loadingFinished")

            # 建立 requestId -> URL 映射
            for evt in sent:
                p = evt.get("params", {})
                rid = p.get("requestId", "")
                if rid:
                    req_urls[rid] = (p.get("request") or {}).get("url", "")
            # responseReceived 中 URL 匹配的 requestId 加入匹配集合
            for evt in responses:
                p = evt.get("params", {})
                rid = p.get("requestId", "")
                rurl = (p.get("response") or {}).get("url", "")
                if not rid:
                    continue
                if rurl and not req_urls.get(rid):
                    req_urls[rid] = rurl
                if _url_matches(url, rurl):
                    matched.add(rid)
            # 只有属于匹配集合（或 URL 已知且匹配）的 loadingFinished 才采用
            for evt in finished:
                rid = evt.get("params", {}).get("requestId", "")
                if not rid:
                    continue
                if rid in matched or _url_matches(url, req_urls.get(rid, "")):
                    matched.add(rid)
                    request_id = rid
            if request_id:
                break

        if not request_id:
            log("  · download_native: 未捕获到目标 URL 的网络响应")
            return False

        # 用 requestId 拿响应体
        try:
            res = self.call("Network.getResponseBody", recv_timeout=30, requestId=request_id)
            data_b64 = res.get("base64Encoded", False)
            body = res.get("body", "")
            if data_b64 and body:
                raw = base64.b64decode(body)
                if len(raw) > 1024:
                    with open(filepath, "wb") as f:
                        f.write(raw)
                    return True
                log(f"  · download_native: 响应体太小({len(raw)}B)")
            else:
                log("  · download_native: 响应体为空")
        except CDPError as e:
            log(f"  · download_native: getResponseBody 失败: {e}")
        return False
# ──────────────────────────────────────────────────────────────
# 浏览器发现 / 拉起
# ──────────────────────────────────────────────────────────────

def probe_port(port: int, timeout: float = 2.0):
    """/json/version 可用即认为调试端口在线，返回版本信息 dict 或 None。"""
    try:
        return _http_json("GET", f"http://127.0.0.1:{port}/json/version", timeout=timeout)
    except Exception:
        return None


def find_browser_exe(kind: str) -> str | None:
    """按 kind 定位可执行文件：注册表 App Paths 优先，常见路径兜底。

    支持的 Chromium 内核浏览器：
      edge / chrome / brave / vivaldi / opera / chromium

    非 Chromium 浏览器（Firefox/Safari/IE）不在支持范围 —— CDP 协议不兼容。
    """
    exe_names = {
        "edge": "msedge.exe",
        "chrome": "chrome.exe",
        "brave": "brave.exe",
        "vivaldi": "vivaldi.exe",
        "opera": "opera.exe",        # Opera stable；launcher 会转发 Chromium 启动参数
        "chromium": "chromium.exe",
    }
    exe = exe_names.get(kind)
    if not exe:
        return None
    if os.name == "nt":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        val = winreg.QueryValue(k, None)
                        if val and os.path.exists(val):
                            return val
                except OSError:
                    continue
        except ImportError:
            pass
        common = {
            "edge": [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            ],
            "chrome": [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ],
            "brave": [
                r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
            ],
            "vivaldi": [
                r"C:\Program Files\Vivaldi\Application\vivaldi.exe",
                r"C:\Program Files (x86)\Vivaldi\Application\vivaldi.exe",
            ],
            "opera": [
                r"C:\Program Files\Opera\opera.exe",
                r"C:\Program Files (x86)\Opera\opera.exe",
                r"C:\Program Files\Opera GX\opera.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe"),
            ],
            "chromium": [
                r"C:\Program Files\Chromium\Application\chromium.exe",
                r"C:\Program Files (x86)\Chromium\Application\chromium.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chromium.exe"),
            ],
        }
        for p in common[kind]:
            if os.path.exists(p):
                return p
    else:
        import shutil
        p = shutil.which(exe)
        if p:
            return p
    return None


def ensure_browser(port: int, kind: str, profile_root: str) -> str:
    """
    确保调试端口在线；不在线则拉起独立配置的浏览器。返回浏览器标识。

    Chromium 136+ 禁止在默认用户配置目录上开远程调试端口，
    必须用独立的 --user-data-dir 专用配置目录。
    """
    if probe_port(port):
        log(f"· 检测到 {port} 端口已有可调试浏览器，直接复用")
        return kind

    exe = find_browser_exe(kind)
    if not exe:
        # 降级链：用户指定浏览器找不到时，按 Chromium 内核优先序兜底
        for fallback in ("chrome", "edge", "brave", "vivaldi", "chromium"):
            if fallback == kind:
                continue
            alt = find_browser_exe(fallback)
            if alt:
                log(f"· 未找到 {kind}，降级使用 {fallback}")
                kind, exe = fallback, alt
                break
    if not exe:
        log(f"✗ 未找到任何受支持的 Chromium 浏览器（edge/chrome/brave/vivaldi/opera/chromium）。"
            f"请安装其一，或用 --browser 指定已安装的浏览器。")
        sys.exit(2)

    profile = os.path.join(profile_root, f"{kind}-profile")
    os.makedirs(profile, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "about:blank",
    ]
    log(f"· 启动浏览器（独立配置，不影响你日常使用的浏览器）:")
    log(f"    {exe}")
    log(f"    配置目录: {profile}")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(args, creationflags=flags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as e:
        log(f"✗ 启动浏览器失败: {e}")
        sys.exit(2)

    deadline = time.time() + 40
    while time.time() < deadline:
        if probe_port(port):
            log(f"· 浏览器已就绪，调试端口 {port}")
            return kind
        time.sleep(0.5)
    log("✗ 浏览器已启动但调试端口未就绪。排查：")
    log(f"    1) 该 {port} 端口是否被其他程序占用 -> 换端口重试：--port 9223")
    log("    2) 是否已有同配置目录的浏览器窗口开着 -> 先关掉它再重试")
    sys.exit(2)


def new_tab(port: int, url: str) -> str:
    """新开标签页，返回 targetId。"""
    quoted = urllib.parse.quote(url, safe=":/?&=%")
    for method in ("PUT", "GET"):  # Chromium 111+ 要求 PUT，旧版兼容 GET
        try:
            data = _http_json(method, f"http://127.0.0.1:{port}/json/new?{quoted}", timeout=60)
            if data.get("id"):
                return data["id"]
        except urllib.error.HTTPError as e:
            if method == "GET":
                raise CDPError(f"新建标签页失败: HTTP {e.code}")
        except Exception as e:
            if method == "GET":
                raise CDPError(f"新建标签页失败: {e}")
    raise CDPError("新建标签页失败：未返回 targetId")


def cookie_header(cookies: list, url: str) -> str:
    """从 CDP cookies 列表构建 HTTP Cookie 头，用于直链下载。"""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    parts = [f"{c['name']}={c['value']}" for c in cookies
             if host.endswith(c.get("domain", "").lstrip(".")) or c.get("domain", "") == ""]
    return "; ".join(parts)
