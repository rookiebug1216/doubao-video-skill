# -*- coding: utf-8 -*-
"""
豆包图生图引擎 —— 所有 skill 共用的"提示词 → 豆包出图 → 无水印下载"全链路能力。

从 doubao-character-ref/scripts/char_ref_gen.py 下沉而来（风格无关部分）：
  * 回复状态检测与额度识别（read_reply_state / quota_exhausted）
  * 图片渲染状态机等待（wait_image）
  * 四级图片下载链路（_ROUTER_DATA 直链 → fetch+CDP → Image+CDP → 截图兜底）
  * 参考图上传 + 图生图单张生成入口（generate_one）

风格相关部分（六视图/道具/关键帧提示词构建）留在各 skill，
本模块只认"一段完整提示词 + 可选参考图"，与画风解耦。

典型用法（skill 侧脚本）：

  from shared import imagegen

  status, path = imagegen.generate_one(
      page, prompt, outdir, name, tag,
      ref_image_paths=[...], expect_portrait=True,
  )
"""

import base64
import json
import os
import re
import time
import urllib.request

from .cdp_browser import CDPPage, CDPError, DOUBAO_HOME, _OPENER, log, cookie_header
from .doubao_page import fill_prompt, submit_prompt, diag_snapshot

# 图片生成通常 30-90 秒，取 5 分钟留余量
RENDER_TIMEOUT = 300
POLL_INTERVAL = 3


# ──────────────────────────────────────────────────────────────
# 图片渲染检测与下载
# ──────────────────────────────────────────────────────────────

# 错误文案检测（额度耗尽 / 审核 / 失败）
REPLY_STATE_JS = """
(()=>{
  const t=document.body.innerText||'';
  const errRe=/(生成次数[^\\n]{0,12}(已用完|用完|不足)|额度[^\\n]{0,10}(不足|用完|已用完)|次数[^\\n]{0,6}用完|今日[^\\n]{0,10}(次数|额度)|免费次数|明天再来|明日再来|审核未通过|不通过审核|内容不(符合|合规|适合)|生成失败|无法生成)/g;
  const errors=[]; let m;
  while((m=errRe.exec(t))!==null){ errors.push(m[0]); if(errors.length>10) break; }
  const hasSpinner=/生成中|正在生成|创作中|绘画中|绘制中/.test(t);
  return JSON.stringify({errors, hasSpinner});
})()
"""

QUOTA_RE = re.compile(r"免费次数|次数[^。\n]{0,6}用完|明天再来|明日再来|额度|今日[^。\n]{0,8}(?:次数|额度)")


def read_reply_state(page: CDPPage) -> dict:
    try:
        info = page.eval_js(REPLY_STATE_JS)
        return json.loads(info) if isinstance(info, str) else (info or {})
    except CDPError:
        return {"errors": [], "hasSpinner": False}


def quota_exhausted(page: CDPPage) -> bool:
    return any(QUOTA_RE.search(e or "") for e in read_reply_state(page).get("errors", []))


def _collect_img_srcs(page: CDPPage) -> set:
    """收集页面所有 <img> 的 src（用于基线对比）。过滤掉 UI 图标、头像和小图片。"""
    try:
        raw = page.eval_js(r"""
        JSON.stringify([...document.querySelectorAll('img')]
          .filter(img=>{
            const s=(img.currentSrc||img.src||'');
            if(!s || s.startsWith('data:') || s.length<=20) return false;
            const sl=s.toLowerCase();
            // 过滤 URL 含 avatar/icon/logo/sprite 的 UI 元素
            if(sl.includes('avatar')||sl.includes('/icon/')||sl.includes('/logo/')||sl.includes('sprite')) return false;
            // 过滤用户头像域名
            if(sl.includes('byteacctimg.com')||sl.includes('passport')) return false;
            // 过滤小尺寸图片
            const w=img.naturalWidth||img.width||0;
            const h=img.naturalHeight||img.height||0;
            if(w>0 && w<200) return false;
            if(h>0 && h<200) return false;
            return true;
          })
          .map(img=>(img.currentSrc||img.src||'').slice(0,200)))
        """)
        return set(json.loads(raw)) if isinstance(raw, str) else set()
    except CDPError:
        return set()


