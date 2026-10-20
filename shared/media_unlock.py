# -*- coding: utf-8 -*-
"""
豆包无水印媒体链路 —— clean-room 实现（视频为主，图片字段一并捕获）。

合规说明：
  本模块只依据"接口事实"（API 路径、请求参数、响应字段名、固定盐值）实现，
  所有 JS/Python 代码均为本项目自行编写，未复制任何第三方扩展的代码表达；
  不包含任何 Seedance 15s 时长改写相关逻辑。

视频无水印链路（依据豆包网页版实际响应行为核对）：
  1. 生成流（SSE）响应中深搜 fallback_api（snssdk.com 域名签名地址）
  2. 将其查询参数改为 channel=no & codec_type=8 & logo_type=unwatermarked 后 GET
  3. 响应 video_list 中取码率/分辨率最高条目的 main_url token：
       - http(s) 直链直接用
       - 宽松 base64 解出来是 URL 的直接用
       - qAAB 前缀：用响应中的 key_seed 派生 AES-CBC 密钥解密
           digest1 = SHA512(seed_bytes[0:32])
           digest2 = SHA512(digest1 || 固定盐)
           key = digest2[0:16], iv = digest2[16:32]
  4. 备用：POST {doubao}/samantha/media/get_play_info，body {"key": vid}，
     取 data.original_media_info.main_url

注入方式：Page.addScriptToEvaluateOnNewDocument 在 document_start 的 MAIN world
安装 fetch/XHR 钩子，因此刷新历史会话也能捕获到旧视频的 fallback_api，
便于在不消耗生成额度的情况下验证链路。
"""

import json
import os
import urllib.request

from shared.cdp_browser import CDPPage, CDPError, DOUBAO_HOME, _OPENER, log, cookie_header

# 固定盐（接口事实，64 字节的十六进制表示）
_QAAB_SALT_HEX = (
    "4dd4c2e6b83162090e52b3c7a6733ba4"
    "1cb2462b829ab58a196b39db57177524"
    "f49baf7f08e8d68d26a72e37c1a95a2f"
    "1f05a51892aef2949732b62a38aadd58"
)

# ──────────────────────────────────────────────────────────────
# 注入到页面 MAIN world 的脚本（自行实现，仅使用浏览器原生能力）
# ──────────────────────────────────────────────────────────────

