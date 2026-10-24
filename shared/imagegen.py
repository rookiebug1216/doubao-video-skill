# -*- coding: utf-8 -*-
"""
豆包图生图引擎 —— 所有 skill 共用的"提示词 → 豆包出图 → 无水印下载"全链路能力。

从 doubao-character-ref/scripts/char_ref_gen.py 下沉而来（风格无关部分）：
  * 回复状态检测与额度识别（read_reply_state / quota_exhausted）
  * 图片渲染状态机等待（wait_image）
  * 双档图片下载：无水印原图优先，带水印页面资源兜底；不做截图、不点击页面按钮
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
from .doubao_page import fill_prompt, submit_prompt, diag_snapshot, upload_images

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
    """收集页面所有 <img> 的 src（用于基线对比）。过滤掉 UI 图标、头像和小图片。

    2026-10-24 实测：豆包新版的生成图 URL 形如
      https://p6-flow-imagex-sign.byteimg.com/tos-cn-i-a9rns2rl98/rc_gen_image/<id>.jpeg~tplv-...
    且页面同时存在 384x216 的缩略图与 2848x1600 的原图两个 <img>。
    原来的「宽或高 < 200 即过滤」会把 384x216 的缩略图整体滤掉，
    导致基线对比拿不到任何新图（wait_image 永远超时）。
    故阈值下调到 120，并排除 data: 内联占位符。
    """
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
            // 过滤小尺寸图片（阈值 120：豆包缩略图 384x216 需保留）
            const w=img.naturalWidth||img.width||0;
            const h=img.naturalHeight||img.height||0;
            if(w>0 && w<120) return false;
            if(h>0 && h<120) return false;
            return true;
          })
          .map(img=>(img.currentSrc||img.src||'')))
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
                    # 2026-10-24 实测：新会话页会同时加载豆包的静态装饰图
                    # （lf-flow-web-cdn.doubao.com/.../static/image/intro...），
                    # 它同样是"新出现"的，会被误判成生成图（实测下到了欢迎页插画）。
                    # 生成图的 URL 必含 rc_gen_image —— 据此过滤。
                    gen_fresh = [s for s in fresh if "rc_gen_image" in s]
                    if not gen_fresh:
                        gen_fresh = []
                    if gen_fresh:
                        # 检查图片是否已完全加载
                        loaded_js = f"""
                        (()=>{{
                          const srcs={json.dumps(list(gen_fresh))};
                          for(const s of srcs){{
                            const img=[...document.querySelectorAll('img')]
                              .find(i=>{{const si=(i.currentSrc||i.src||''); return si===s||si.startsWith(s)||s.startsWith(si);}});
                            if(img && img.complete && img.naturalWidth > 200){{
                              return JSON.stringify({{src: (img.currentSrc||img.src||''),
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


def _router_ori_urls(page: CDPPage, file_id: str = "") -> list:
    """从页面首屏 window._ROUTER_DATA 提取当前生成图的无水印原图签名 URL 列表。

    2026-10-24 实测（豆包服务端变更后）：
      * 旧数据源 image_raw 已恒为 null，保留提取仅作兼容
      * 活源是 creations[].image.image_ori 字段的 url ——
        ~tplv-a9rns2rl98-image.png 变体即无水印原图（2720x1520 级），
        与 cgen_lwm3（带水印）/ cthumb_wm0（缩略图）共用同一 32-hex 文件 ID
      * _ROUTER_DATA 中混有 UI 图标（image-qvalue 320x320）、用户头像等，
        必须按文件 ID 精确匹配 + 排除非生成图路径，否则会下载到图标

    file_id: 优先传本镜 wait_image 返回 src 中的 rc_gen_image/<id>；
             为空时回落到页面当前最大生成图的 ID（多图会话可能串图）。
    刚生成、尚未写入首屏数据的图返回空（调用方重导航后重试）。
    """
    try:
        if not file_id:
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
            file_id = m.group(1)
        rd = page.eval_js("JSON.stringify(window._ROUTER_DATA||null)")
        if not rd or rd == "null":
            return []
        clean = rd.replace("\\u002F", "/").replace("\\/", "/")
        urls = []
        # image_ori: {"url": "..."} 或直接字符串（无水印原图，当前活源）
        urls += re.findall(r'"image_ori"\s*:\s*\{[^{}]*?"url"\s*:\s*"([^"]+)"', clean)
        urls += re.findall(r'"image_ori"\s*:\s*"([^"]+)"', clean)
        urls += re.findall(r'"image_ori_raw"\s*:\s*"([^"]+)"', clean)
        # image_raw: 旧数据源（服务端变更后恒 null，保留兼容）
        urls += re.findall(r"https?://[^\"'\s\\<>]+image_raw[^\"'\s\\<>]*", clean)
        return [u for u in urls
                if file_id in u
                and not re.search(r"qvalue|user-avatar|BIZ_BOT_ICON|ocean-cloud-tos|byteacctimg", u)]
    except (CDPError, json.JSONDecodeError):
        return []


def _reload_session(page: CDPPage, wait_s: int = 20) -> bool:
    """重导航当前会话页，让服务端首屏 _ROUTER_DATA 收录刚生成的图。

    2026-10-24 实测：刚生成的图不在当前 _ROUTER_DATA（首屏数据在页面加载时
    由服务端注入），需重导航一次让新图进入首屏数据。
    注意：
      * SPA 会话路由在 Page.reload 后会退回 /chat，故用 Page.navigate 到原 URL
      * 导航会重置页面 JS 状态（fetch 钩子、捕获缓存），调用方须先取走捕获结果
    """
    try:
        url = page.eval_js("location.href")
        if not url or not str(url).startswith("http"):
            return False
        page.call("Page.navigate", recv_timeout=60, url=url)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(1)
            try:
                if page.eval_js("JSON.stringify(window._ROUTER_DATA!=null)") == "true":
                    return True
            except CDPError:
                pass
        return False
    except Exception:
        return False


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
    if len(data) < 20 * 1024:
        log(f"  · 直链下载文件太小({len(data) // 1024}KB)")
        return False
    if data[:8] != b"\x89PNG\r\n\x1a\n" and data[:3] != b"\xff\xd8\xff":
        log(f"  · 直链响应非图片(head={data[:8]!r})")
        return False
    with open(filepath, "wb") as f:
        f.write(data)
    return True


def _img_dimensions(filepath: str):
    """读取 PNG/JPEG 宽高，返回 (w, h)。无法解析返回 None。"""
    try:
        with open(filepath, "rb") as f:
            head = f.read(32)
        # PNG: 宽/高在 IHDR 第 16-24 字节（大端）
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            import struct
            with open(filepath, "rb") as f:
                f.seek(16)
                w, h = struct.unpack(">II", f.read(8))
            return w, h
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
                        return w, h
                    if marker in (b"\xd8", b"\xd9") or b"\xd0" <= marker <= b"\xd7":
                        continue
                    seg_len = struct.unpack(">H", f.read(2))[0]
                    f.seek(seg_len - 2, 1)
    except Exception:
        return None
    return None


def _img_is_portrait(filepath: str) -> bool | None:
    """判断图片是否竖版。无法解析返回 None。"""
    dims = _img_dimensions(filepath)
    return None if dims is None else dims[1] > dims[0]


def _ratio_mismatch(filepath: str, expect_portrait: bool | None) -> bool:
    """下载图方向与预期不符（抓错图的信号，如把横版六视图当竖版尾帧图）。"""
    if expect_portrait is None:
        return False
    is_portrait = _img_is_portrait(filepath)
    if is_portrait is None:
        return False  # 无法判断时不拦截
    return is_portrait != expect_portrait


def _is_image_file(filepath: str) -> bool:
    """校验落盘文件确为图片：PNG/JPEG 文件头 + 体积阈值（>20KB）。

    注意：该校验不拦缩略图（384x216 缩略图约 105KB 也能通过）。
    下载链路统一用 _is_image_file_hires（含分辨率守卫），本函数仅供诊断。
    """
    try:
        if not os.path.isfile(filepath) or os.path.getsize(filepath) <= 20 * 1024:
            return False
        with open(filepath, "rb") as f:
            head = f.read(8)
        return head == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff"
    except OSError:
        return False


def _is_image_file_hires(filepath: str, min_side: int = 1280) -> bool:
    """校验落盘文件为 PNG/JPEG 且分辨率达标（最长边 >= min_side）。

    2026-10-24 缩略图事故（断刃黄昏 shot_04/05 尾帧实锤）：384x216 缩略图
    （105KB）通过了旧版 20KB 体积校验落盘，并被误报为"无水印原图下载成功"，
    导致后续视频参考图模糊。豆包生成原图最长边 >=1520（2720x1520 /
    1520x2720 / 2848x1600），缩略图最长边 384，取 1280 可精确区分两者。
    """
    try:
        if not os.path.isfile(filepath) or os.path.getsize(filepath) <= 20 * 1024:
            return False
        dims = _img_dimensions(filepath)
        if dims is None:
            return False
        return max(dims) >= min_side
    except OSError:
        return False


def _discard_file(filepath: str):
    """校验不通过时删除残留文件，避免污染输出目录。"""
    try:
        os.remove(filepath)
    except OSError:
        pass


def _candidate_url(item) -> str:
    """兼容字符串 / {url:...} 两种候选条目，取出 URL。"""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("url", "src", "image_url"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _largest_gen_image_src(page: CDPPage) -> str:
    """页面中面积最大的 rc_gen_image 图片 src（带水印页面资源）。

    2026-10-24 实测：豆包同时渲染两个 <img> —— 384x216 的缩略图与
    2848x1600 的原图。必须按 naturalWidth*naturalHeight 选最大者，
    否则只会拿到缩略图。原图的 offsetParent 可能为 null（虚拟列表/懒加载），
    故不用 offsetParent 过滤，只排除 data: 内联占位符。
    """
    try:
        found = page.eval_js(r"""
        (()=>{
          const imgs=[...document.querySelectorAll('img')]
            .filter(img=>{
              const s=(img.currentSrc||img.src||'');
              if(!s || s.startsWith('data:')) return false;
              if(!s.includes('rc_gen_image')) return false;
              // 排除豆包站点自身的静态资源（欢迎页装饰图等）
              if(s.includes('lf-flow-web-cdn.doubao.com')) return false;
              return img.naturalWidth>100;
            });
          if(!imgs.length) return null;
          imgs.sort((a,b)=>(b.naturalWidth*b.naturalHeight)-(a.naturalWidth*a.naturalHeight));
          return (imgs[0].currentSrc||imgs[0].src||'');
        })()
        """)
        return found or ""
    except CDPError:
        return ""


def _forget_captured(page: CDPPage):
    """下载成功后清空拦截缓存，避免后续任务串用旧图 URL。"""
    try:
        page.clear_captured_images()
    except CDPError:
        pass


def download_image(page: CDPPage, outdir: str, name: str, tag: str,
                   img_info: dict | None = None, expect_portrait: bool | None = None) -> str | None:
    """下载图片包装：成功后校验图片方向与预期是否一致（防止抓错参考图）。

    尾帧/首帧图为竖版 9:16（expect_portrait=True），六视图/道具图方向不定
    （expect_portrait=None，不校验）。方向不符时删除文件并返回 None，
    让上层判定本镜关键帧生成失败，避免基于错误参考图继续生成视频。
    """
    result = _download_image_impl(page, outdir, name, tag, img_info)
    if result and os.path.isfile(result) and _ratio_mismatch(result, expect_portrait):
        orient = "竖版" if expect_portrait else "横版"
        log(f"  ✗ 下载图方向与预期({orient})不符，疑似抓错参考图，已删除: {os.path.basename(result)}")
        try:
            os.remove(result)
        except OSError:
            pass
        return None
    return result


def _download_image_impl(page: CDPPage, outdir: str, name: str, tag: str,
                         img_info: dict | None = None) -> str | None:
    """
    下载图片（双档，不做截图、不点击页面按钮）：
      1) 无水印原图（始终优先）：_ROUTER_DATA 的 image_ori 签名直链（按本镜
         文件 ID 匹配，排除 UI 图标）+ fetch 钩子捕获的原图 URL；首轮未命中时
         重导航会话页让首屏数据收录新图后再试一轮。先 Python 直链，失败再页面 fetch
      2) 带水印（兜底）：页面尺寸最大的 rc_gen_image，依次尝试 Python 直链 /
         页面 fetch / download_native / download_raw

    每个方案落盘后用 _is_image_file_hires 校验（PNG/JPEG 头 + 最长边 >=1280，
    拦截 384x216 缩略图），不通过即删除并继续下一候选。
    img_info: wait_image() 返回的 {"src": ...}，用于提取本镜文件 ID 与兜底 src。
    """
    os.makedirs(outdir, exist_ok=True)
    filepath = os.path.join(outdir, f"{name}_{tag}.png")

    own_src = (img_info or {}).get("src") or ""
    fid_m = re.search(r"rc_gen_image/([0-9a-zA-Z]+)", own_src)
    own_fid = fid_m.group(1) if fid_m else ""

    def _try_download(url: str) -> bool:
        """单候选两路下载 + 分辨率守卫，成功时文件已落盘。"""
        if _direct_download_image(page, url, filepath) and _is_image_file_hires(filepath):
            log(f"  · 无水印原图下载成功（直链），{os.path.getsize(filepath) // 1024} KB")
            return True
        _discard_file(filepath)
        try:
            fetched = page.download_via_fetch(url, filepath, timeout=30)
        except CDPError:
            fetched = False
        if fetched and _is_image_file_hires(filepath):
            log(f"  · 无水印原图下载成功（页面 fetch），{os.path.getsize(filepath) // 1024} KB")
            return True
        _discard_file(filepath)
        return False

    # ── 第一档：无水印原图（始终优先）──
    # fetch 钩子捕获必须在重导航前取走（导航会重置页面 JS 状态）
    captured = []
    try:
        ori = page.eval_js("JSON.stringify(window.__doubao_ori_images||[])")
        if isinstance(ori, str) and ori:
            captured.extend(json.loads(ori))
    except (CDPError, json.JSONDecodeError):
        pass
    try:
        captured.extend(page.get_captured_images() or [])
    except CDPError:
        pass

    seen = set()
    candidates = []
    # ① _ROUTER_DATA 的 image_ori 直链（按本镜文件 ID 匹配，无水印签名现成）
    try:
        router_urls = _router_ori_urls(page, own_fid)
    except CDPError as e:
        log(f"  · _ROUTER_DATA 读取异常: {e}")
        router_urls = []
    for url in [_candidate_url(u) for u in router_urls]:
        if url and url not in seen:
            seen.add(url)
            candidates.append(url)
    # ② fetch 钩子捕获的原图 URL（累积列表，最新在后 -> 翻转后最新优先）
    captured.reverse()
    for item in captured:
        url = _candidate_url(item)
        if url and url not in seen:
            seen.add(url)
            candidates.append(url)
    # ③ wait_image 返回的本镜 src（可能是缩略图，靠分辨率守卫拦截后继续）
    if own_src and own_src not in seen:
        candidates.append(own_src)

    for url in candidates:
        if _try_download(url):
            _forget_captured(page)
            return filepath

    # 刚生成的图不在当前首屏数据：重导航会话页让服务端收录，再提取一轮
    if _reload_session(page):
        try:
            router_urls = _router_ori_urls(page, own_fid)
        except CDPError:
            router_urls = []
        for url in [_candidate_url(u) for u in router_urls]:
            if url and url not in seen:
                if _try_download(url):
                    _forget_captured(page)
                    return filepath
        log("  · 无水印原图候选均无效（含重导航重试），转带水印兜底…")
    elif candidates:
        log("  · 无水印原图候选均无效，转带水印兜底…")
    else:
        log("  · 未捕获到无水印原图 URL，转带水印兜底…")

    # ── 第二档：带水印页面资源（兜底）──
    src = own_src
    page_src = _largest_gen_image_src(page)
    # 2026-10-24 实测：wait_image 返回的是 320x320 的缩略图 src，
    # 而页面里同时存在 2848x1600 的原图 <img>。CDN 用同一个 URL 也能返回原图，
    # 所以必须优先用「页面中像素最大的那张」的 URL —— 否则只下到 70KB 缩略图。
    # 原判断 page_src.startswith(src[:80]) 在两图路径不同（rc_gen_image/xxx.jpeg
    # vs xxx.png）时恒为 False，导致永远拿缩略图。
    if page_src:
        src = page_src
    elif src and "rc_gen_image" not in src:
        # 回落到 wait_image 的 src，但它不是生成图（豆包静态装饰图等）—— 不可用
        log("  ! 页面未找到生成图（仅静态资源），下载跳过")
        return None
    if not src:
        log("  ! 页面未找到可用图片 src")
        return None
    log(f"  · 带水印下载: {src[:80]}…")
    # blob: 只能页面内 fetch 取回 base64
    if src.startswith("blob:"):
        try:
            res = page.eval_js(f"""
            (async ()=>{{
              const src={json.dumps(src)};
              try {{
                const b=await (await fetch(src)).blob();
                if(b.size > 100*1024*1024) return null;
                const buf=await b.arrayBuffer();
                const bytes=new Uint8Array(buf);
                let binary=''; const chunk=0x8000;
                for(let i=0;i<bytes.length;i+=chunk){{
                  binary+=String.fromCharCode.apply(null, bytes.subarray(i,i+chunk));
                }}
                return {{b64: btoa(binary), size: b.size}};
              }} catch(e) {{ return null; }}
            }})()
            """, await_promise=True, recv_timeout=120)
            if isinstance(res, dict) and res.get("b64"):
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(res["b64"]))
                if _is_image_file_hires(filepath):
                    log(f"  · 带水印下载成功（blob fetch），{os.path.getsize(filepath) // 1024} KB")
                    return filepath
        except (CDPError, ValueError):
            pass
        _discard_file(filepath)
        return None

    # 1) Python 直链（urllib + Cookie + Referer）
    if src.startswith(("http://", "https://")):
        if _direct_download_image(page, src, filepath) and _is_image_file_hires(filepath):
            log(f"  · 带水印下载成功（直链），{os.path.getsize(filepath) // 1024} KB")
            return filepath
        _discard_file(filepath)

    # 2) 页面内 fetch
    try:
        fetched = page.download_via_fetch(src, filepath, timeout=30)
    except CDPError:
        fetched = False
    if fetched and _is_image_file_hires(filepath):
        log(f"  · 带水印下载成功（页面 fetch），{os.path.getsize(filepath) // 1024} KB")
        return filepath
    _discard_file(filepath)

    # 3) fetch(no-cors) + CDP getResponseBody
    try:
        native = page.download_native(src, filepath, timeout=60)
    except CDPError:
        native = False
    if native and _is_image_file_hires(filepath):
        log(f"  · 带水印下载成功（download_native），{os.path.getsize(filepath) // 1024} KB")
        return filepath
    _discard_file(filepath)

    # 4) new Image + CDP getResponseBody
    try:
        raw = page.download_raw(src, filepath, timeout=60)
    except CDPError:
        raw = False
    if raw and _is_image_file_hires(filepath):
        log(f"  · 带水印下载成功（download_raw），{os.path.getsize(filepath) // 1024} KB")
        return filepath
    _discard_file(filepath)

    log("  ! 带水印下载失败（直链 / fetch / CDP 均未取到有效图片）")
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
            # 上传未确认成功就提交，豆包会退化成纯文生图，人物与参考图脱钩
            if not upload_images(page, valid_refs, log_prefix="参考图"):
                diag_snapshot(page, outdir, f"{name}_{tag}_upload_fail")
                log("  ! 参考图未全部上传成功，已中止本次生成（不生成无参考图）")
                return "failed", None

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