def wait_image(page: CDPPage, baseline_srcs: set, timeout: int = RENDER_TIMEOUT):
    """
    等待图片渲染完成（状态机）：

      1. 等 spinner 出现（确认豆包开始生成）
      2. 等 spinner 消失（确认生成完成）
      3. 等图片完全加载（img.complete && naturalWidth > 0）
      4. 返回图片信息
      5. 额度耗尽 -> (None, "quota")
      6. 超时 -> (None, None)
    """
    deadline = time.time() + timeout
    last_report = 0
    try:
        pre_errors = set(read_reply_state(page).get("errors", []))
    except Exception:
        pre_errors = set()

    spinner_seen = False  # 是否已看到 spinner（确认生成开始）
    img_loaded = False    # 图片是否已完全加载

    while time.time() < deadline:
        try:
            state = read_reply_state(page)
            has_spinner = state.get("hasSpinner", False)

            # 检测额度耗尽
            for e in state.get("errors", []):
                if QUOTA_RE.search(e):
                    log(f"  ! 豆包回复：{e}")
                    return None, "quota"
                if e not in pre_errors:
                    log(f"  ! 豆包回复报错：{e}")
                    return None, None

            # 阶段1：等 spinner 出现（确认生成已开始）
            if not spinner_seen:
                if has_spinner:
                    spinner_seen = True
                    log("  · 检测到生成中，等待完成…")
                else:
                    # 可能在排队，继续等
                    pass
            # 阶段2：spinner 消失 → 生成完成，开始等图片加载
            elif spinner_seen and not has_spinner:
                if not img_loaded:
                    # spinner 刚消失，检查图片是否已完全加载
                    current_srcs = _collect_img_srcs(page)
                    fresh = current_srcs - baseline_srcs
                    if fresh:
                        # 检查图片是否已完全加载
                        loaded_js = f"""
                        (()=>{{
                          const srcs={json.dumps(list(fresh))};
                          for(const s of srcs){{
                            const img=[...document.querySelectorAll('img')]
                              .find(i=>{{const si=(i.currentSrc||i.src||''); return si.startsWith(s)||s.startsWith(si.slice(0,200));}});
                            if(img && img.complete && img.naturalWidth > 200){{
                              return JSON.stringify({{src: (img.currentSrc||img.src||'').slice(0,200),
                                w: img.naturalWidth, h: img.naturalHeight}});
                            }}
                          }}
                          return null;
                        }})()
                        """
                        try:
                            loaded_res = page.eval_js(loaded_js)
                            if loaded_res:
                                info = json.loads(loaded_res)
                                log(f"  · 图片已加载完成: {info['w']}x{info['h']}px")
                                return {"src": info["src"]}, None
                        except (CDPError, json.JSONDecodeError):
                            pass
                    # 图片还没加载完，继续等
                    img_loaded = False

            if time.time() - last_report > 30:
                phase = "等待生成" if not spinner_seen else ("生成中" if has_spinner else "等待图片加载")
                log(f"    · {phase}…（已等待 {int(time.time()-(deadline-timeout))}s）")
                last_report = time.time()
        except CDPError:
            pass
        time.sleep(POLL_INTERVAL)

    return None, None


def _router_raw_image_urls(page: CDPPage) -> list:
    """从页面首屏 window._ROUTER_DATA 提取当前生成图的 image_raw 无水印签名 URL 列表。

    原理：豆包 SPA 首屏数据中存有服务端签好的 image_raw URL（x-signature 现成，
    x-expires 至 2036 年），无需额外签名构造。按页面上当前最大生成图的 URI id
    精确匹配，避免多图会话中串图；刚生成、尚未写入首屏数据的图返回空（自动降级）。

    注意：同 id 条目中会混入视频（ftyp 头）——调用方须按文件头逐条校验。
    """
    try:
        cur_src = page.eval_js(r"""
        (()=>{
          const imgs=[...document.querySelectorAll('img')]
            .filter(img=>img.offsetParent!==null && (img.currentSrc||img.src||'').includes('rc_gen_image') && img.naturalWidth>100);
          if(!imgs.length) return '';
          imgs.sort((a,b)=>(b.naturalWidth*b.naturalHeight)-(a.naturalWidth*a.naturalHeight));
          return (imgs[0].currentSrc||imgs[0].src||'');
        })()
        """) or ""
        m = re.search(r"rc_gen_image/([0-9a-zA-Z]+)", cur_src)
        if not m:
            return []
        rd = page.eval_js("JSON.stringify(window._ROUTER_DATA||null)")
        if not rd or rd == "null":
            return []
        clean = rd.replace("\\u002F", "/").replace("\\/", "/")
        return [u for u in re.findall(r"https?://[^\"'\s\\<>]+image_raw[^\"'\s\\<>]*", clean)
                if m.group(1) in u]
    except (CDPError, json.JSONDecodeError):
        return []