_UNLOCK_JS = r"""
(() => {
  if (window.__dbm_unlock__) return;
  const SALT_HEX = __SALT__;
  const records = [];   // {api, vid, at}
  const seenApi = new Set();

  const U = window.__dbm_unlock__ = {
    records,
    clear() { records.length = 0; seenApi.clear(); },
    latest() { return records.length ? records[records.length - 1] : null; },
    count() { return records.length; }
  };

  // ── 基础工具 ────────────────────────────────────────────────
  const isHttp = (s) => typeof s === 'string' && /^https?:\/\//i.test(s);

  function unescapeFragment(v) {
    let t = String(v);
    for (let i = 0; i < 3; i++) {
      let d;
      try { d = JSON.parse('"' + t.replace(/"/g, '\\"') + '"'); }
      catch (e) { break; }
      if (d === t) break;
      t = d;
    }
    return t.replace(/\\u0026/g, '&').replace(/\\\//g, '/');
  }

  function tryParseEmbedded(s) {
    if (typeof s !== 'string') return null;
    const t = s.trim();
    if (!t || (t[0] !== '{' && t[0] !== '[')) return null;
    try { return JSON.parse(t); } catch (e) { return null; }
  }

  function classifySource(u) {
    const s = String(u || '');
    // /im/chain/* 是对话主链路（提交提示词后的生成流，本镜结果只可能在这里）
    if (/\/im\/chain\//.test(s)) return 'chain';
    // /im/conversation/* 是历史会话批量回取，里面的 fallback_api 全是旧片，必须排除
    if (/\/im\/conversation\//.test(s)) return 'history';
    return 'other';
  }

  function addRecord(api, vid, src) {
    const url = unescapeFragment(api);
    if (!isHttp(url)) return;
    if (seenApi.has(url)) return;
    seenApi.add(url);
    records.push({ api: url, vid: vid ? String(vid) : '', src: src || 'other', at: Date.now() });
  }

  // 深搜对象（含"字符串里套 JSON"的情况），命中 targetKey 即回调
  function walk(node, visit, depth, seen) {
    if (node == null || depth > 12) return;
    if (typeof node === 'string') {
      const embedded = tryParseEmbedded(node);
      if (embedded) walk(embedded, visit, depth + 1, seen);
      return;
    }
    if (typeof node !== 'object' || seen.has(node)) return;
    seen.add(node);
    visit(node);
    if (Array.isArray(node)) {
      for (const item of node) walk(item, visit, depth + 1, seen);
    } else {
      for (const k of Object.keys(node)) walk(node[k], visit, depth + 1, seen);
    }
  }

  function scanObject(obj, src) {
    let vid = '';
    walk(obj, (n) => {
      if (!vid && (typeof n.vid === 'string' || typeof n.vid === 'number')) vid = String(n.vid);
      if (!vid && (typeof n.video_id === 'string' || typeof n.video_id === 'number')) vid = String(n.video_id);
      const v = n.fallback_api;
      if (typeof v === 'string' && v) addRecord(v, vid, src);
    }, 0, new Set());
  }

  // 同时用正则兜底原始文本（SSE 片段可能整体 parse 不出来）
  const RAW_PATTERNS = [
    /fallback_api\\":\\"(.*?)\\"/g,
    /"fallback_api"\s*:\s*"([^"]+)"/g,
  ];
  function scanRawText(text, src) {
    if (typeof text !== 'string' || text.indexOf('fallback_api') < 0) return;
    for (const re of RAW_PATTERNS) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(text)) !== null) addRecord(m[1], '', src);
    }
  }

  function scanBodyText(text, src) {
    if (!text) return;
    scanRawText(text, src);
    // SSE：逐行解析 data: 载荷
    const lines = text.split('\n');
    for (const line of lines) {
      const t = line.trim();
      if (!t.startsWith('data:')) continue;
      const payload = t.slice(5).trim();
      if (!payload || payload === '[DONE]') continue;
      let obj;
      try { obj = JSON.parse(payload); } catch (e) { continue; }
      scanObject(obj, src);
    }
    // 非流式整 JSON
    if (lines.length <= 2) {
      const whole = tryParseEmbedded(text);
      if (whole) scanObject(whole, src);
    }
  }

  // ── 安装 fetch 钩子（clone 后异步读取，不影响页面自身消费） ──
  const origFetch = window.fetch;
  if (typeof origFetch === 'function') {
    window.fetch = function (...args) {
      const reqUrl = typeof args[0] === 'string' ? args[0]
        : (args[0] && args[0].url) || '';
      const p = origFetch.apply(this, args);
      p.then((resp) => {
        try {
          const ct = resp.headers.get('content-type') || '';
          if (!/json|event-stream|text\/plain|octet-stream/i.test(ct)) return;
          // 以最终响应 URL 为准（重定向后也准确）
          const src = classifySource(resp.url || reqUrl);
          resp.clone().text().then((tx) => scanBodyText(tx, src)).catch(() => {});
        } catch (e) {}
      }).catch(() => {});
      return p;
    };
  }

  // ── 安装 XHR 钩子（兜底） ──
  try {
    const XO = XMLHttpRequest.prototype.open;
    const XS = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (...a) {
      this.__dbm_u = true;
      this.__dbm_url = a[1] || '';
      return XO.apply(this, a);
    };
    XMLHttpRequest.prototype.send = function (...a) {
      this.addEventListener('loadend', () => {
        try {
          if (this.responseType && this.responseType !== '' && this.responseType !== 'text') return;
          if (typeof this.responseText === 'string') {
            scanBodyText(this.responseText, classifySource(this.responseURL || this.__dbm_url));
          }
        } catch (e) {}
      });
      return XS.apply(this, a);
    };
  } catch (e) {}

  // ── token 解码（qAAB / 宽松 base64） ────────────────────────
  function b64Loose(text) {
    const input = String(text || '').trim();
    const variants = [
      input,
      input.replace(/[$@#]/g, (c) => ({ '$': '_', '@': '/', '#': '.' }[c])),
      input.replace(/[$@#]/g, (c) => ({ '$': '+', '@': '/', '#': '=' }[c])),
    ];
    const tried = new Set();
    for (let cand of variants) {
      if (!cand || tried.has(cand)) continue;
      tried.add(cand);
      try {
        const pad = (4 - (cand.length % 4)) % 4;
        cand = (cand + '='.repeat(pad)).replace(/-/g, '+').replace(/_/g, '/');
        const bin = atob(cand);
        const out = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
        return out;
      } catch (e) {}
    }
    return null;
  }

  const asciiUrl = (bytes) => {
    if (!bytes || !bytes.length) return '';
    for (const b of bytes) {
      if (b !== 9 && b !== 10 && b !== 13 && (b < 32 || b > 126)) return '';
    }
    return new TextDecoder().decode(bytes);
  };

  function stripPkcs7(bytes) {
    const pad = bytes[bytes.length - 1];
    if (pad < 1 || pad > 16 || pad > bytes.length) return bytes;
    for (let i = bytes.length - pad; i < bytes.length; i++) {
      if (bytes[i] !== pad) return bytes;
    }
    return bytes.slice(0, bytes.length - pad);
  }

  const hexToBytes = (hex) => {
    const out = new Uint8Array(hex.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    return out;
  };
  const concat = (a, b) => { const o = new Uint8Array(a.length + b.length); o.set(a, 0); o.set(b, a.length); return o; };

  async function decodeToken(token, keySeed) {
    if (isHttp(token)) return token;
    const direct = b64Loose(token);
    if (direct) {
      const u = asciiUrl(direct);
      if (isHttp(u)) return u;
    }
    if (token.slice(0, 4) === 'qAAB' && keySeed) {
      const data = b64Loose(token);
      const seed = b64Loose(keySeed);
      if (!data || !seed) return '';
      const d1 = new Uint8Array(await crypto.subtle.digest('SHA-512', seed.slice(0, 32)));
      const d2 = new Uint8Array(await crypto.subtle.digest('SHA-512', concat(d1, hexToBytes(SALT_HEX))));
      const key = d2.slice(0, 16), iv = d2.slice(16, 32);
      const attempts = [];
      if (data.length >= 4 && data[0] === 0xa8 && data[1] === 0x00 && data[2] === 0x01 && data[3] === 0x00) {
        attempts.push([data.slice(4), key, iv]);
        attempts.push([data.slice(4), iv, key]);
        if (data.length > 36) {
          attempts.push([data.slice(36), key, data.slice(20, 36)]);
          attempts.push([data.slice(36), key, iv]);
        }
      } else {
        attempts.push([data, key, iv]);
      }
      for (const [payload, k, ivv] of attempts) {
        const u = await aesCbcUrl(payload, k, ivv);
        if (u) return u;
      }
    }
    return '';
  }

  async function aesCbcUrl(payload, keyBytes, ivBytes) {
    if (!payload.length || payload.length % 16 !== 0) return '';
    try {
      const ck = await crypto.subtle.importKey('raw', keyBytes, 'AES-CBC', false, ['decrypt']);
      const plain = new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-CBC', iv: ivBytes }, ck, payload));
      const direct = asciiUrl(plain);
      if (isHttp(direct)) return direct;
      const u = asciiUrl(stripPkcs7(plain));
      return isHttp(u) ? u : '';
    } catch (e) { return ''; }
  }

  // 在任意层级里找 key_seed（对象字段或字符串里的参数/转义 JSON）
  function findKeySeed(value, depth) {
    if (depth === undefined) depth = 0;
    if (depth > 10 || value == null) return '';
    if (typeof value === 'string') {
      let m = value.match(/(?:^|[?&])key_seed=([^&"'<>\s\\]+)/i);
      if (m) return decodeURIComponent(m[1]);
      m = value.match(/["']key_seed["']\s*:\s*["']([^"']+)/i);
      return m ? decodeURIComponent(m[1]) : '';
    }
    if (typeof value !== 'object') return '';
    if (typeof value.key_seed === 'string' && value.key_seed.trim()) return value.key_seed.trim();
    for (const k of Object.keys(value)) {
      const hit = findKeySeed(value[k], depth + 1);
      if (hit) return hit;
    }
    return '';
  }

  function pickEntry(data) {
    const list = data && data.video_list;
    const entries = (list && typeof list === 'object' && Object.keys(list).length)
      ? Object.values(list) : [data];
    let best = null;
    for (const e of entries) {
      if (!e || typeof e !== 'object') continue;
      const token = e.main_url || e.play_url || '';
      if (typeof token !== 'string' || !token.trim()) continue;
      const w = Number(e.vwidth || e.width || 0), h = Number(e.vheight || e.height || 0);
      const score = Number(e.bitrate || e.real_bitrate || 0) + w * h;
      if (!best || score > best.score) best = { token: token.trim(), score, e };
    }
    return best;
  }

  async function viaFallbackApi(apiUrl) {
    const u = new URL(apiUrl);
    u.searchParams.set('channel', 'no');
    u.searchParams.set('codec_type', '8');
    u.searchParams.set('logo_type', 'unwatermarked');
    const resp = await origFetch(u.toString(), {
      method: 'GET',
      credentials: 'omit',
      headers: { accept: 'application/json,text/plain,*/*' },
    });
    const payload = await resp.json();
    const info = payload && (payload.video_info || (payload.data && payload.data.video_info) || payload);
    const data = (info && info.data) || info || {};
    const picked = pickEntry(data);
    if (!picked) return null;
    const url = await decodeToken(picked.token, findKeySeed(payload));
    if (!url) return null;
    const e = picked.e;
    return {
      url,
      via: 'fallback_api',
      width: Number(e.vwidth || e.width || data.vwidth || data.width || 0),
      height: Number(e.vheight || e.height || data.vheight || data.height || 0),
      duration: Number(e.duration || data.duration || 0),
    };
  }

  async function viaPlayInfo(vid) {
    if (!vid) return null;
    const q = new URLSearchParams({
      version_code: '20800', language: 'zh-CN', device_platform: 'web',
      aid: '497858', real_aid: '497858', pkg_type: 'release_version',
      device_id: '', pc_version: '2.51.7', region: '', sys_region: '',
      samantha_web: '1', 'use-olympus-account': '1', web_tab_id: '',
    }).toString();
    const resp = await origFetch(location.origin + '/samantha/media/get_play_info?' + q, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', origin: location.origin },
      body: JSON.stringify({ key: vid }),
    });
    const result = await resp.json();
    const main = result && result.data && result.data.original_media_info && result.data.original_media_info.main_url;
    if (!main) return null;
    const url = await decodeToken(main, '');
    return url ? { url, via: 'get_play_info', width: 0, height: 0, duration: 0 } : null;
  }

  // 由 Python 侧调用：解析「本镜生成流」中的 fallback 记录。
  // 选取优先级：chain（/im/chain/* 生成流）→ other → 显式允许时才用 history。
  // history（/im/conversation/* 历史回取）里的都是旧片，默认绝不选用，
  // 否则连续生成时可能下载到上一镜甚至别的会话的视频。
  U.resolveLatest = async function (opts) {
    const allowHistory = !!(opts && opts.allowHistory);
    let pool = records.filter(r => r.src === 'chain');
    let degraded = '';
    if (!pool.length) {
      const nonHistory = records.filter(r => r.src !== 'history');
      if (nonHistory.length) { pool = nonHistory; degraded = 'non-chain'; }
      else if (allowHistory) { pool = records.slice(); degraded = 'history'; }
    }
    const rec = pool.length ? pool[pool.length - 1] : null;
    if (!rec) return { error: 'no_capture' };
    try {
      const r = await viaFallbackApi(rec.api);
      if (r && r.url) { r.source = rec.src; r.degraded = degraded; r.poolSize = pool.length; return r; }
    } catch (e) {
      const byApi = { error: 'fallback_failed: ' + (e && e.message || e) };
      try {
        const r2 = await viaPlayInfo(rec.vid);
        if (r2 && r2.url) {
          r2.source = rec.src; r2.degraded = degraded; r2.poolSize = pool.length;
          return r2;
        }
        byApi.vid = rec.vid || '';
      } catch (e2) {
        byApi.playInfoError = String(e2 && e2.message || e2);
      }
      return byApi;
    }
    try {
      const r2 = await viaPlayInfo(rec.vid);
      if (r2 && r2.url) {
        r2.source = rec.src; r2.degraded = degraded; r2.poolSize = pool.length;
        return r2;
      }
    } catch (e) {}
    return { error: 'resolve_failed', vid: rec.vid || '' };
  };
})();
""".replace("__SALT__", json.dumps(_QAAB_SALT_HEX))