def _direct_download_image(page: CDPPage, url: str, filepath: str) -> bool:
    """直链下载图片：urllib + 浏览器 Cookie + Referer（绕过 CDN 403）。

    带文件头校验：_ROUTER_DATA 中会混入 mp4 条目（ftyp 头），必须过滤掉。
    """
    try:
        headers = {
            "User-Agent": page.eval_js("navigator.userAgent") or "Mozilla/5.0",
            "Referer": DOUBAO_HOME + "/",
        }
        ck = cookie_header(page.cookies(), url)
        if ck:
            headers["Cookie"] = ck
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=180) as r:
            data = r.read()
    except Exception as e:
        log(f"  · 直链下载失败: {e}")
        return False
    if len(data) < 100 * 1024:
        log(f"  · 直链下载文件太小({len(data) // 1024}KB)")
        return False
    if data[:8] != b"\x89PNG\r\n\x1a\n" and data[:3] != b"\xff\xd8\xff":
        log(f"  · 直链响应非图片(head={data[:8]!r})")
        return False
    with open(filepath, "wb") as f:
        f.write(data)
    return True


def _img_is_portrait(filepath: str) -> bool | None:
    """读取图片宽高，判断是否竖版。无法解析返回 None。"""
    try:
        with open(filepath, "rb") as f:
            head = f.read(32)
        # PNG: 宽/高在 IHDR 第 16-24 字节（大端）
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            import struct
            with open(filepath, "rb") as f:
                f.seek(16)
                w, h = struct.unpack(">II", f.read(8))
            return h > w
        # JPEG: 扫 SOF0/2 段
        if head[:2] == b"\xff\xd8":
            import struct
            with open(filepath, "rb") as f:
                f.read(2)
                while True:
                    b = f.read(1)
                    if not b:
                        return None
                    if b != b"\xff":
                        continue
                    marker = f.read(1)
                    if marker in (b"\xc0", b"\xc1", b"\xc2"):
                        f.read(3)
                        h, w = struct.unpack(">HH", f.read(4))
                        return h > w
                    if marker in (b"\xd8", b"\xd9") or b"\xd0" <= marker <= b"\xd7":
                        continue
                    seg_len = struct.unpack(">H", f.read(2))[0]
                    f.seek(seg_len - 2, 1)
    except Exception:
        return None
    return None


def _ratio_mismatch(filepath: str, expect_portrait: bool | None) -> bool:
    """下载图方向与预期不符（抓错图的信号，如把横版六视图当竖版尾帧图）。"""
    if expect_portrait is None:
        return False
    is_portrait = _img_is_portrait(filepath)
    if is_portrait is None:
        return False  # 无法判断时不拦截
    return is_portrait != expect_portrait


def download_image(page: CDPPage, outdir: str, name: str, tag: str,
                   img_info: dict | None = None, expect_portrait: bool | None = None,
                   unlock: bool = False) -> str | None:
    """下载图片包装：成功后校验图片方向与预期是否一致（防止抓错参考图）。

    尾帧/首帧图为竖版 9:16（expect_portrait=True），六视图/道具图方向不定
    （expect_portrait=None，不校验）。方向不符时删除文件并返回 None，
    让上层判定本镜关键帧生成失败，避免基于错误参考图继续生成视频。

    unlock: 是否启用无水印链路（默认 False，带水印下载）。
    """
    result = _download_image_impl(page, outdir, name, tag, img_info, unlock=unlock)
    if result and result not in ("DOWNLOAD_TRIGGERED",) and os.path.isfile(result):
        if _ratio_mismatch(result, expect_portrait):
            orient = "竖版" if expect_portrait else "横版"
            log(f"  ✗ 下载图方向与预期({orient})不符，疑似抓错参考图，已删除: {os.path.basename(result)}")
            try:
                os.remove(result)
            except OSError:
                pass
            return None
    return result