# ──────────────────────────────────────────────────────────────
# Python 侧封装
# ──────────────────────────────────────────────────────────────

def arm(page: CDPPage, home_url: str = DOUBAO_HOME) -> bool:
    """
    在标签页上安装 document_start 钩子并重新导航一次，保证钩子早于页面请求。
    须在 page_ready / 登录检查之前调用。
    """
    try:
        try:
            page.call("Page.enable", recv_timeout=30)
        except CDPError:
            pass
        page.call("Page.addScriptToEvaluateOnNewDocument", recv_timeout=30, source=_UNLOCK_JS)
        # 新标签在挂钩子前可能已开始加载首页，主动重导航一次让钩子在 document_start 生效
        page.call("Page.navigate", recv_timeout=30, url=home_url)
        log("· 无水印媒体捕获钩子已安装（document_start, MAIN world）")
        return True
    except CDPError as e:
        log(f"  ! 无水印钩子安装失败，将沿用带水印下载链路：{e}")
        return False


def is_armed(page: CDPPage) -> bool:
    try:
        return bool(page.eval_js("!!(window.__dbm_unlock__)"))
    except CDPError:
        return False


def clear_captured(page: CDPPage):
    """清空已捕获的 fallback_api 记录（每镜提交前调用，确保 latest 对应当前镜头）。"""
    try:
        page.eval_js("window.__dbm_unlock__ && window.__dbm_unlock__.clear()")
    except CDPError:
        pass


def captured_count(page: CDPPage) -> int:
    try:
        return int(page.eval_js("window.__dbm_unlock__ ? window.__dbm_unlock__.count() : 0") or 0)
    except CDPError:
        return 0


def resolve_unwatermarked(page: CDPPage, timeout: float = 90,
                          allow_history: bool = False) -> dict | None:
    """
    用页面内 JS 完成 fallback_api 请求与 token 解密，返回无水印直链信息：
      {url, via, source, degraded, width, height, duration} 或 None。

    allow_history=False（默认）：只采用 /im/chain/* 生成流中的记录，
      /im/conversation/* 历史回取里的旧片永不选用；
    allow_history=True：零额度排障时用历史会话验证链路（可能选到旧片，仅限调试）。
    """
    js = """
    (async () => {
      try {
        if (!window.__dbm_unlock__) return JSON.stringify({error: 'not_armed'});
        const r = await window.__dbm_unlock__.resolveLatest(%s);
        return JSON.stringify(r || {error: 'null_result'});
      } catch (e) {
        return JSON.stringify({error: String(e && e.message || e)});
      }
    })()
    """ % ("{allowHistory: true}" if allow_history else "{}")
    try:
        raw = page.eval_js(js, await_promise=True, recv_timeout=timeout)
        info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (CDPError, json.JSONDecodeError) as e:
        log(f"  · 无水印解析调用失败: {e}")
        return None
    if not isinstance(info, dict):
        return None
    if info.get("url"):
        return info
    log(f"  · 无水印链路未取到直链（{info.get('error') or info}）")
    return None