def _download_image_impl(page: CDPPage, outdir: str, name: str, tag: str,
                   img_info: dict | None = None, unlock: bool = False) -> str | None:
    """
    下载图片，四级策略：
      0) _ROUTER_DATA 直取：读页面首屏数据中服务端签好的 image_raw 无水印 URL，直链下载（首选）
      1) fetch 拦截：在豆包页面用 fetch(no-cors) 触发请求，CDP getResponseBody 取原始字节
      2) new Image+CDP：新建 img 触发加载，CDP getResponseBody 取原始字节（备选）
      3) 注入缓存图截图：页面内新建 img 设原始尺寸后截图（兜底）

    img_info: wait_image() 返回的 {"src": ...}，优先使用该 src 而非重新搜索。
    unlock: 是否启用无水印链路（默认 False，带水印下载；需 --unlock 启用）
    """
    os.makedirs(outdir, exist_ok=True)
    filename = f"{name}_{tag}.png"
    filepath = os.path.join(outdir, filename)

    if unlock:
        # 方案0（首选）：_ROUTER_DATA 首屏数据直取 image_raw 无水印原图
        try:
            raw_urls = _router_raw_image_urls(page)
            if raw_urls:
                log(f"  · _ROUTER_DATA 命中 {len(raw_urls)} 条 image_raw URL，逐条直链下载…")
                for raw_url in raw_urls:
                    if _direct_download_image(page, raw_url, filepath):
                        log(f"  · 无水印原图下载成功（_ROUTER_DATA 直链），{os.path.getsize(filepath) // 1024} KB")
                        return filepath
                    log("  · 该条非有效图片（视频/过期），尝试下一条…")
                log("  · _ROUTER_DATA 条目均无效，尝试其他方案…")
            else:
                log("  · _ROUTER_DATA 未命中（图未入首屏数据/页面无生成图），尝试其他方案…")
        except CDPError as e:
            log(f"  · _ROUTER_DATA 方案异常: {e}")
    else:
        log("  · 无水印链路已禁用，将使用带水印下载")

    # 优先使用 wait_image() 已找到的 src
    src = img_info.get("src") if img_info else None

    # 方案1：从 fetch 拦截获取无水印原图 URL（带水印时也可用，但 URL 可能是 cgen_lwm3）
    try:
        ori_images = page.eval_js("JSON.stringify(window.__doubao_ori_images||[])")
        ori_list = json.loads(ori_images) if ori_images else []
        if ori_list:
            ori_url = ori_list[-1]  # 取最新的一张
            log(f"  · 拦截到无水印原图 URL: {ori_url[:80]}…")
            # 尝试直接下载
            if ori_url.startswith(("http://", "https://")):
                try:
                    headers = {
                        "User-Agent": page.eval_js("navigator.userAgent") or "Mozilla/5.0",
                        "Referer": DOUBAO_HOME + "/",
                    }
                    ck = cookie_header(page.cookies(), ori_url)
                    if ck:
                        headers["Cookie"] = ck
                    with _OPENER.open(urllib.request.Request(ori_url, headers=headers), timeout=180) as r:
                        data = r.read()
                    if len(data) > 1024:
                        with open(filepath, "wb") as f:
                            f.write(data)
                        log(f"  · 无水印原图下载成功，{len(data)//1024} KB")
                        return filepath
                    log(f"  · 无水印 URL 下载太小({len(data)}B)")
                except Exception as e:
                    log(f"  · 无水印 URL 下载失败: {e}")
            # blob: URL 走页面内 fetch
            if ori_url.startswith("blob:"):
                src = ori_url  # 让方案2处理
    except (CDPError, json.JSONDecodeError):
        pass

    # 方案0b：高清下载——先尝试 fetch 拦截提取原图 URL，再 CDP 下载，最后截图兜底
    try:
        # 方案A：从 fetch 拦截器捕获的 image URL 下载
        ori_images = page.get_captured_images()
        if ori_images:
            for img_info in ori_images:
                ori_url = img_info.get("url", "")
                if ori_url and ("rc_gen_image" in ori_url or "byteimg" in ori_url or "imagex" in ori_url):
                    log(f"  · 从 API 响应提取到原图 URL（{img_info.get('type','')}）")
                    # 用页面内 fetch 下载（origin 相同，绕过 CORS）
                    if page.download_via_fetch(ori_url, filepath, timeout=30):
                        size = os.path.getsize(filepath)
                        if size > 100 * 1024:
                            log(f"  · 原图下载成功（fetch+blob），{size//1024} KB")
                            page.clear_captured_images()
                            return filepath
                        log(f"  · fetch 下载文件太小({size//1024}KB)")
                    else:
                        log(f"  · fetch 下载失败，尝试 CDP 方案…")
                    break

        # 方案B：用 CDP Network.getResponseBody 直接拿原始文件字节
        # 始终搜索页面上的 rc_gen_image 图片（不依赖 wait_image 返回的 src）
        target_src = ""
        found = page.eval_js(r"""
        (()=>{
          const imgs=[...document.querySelectorAll('img')]
            .filter(img=>{
              if(img.offsetParent===null) return false;
              const s=(img.currentSrc||img.src||'');
              if(!s || !s.includes('rc_gen_image')) return false;
              return img.naturalWidth>100;
            });
          if(!imgs.length) return null;
          imgs.sort((a,b)=>(b.naturalWidth*b.naturalHeight)-(a.naturalWidth*a.naturalHeight));
          return (imgs[0].currentSrc||imgs[0].src||'').slice(0,500);
        })()
        """)
        if found:
            target_src = found

        if target_src and "rc_gen_image" in target_src:
            # 等待高清图加载（刚生成的图可能先出现缩略图）
            time.sleep(3)
            # 重新搜索最大的 rc_gen_image（可能已加载高清版）
            big_found = page.eval_js(r"""
            (()=>{
              const imgs=[...document.querySelectorAll('img')]
                .filter(img=>{
                  if(img.offsetParent===null) return false;
                  const s=(img.currentSrc||img.src||'');
                  if(!s || !s.includes('rc_gen_image')) return false;
                  return img.naturalWidth>500;
                });
              if(!imgs.length) return null;
              imgs.sort((a,b)=>(b.naturalWidth*b.naturalHeight)-(a.naturalWidth*a.naturalHeight));
              return (imgs[0].currentSrc||imgs[0].src||'').slice(0,500);
            })()
            """)
            if big_found:
                target_src = big_found
                log(f"  · 找到高清图 URL")

            # 方案1：download_native（fetch no-cors + CDP getResponseBody）——最可靠
            log(f"  · 用 fetch+CDP 下载原图…")
            if page.download_native(target_src, filepath, timeout=60):
                size = os.path.getsize(filepath)
                if size > 100 * 1024:
                    log(f"  · 原图下载成功，{size//1024} KB")
                    return filepath
                log(f"  · 下载文件太小({size//1024}KB)，尝试 CDP 备选方案…")
            else:
                log(f"  · download_native 失败，尝试 CDP 备选方案…")

            # 方案2：download_raw（new Image + CDP getResponseBody）——备选
            log(f"  · 用 new Image+CDP 下载原图…")
            if page.download_raw(target_src, filepath, timeout=60):
                size = os.path.getsize(filepath)
                if size > 100 * 1024:
                    log(f"  · 原图下载成功（CDP备选），{size//1024} KB")
                    return filepath
                log(f"  · CDP 备选下载文件太小({size//1024}KB)，尝试截图方案…")
            else:
                log(f"  · CDP 备选下载失败，尝试截图方案…")

        # 方案0b：在页面内新建 img（缓存图），设为原始尺寸后截图
        if target_src and "rc_gen_image" in target_src:
            dims = page.eval_js(f"""
            (()=>{{
              const src={json.dumps(target_src)};
              const img=[...document.querySelectorAll('img')]
                .find(i=>(i.currentSrc||i.src||'').includes(src.slice(0,80)));
              if(img) return JSON.stringify({{nw:img.naturalWidth, nh:img.naturalHeight}});
              return null;
            }})()
            """)
            target_nw, target_nh = 0, 0
            if dims:
                d = json.loads(dims)
                target_nw = d["nw"]
                target_nh = d["nh"]
            if target_nw > 500:
                log(f"  · 注入缓存图（{target_nw}x{target_nh}px）截图…")
                page.eval_js(f"""
                (()=>{{
                  const src={json.dumps(target_src)};
                  const nw={target_nw}, nh={target_nh};
                  const ov=document.createElement('div');
                  ov.id='__dl_overlay';
                  ov.style.cssText='position:fixed;top:0;left:0;z-index:999999;background:#000;padding:0;margin:0;';
                  const img=document.createElement('img');
                  img.src=src;
                  img.style.cssText='display:block;width:'+nw+'px;height:'+nh+'px;max-width:none;max-height:none;';
                  ov.appendChild(img);
                  document.body.appendChild(ov);
                  return 'injected';
                }})()
                """)
                time.sleep(2)
                rect = page.eval_js(r"""
                (()=>{
                  const ov=document.getElementById('__dl_overlay');
                  if(!ov) return null;
                  const r=ov.getBoundingClientRect();
                  return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height});
                })()
                """)
                if rect:
                    r = json.loads(rect)
                    if r.get("w", 0) > 500:
                        page.screenshot_clip(filepath, r["x"], r["y"], r["w"], r["h"])
                        if os.path.getsize(filepath) > 1024:
                            log(f"  · 截图成功，{target_nw}x{target_nh}px，{os.path.getsize(filepath)//1024} KB")
                            page.eval_js("(()=>{const o=document.getElementById('__dl_overlay');if(o)o.remove();})()")
                            return filepath
                page.eval_js("(()=>{const o=document.getElementById('__dl_overlay');if(o)o.remove();})()")
    except (CDPError, json.JSONDecodeError) as e:
        log(f"  · 高清下载失败: {e}")

    # 兜底：重新搜索页面图片（取尺寸最大的，而非最后一个）
    if not src:
        try:
            src = page.eval_js("""
            (()=>{
              const imgs=[...document.querySelectorAll('img')]
                .filter(i=>i.offsetParent!==null && (i.src||'').length>20
                       && !i.src.includes('data:')
                       && !i.src.includes('icon') && !i.src.includes('logo'));
              if(!imgs.length) return null;
              // 按面积排序取最大的（生成图通常比 UI 图标大）
              imgs.sort((a,b)=>{
                const ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
                return (rb.width*rb.height)-(ra.width*ra.height);
              });
              return imgs[0].currentSrc||imgs[0].src||'';
            })()
            """)
        except CDPError:
            src = None

    # 方案1：https 直链 + Cookie
    if src and src.startswith(("http://", "https://")) and not src.startswith("blob:"):
        log(f"  · 尝试方案1（https 直链下载）…")
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
            log(f"  · 方案1 下载到 {len(data)} 字节")
            if len(data) > 1024:
                with open(filepath, "wb") as f:
                    f.write(data)
                return filepath
            else:
                log(f"  · 方案1 数据太小({len(data)}B)，可能不是图片")
        except Exception as e:
            log(f"  · 方案1 失败: {e}")

    # 方案2：blob 源 → 页面内 fetch 取回 base64
    if src:
        log(f"  · 尝试方案2（blob fetch base64）… src 类型: {'blob' if src.startswith('blob:') else '其他'}")
        try:
            res = page.eval_js(f"""
            (async ()=>{{
              const src={json.dumps(src)};
              try {{
                const b=await (await fetch(src)).blob();
                if(b.size > 100*1024*1024) return {{tooBig:true}};
                const buf=await b.arrayBuffer();
                const bytes=new Uint8Array(buf);
                let binary=''; const chunk=0x8000;
                for(let i=0;i<bytes.length;i+=chunk){{
                  binary+=String.fromCharCode.apply(null, bytes.subarray(i,i+chunk));
                }}
                return {{b64: btoa(binary), size: b.size}};
              }} catch(e) {{
                // canvas 兜底：找到 src 匹配的 img 元素
                const img=[...document.querySelectorAll('img')]
                  .find(i=>(i.currentSrc||i.src)===src);
                if(!img) return null;
                const c=document.createElement('canvas');
                c.width=img.naturalWidth||img.width;
                c.height=img.naturalHeight||img.height;
                const ctx=c.getContext('2d');
                ctx.drawImage(img,0,0);
                const dataUrl=c.toDataURL('image/png');
                return {{b64: dataUrl.split(',')[1], size: dataUrl.length}};
              }}
            }})()
            """, await_promise=True, recv_timeout=120)
            if isinstance(res, dict) and res.get("b64"):
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(res["b64"]))
                if os.path.getsize(filepath) > 1024:
                    return filepath
        except CDPError:
            pass

    # 方案3：点击下载按钮
    js_dl = """
    (()=>{
      const kws=['下载','保存','导出'];
      const btns=[...document.querySelectorAll('button,[role=button],a')]
        .filter(b=>{
          if(b.offsetParent===null) return false;
          const t=(b.innerText||b.getAttribute('aria-label')||b.getAttribute('title')||'').trim();
          return t && kws.some(k=>t.includes(k));
        });
      if(btns.length){btns[0].click();return 'clicked';}
      return null;
    })()
    """
    try:
        if page.eval_js(js_dl):
            log("  · 已触发下载按钮，浏览器将保存到默认下载目录")
            log(f"  · 请手动把该文件改名/移动到: {filepath}")
            return "DOWNLOAD_TRIGGERED"
    except CDPError:
        pass

    return None


# ──────────────────────────────────────────────────────────────
# fetch/XHR 拦截与单张生成入口
# ──────────────────────────────────────────────────────────────

def inject_fetch_hook(page: CDPPage):
    """
    注入 fetch + XHR 拦截脚本，从豆包流式响应中提取 image_ori_raw（无水印原图 URL）。
    """
    res = page.eval_js(r"""
    (()=>{
      if(window.__doubao_fetch_hooked) return 'already';
      window.__doubao_fetch_hooked = true;
      window.__doubao_ori_images = [];
      window.__doubao_debug = {fetch_calls:0, xhr_calls:0, ori_found:0, lines_with_image:0};
      window.__doubao_raw_lines = [];  // 存含 image_ori 的原始行，用于调试

      // ── hook fetch ──
      const origFetch = window.fetch;
      window.fetch = async function(...args){
        const url = typeof args[0]==='string' ? args[0] : (args[0]?.url||'');
        if(url.includes('/chat/completion') || url.includes('/completion')){
          window.__doubao_debug.fetch_calls++;
        }
        const resp = await origFetch.apply(this, args);
        if(url.includes('/chat/completion') && resp.body && typeof resp.body.getReader==='function'){
          const reader = resp.body.getReader();
          const dec = new TextDecoder();
          const stream = new ReadableStream({
            async start(ctrl){
              let buf='';
              while(true){
                const {done, value} = await reader.read();
                if(done) break;
                buf += dec.decode(value, {stream:true});
                const lines = buf.split('\n');
                buf = lines.pop()||'';
                for(const line of lines){
                  if(!line.startsWith('data: ')) continue;
                  const jsonStr = line.substring(6);
                  // 同时匹配 image_ori 和 creations（生成结果的关键字）
                  if(!jsonStr.includes('image_ori') && !jsonStr.includes('creations') && !jsonStr.includes('creation')) continue;
                  window.__doubao_debug.lines_with_image++;
                  if(window.__doubao_raw_lines.length < 5) window.__doubao_raw_lines.push(jsonStr.substring(0,800));
                  try{
                    const d = JSON.parse(jsonStr);
                    let creations = d.event_data?.message?.creations
                      || d.patch_op?.[0]?.insert?.[0]?.creations
                      || d.event_data?.creations
                      || d.creations
                      || [];
                    if(!Array.isArray(creations)) creations=[creations];
                    for(const c of creations){
                      if(!c) continue;
                      const img = c.image || c.image_url || {};
                      // 尝试所有可能的字段名
                      const candidates = [img.image_ori_raw, img.image_ori, img.url, img.image_url, img.src];
                      for(const ori of candidates){
                        if(!ori) continue;
                        const u = typeof ori==='string' ? ori : (ori?.url||ori?.image_url||'');
                        if(u && u.startsWith('http') && !window.__doubao_ori_images.includes(u)){
                          window.__doubao_ori_images.push(u);
                          window.__doubao_debug.ori_found++;
                        }
                      }
                    }
                  }catch(e){}
                }
                ctrl.enqueue(value);
              }
              ctrl.close();
            }
          });
          return new Response(stream, {headers:resp.headers, status:resp.status, statusText:resp.statusText});
        }
        return resp;
      };

      // ── hook XMLHttpRequest ──
      const origOpen = XMLHttpRequest.prototype.open;
      const origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function(method, url, ...args){
        this.__url = url;
        return origOpen.apply(this, [method, url, ...args]);
      };
      XMLHttpRequest.prototype.send = function(...args){
        if(this.__url && (this.__url.includes('/chat/completion')||this.__url.includes('/im/'))){
          window.__doubao_debug.xhr_calls++;
          this.addEventListener('load', function(){
            if(this.__url && this.__url.includes('/im/chain')){
              try{
                const data = JSON.parse(this.responseText);
                const msgs = data?.downlink_body?.pull_singe_chain_downlink_body?.messages;
                if(Array.isArray(msgs)){
                  for(const msg of msgs){
                    const creations = msg?.content?.creations || msg?.creations || [];
                    for(const c of creations){
                      const img = c?.image||{};
                      const ori = img.image_ori_raw || img.image_ori;
                      if(ori){
                        const u = typeof ori==='string'?ori:(ori?.url||'');
                        if(u && !window.__doubao_ori_images.includes(u)){
                          window.__doubao_ori_images.push(u);
                          window.__doubao_debug.ori_found++;
                        }
                      }
                    }
                  }
                }
              }catch(e){}
            }
          });
        }
        return origSend.apply(this, args);
      };

      return 'hooked';
    })()
    """)
    log(f"  · fetch/XHR 拦截已注入")