def _looks_like_mp4(data: bytes) -> bool:
    return len(data) > 10 * 1024 and data[4:8] == b"ftyp"


def download_unwatermarked_video(page: CDPPage, filepath: str, timeout: float = 240,
                                 allow_history: bool = False) -> str | None:
    """
    解析最新镜头的无水印直链并下载，成功返回 filepath。
    下载通道：Python 直连（签名 URL 通常无需 Cookie）→ 页面 no-cors fetch + CDP 取字节。
    任何失败都返回 None，由调用方降级到带水印下载策略。
    """
    info = resolve_unwatermarked(page, timeout=min(90, timeout),
                                 allow_history=allow_history)
    if not info:
        return None
    url = info["url"]
    via = info.get("via", "?")
    src = info.get("source", "?")
    if info.get("degraded"):
        log(f"  · ⚠ 未在生成流(/im/chain)中捕获 fallback，降级使用 {src} 来源，"
            f"请核对是否为本镜视频")
    log(f"  · 无水印直链已解析（{via}/{src}，{info.get('width', 0)}x{info.get('height', 0)}）")

    # 通道1：Python 直连
    try:
        headers = {
            "User-Agent": page.eval_js("navigator.userAgent") or "Mozilla/5.0",
            "Referer": DOUBAO_HOME + "/",
        }
        ck = cookie_header(page.cookies(), url)
        if ck:
            headers["Cookie"] = ck
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            data = r.read()
        if _looks_like_mp4(data):
            if os.path.exists(filepath):
                os.remove(filepath)
            with open(filepath, "wb") as f:
                f.write(data)
            return filepath
        log(f"  · 直连返回内容不像 mp4（{len(data)}B），尝试页面通道")
    except Exception as e:
        log(f"  · 无水印直连失败（{e}），尝试页面通道")

    # 通道2：页面内 no-cors fetch 触发请求，CDP Network.getResponseBody 取字节
    try:
        if page.download_native(url, filepath, timeout=120):
            with open(filepath, "rb") as f:
                head = f.read(12)
            size = os.path.getsize(filepath)
            if head[4:8] == b"ftyp" or size > 500 * 1024:
                return filepath
    except (CDPError, OSError) as e:
        log(f"  · 页面通道失败: {e}")
    return None