def generate_one(page: CDPPage, prompt: str, outdir: str, name: str, tag: str,
                 ref_image_paths: list = None, expect_portrait: bool = None) -> tuple:
    """
    生成单张图（图生图模式）。返回 (状态, 路径)。状态∈ {ok, failed, quota}。
    tag: '六视图' | '道具' | '尾帧' | '首帧' 等任意标签（用于输出文件命名与诊断快照）。

    ref_image_paths: 图生图模式下的参考图路径列表（角色图+道具图）。
      非空时，先通过 set_file_input 上传参考图，再发送 prompt。
      用于角色参考图、场景关键帧（尾帧/首帧）等一切"以图参考出图"的场景。

    本函数与画风/用途解耦：prompt 由调用方构建（风格相关逻辑留在各 skill）。
    """
    log(f"\n[{name} · {tag}]")

    try:
        if quota_exhausted(page):
            log("  ! 页面已显示免费额度耗尽，跳过")
            return "quota", None
    except CDPError:
        pass

    # 注入 fetch 拦截，捕获无水印原图 URL
    try:
        inject_fetch_hook(page)
    except CDPError:
        pass

    baseline_srcs = _collect_img_srcs(page)

    # ── 图生图：上传参考图（角色图+道具图）──
    if ref_image_paths:
        valid_refs = [p for p in ref_image_paths if p and os.path.exists(p)]
        if valid_refs:
            log(f"  · 图生图模式，上传参考图 {len(valid_refs)} 张: "
                f"{', '.join(os.path.basename(p) for p in valid_refs)}")
            try:
                page.set_file_input(valid_refs)
                time.sleep(3)  # 等待图片预览加载
            except CDPError as e:
                log(f"  ! 上传参考图失败: {e}，改用纯文本模式")

    if not fill_prompt(page, prompt):
        diag_snapshot(page, outdir, f"{name}_{tag}_fill_fail")
        return "failed", None

    time.sleep(0.8)

    if not submit_prompt(page):
        log("  ! 提交失败（可能额度用尽或需要人工确认）")
        diag_snapshot(page, outdir, f"{name}_{tag}_submit_fail")
        return "failed", None

    log("  · 已提交，等待出图…")
    img, error_tag = wait_image(page, baseline_srcs)

    if error_tag == "quota":
        return "quota", None

    if not img:
        log(f"  ! 未出图（报错或超时 {RENDER_TIMEOUT}s）")
        diag_snapshot(page, outdir, f"{name}_{tag}_timeout")
        return "failed", None

    log("  · 图片已生成，开始下载…")
    path = download_image(page, outdir, name, tag, img_info=img, expect_portrait=expect_portrait)
    if path == "DOWNLOAD_TRIGGERED":
        return "ok", path
    if path:
        size = os.path.getsize(path) / 1024
        log(f"  · 已下载: {path} ({size:.0f} KB)")
        return "ok", path
    log("  ! 未取到图片文件")
    try:
        dbg = page.eval_js("JSON.stringify(window.__doubao_debug||{})")
        if dbg and dbg != "{}":
            log(f"  · 拦截调试: {dbg}")
    except CDPError:
        pass
    diag_snapshot(page, outdir, f"{name}_{tag}_download_fail")
    return "failed", None
