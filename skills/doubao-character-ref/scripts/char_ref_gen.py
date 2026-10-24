#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆包角色参考图生成器 —— 直连 CDP 驱动本地 Edge/Chrome，逐角色生成六视图 + 道具图。

共享内核：CDP 客户端、浏览器管理、页面操作原语在 shared/ 模块；
图生图引擎（渲染检测/无水印下载/参考图上传/单张生成）已下沉到 shared/imagegen.py，
本文件只保留风格相关逻辑：六视图/道具提示词构建 + CLI 编排。

关键帧场景图（尾帧/首帧）等一切图生图需求请直接用 shared.imagegen.generate_one，
不要再 import 本文件（保留的 re-export 仅为兼容旧调用方）。

与 doubao-manju-video 的 doubao_gen.py 共用浏览器配置和登录态。

用途：为主要人物生成"六视图角色设定图"（正/背/左/右/3/4前/3/4后）与"道具特写图"，
       保证漫剧跨镜/跨章人物一致性。

用法：
  python char_ref_gen.py characters.json -o ./char_refs              # 全流程生成
  python char_ref_gen.py characters.json -o ./char_refs --dry-run    # 只打印提示词
  python char_ref_gen.py characters.json -o ./char_refs --only 1,2   # 只生成指定角色
  python char_ref_gen.py characters.json -o ./char_refs --only-props # 只跑道具部分
  python char_ref_gen.py --probe                                      # 只测浏览器连通与登录态
"""

import argparse
import glob
import json
import os
import re
import sys

# ── 加载共享内核 ──
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared import cdp_browser, doubao_page, style_presets, imagegen
from shared.cdp_browser import CDPPage, CDPError, DOUBAO_HOME, log
from shared.doubao_page import page_ready, is_logged_in, wait_login, diag_snapshot
from shared.project_manager import ProjectManager, resolve_output_dir

# ── 图生图引擎重导出（兼容旧调用方：probe 脚本 / doubao_gen.py 历史引用）──
# 新代码请直接 from shared import imagegen
from shared.imagegen import (  # noqa: F401
    RENDER_TIMEOUT, POLL_INTERVAL, REPLY_STATE_JS, QUOTA_RE,
    read_reply_state, quota_exhausted, _collect_img_srcs, wait_image,
    download_image, inject_fetch_hook, generate_one,
)


# ──────────────────────────────────────────────────────────────
# 提示词构建
# ──────────────────────────────────────────────────────────────

def build_sixview_prompt(name: str, desc: str, style_key: str,
                         char_data: dict = None) -> str:
    """构建六视图角色设定图提示词。

    升级版：优先按结构化 persona 生成（age/gender/body/face/hair/clothing/props/distinguishing），
    无 persona 时退化为旧版（name+desc）。
    """
    style = style_presets.get_style(style_key)

    # 路径1：结构化 persona（推荐）
    if char_data and char_data.get("persona"):
        p = char_data["persona"]
        parts = [
            f"角色设定图，六视图并排（character turnaround sheet）：",
            f"角色名：{name}",
        ]
        if p.get("age") or p.get("gender"):
            age_gender = []
            if p.get("age"):
                age_gender.append(p["age"])
            if p.get("gender"):
                age_gender.append(p["gender"])
            parts.append("年龄/性别：" + "/".join(age_gender))
        if p.get("body"):
            parts.append(f"体型：{p['body']}")
        if p.get("face"):
            parts.append(f"面容：{p['face']}")
        if p.get("hair"):
            parts.append(f"发型：{p['hair']}")
        if p.get("clothing"):
            parts.append(f"服饰：{p['clothing']}")
        if p.get("props"):
            parts.append(f"随身道具：{', '.join(p['props'])}")
        if p.get("distinguishing"):
            parts.append(f"识别特征：{p['distinguishing']}")
        parts.append("六个角度：正面全身/背面全身/左侧面/右侧面/3/4前视/3/4后视")
        parts.append("六个角度必须是同一个人物，五官/发型/服装/体型/配饰完全一致")
        parts.append("白底参考图，角色居中站立，自然垂手站姿")
        parts.append("高质量设定图，六视图严格对齐，人物形象稳定不变形，细节清晰")
        body = "\n".join(parts)
        return f"{style}。\n{body}"

    # 路径2：旧版兼容（desc-only）
    return (
        f"{style}。\n"
        f"角色设定图，六视图并排（character turnaround sheet）：\n"
        f"角色名：{name}\n"
        f"外形：{desc}\n"
        f"六个角度：正面全身、背面全身、左侧面全身、右侧面全身、3/4前视全身、3/4后视全身\n"
        f"六个角度必须是同一个人物，五官、发型、服装、体型、配饰完全一致\n"
        f"白底参考图风格，角色居中站立，自然垂手站姿\n"
        f"高质量角色设定图，六个角度严格对齐，人物形象稳定不变形，细节清晰"
    )


def build_prop_prompt(name: str, desc: str, style_key: str,
                     prop_data: dict = None) -> str:
    """构建道具特写参考图提示词。

    升级版：兼容旧版（name+desc）和新版（含 aliases 的 prop_data）。
    """
    style = style_presets.get_style(style_key)
    aliases_note = ""
    if prop_data and prop_data.get("aliases"):
        aliases_note = f"（别名：{', '.join(prop_data['aliases'])}）"
    return (
        f"{style}。\n"
        f"道具特写参考图（prop reference sheet）：\n"
        f"道具名：{name}{aliases_note}\n"
        f"描述：{desc}\n"
        f"白底，居中展示，多角度并排（正面、侧面、俯视、手持比例参照），细节清晰\n"
        f"高质量道具参考图，材质真实，细节清晰"
    )


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────

def main():
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="豆包角色参考图生成器（六视图 + 道具，直连 CDP）")
    ap.add_argument("chars_json", nargs="?", default=None,
                    help="角色定义 JSON 文件（含 characters 和 props）")
    ap.add_argument("-o", "--out", default="./char_refs", help="输出目录（图片/索引/诊断）")
    ap.add_argument("-p", "--project-root", default=None,
                    help="项目根目录（启用统一项目管理，产物归档到 characters/ 子目录）")
    ap.add_argument("--probe", action="store_true", help="只测浏览器连通与豆包登录态")
    ap.add_argument("--browser", default="edge",
                    choices=["edge", "chrome", "brave", "vivaldi", "opera", "chromium"],
                    help="可执行浏览器类型（默认 edge，仅支持 Chromium 内核）")
    ap.add_argument("--port", type=int, default=9222, help="CDP 调试端口（默认 9222）")
    ap.add_argument("--profile-dir", default=None,
                    help="专用浏览器配置根目录（默认 %%LOCALAPPDATA%%/doubao-manju）")
    ap.add_argument("--login-wait", type=int, default=600,
                    help="检测到登录墙时等待用户登录的秒数（默认 600）")
    ap.add_argument("--style", default=None,
                    help="画风预设（默认从 JSON 的 style 字段读取，否则 3d-realistic）")
    ap.add_argument("--only", default=None,
                    help="只生成指定角色编号，逗号分隔，如 1,2；"
                         "默认会跳过道具以避免误覆盖已有图（如霜颤声/苏见川已生成、"
                         "剑_道具已手动重生等场景），需同时跑所有道具时加 --also-props")
    ap.add_argument("--also-props", action="store_true",
                    help="与 --only 连用时也跑所有道具（默认 --only 只跑指定角色）")
    ap.add_argument("--only-props", action="store_true",
                    help="只跑道具部分，跳过角色六视图")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的提示词")
    ap.add_argument("--keep-tab", action="store_true", help="结束后保留浏览器标签页")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="单张失败后继续下一张")
    args = ap.parse_args()

    # 解析输出目录和项目管理器
    out_dir, pm = resolve_output_dir(args.project_root, args.out)
    if pm:
        out_dir = pm.get_path("characters")
        os.makedirs(out_dir, exist_ok=True)

    if args.profile_dir is None:
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/.local/share"))
        args.profile_dir = os.path.join(base, "doubao-manju")

    # ── probe 模式 ──
    if args.probe:
        doubao_page.probe_connectivity(
            args.port, args.browser, args.profile_dir, out_dir,
            keep_tab=args.keep_tab, login_wait=args.login_wait
        )
        return

    # ── 定位角色定义文件 ──
    # 优先从 project_root/storyboard/ 读取 characters.json
    chars_json = args.chars_json
    if not chars_json and pm:
        storyboard_dir = pm.get_path("storyboard")
        cands = glob.glob(os.path.join(storyboard_dir, "*.characters.json"))
        if cands:
            chars_json = max(cands, key=os.path.getmtime)
            log(f"· 从项目读取角色定义: {chars_json}")
    if not chars_json:
        cands = glob.glob(os.path.join(out_dir, "**", "*.json"), recursive=True)
        cands += glob.glob("./**/char*.json", recursive=True)
        if not cands:
            log("✗ 未找到角色定义 JSON。请先编写，或显式传入路径。")
            sys.exit(1)
        chars_json = max(cands, key=os.path.getmtime)
        log(f"· 自动选用角色定义文件: {chars_json}")

    with open(chars_json, encoding="utf-8") as f:
        data = json.load(f)

    project = data.get("project", "未命名项目")
    style_key = args.style or data.get("style", "3d-realistic")
    characters = data.get("characters", [])
    props = data.get("props", [])

    if args.only:
        want = {int(x) for x in re.split(r"[,\s]+", args.only) if x.strip()}
        characters = [c for i, c in enumerate(characters, 1) if i in want]
        # --only 用于聚焦指定角色；为避免连带重跑道具覆盖已生成/已手动重生的图，
        # 默认跳过道具。需同时跑所有道具时显式加 --also-props。
        # --only-props 优先级最高（用户明确说"只要道具"），不被本逻辑清掉。
        if not args.also_props and not args.only_props:
            props = []

    if args.only_props:
        characters = []

    total_tasks = len(characters) + len(props)
    total_credits = total_tasks

    log("=" * 62)
    log(f"《{project}》角色参考图生成")
    log(f"画风: {style_key} | 角色: {len(characters)} | 道具: {len(props)}")
    log(f"预计额度: {total_credits} 次（每张图 1 次）")
    log("=" * 62)

    if args.dry_run:
        for c in characters:
            prompt = build_sixview_prompt(c["name"], c["desc"], style_key, char_data=c)
            log(f"\n[{c['name']} · 六视图]")
            log(f"  {prompt[:120]}…")
        for p in props:
            prompt = build_prop_prompt(p["name"], p["desc"], style_key, prop_data=p)
            log(f"\n[{p['name']} · 道具]")
            log(f"  {prompt[:120]}…")
        log("\n(dry-run) 未实际生成。去掉 --dry-run 即开始。")
        return

    if total_tasks == 0:
        log("没有要生成的角色或道具。")
        return

    # ── 打开浏览器 + 登录 ──
    cdp_browser.ensure_browser(args.port, args.browser, args.profile_dir)
    log("\n正在打开豆包…")
    # 每个任务用独立的新标签页。
    # 2026-10-24 实测：复用已有标签页时，豆包可能把新提示词当成对旧会话的
    # 追问（只回文字建议不出图），或把结果渲染到别的标签页导致本页等不到。
    # 新开标签页 + 空白会话是最稳的组合。
    tid = cdp_browser.new_tab(args.port, DOUBAO_HOME)
    created_tid = tid
    page = CDPPage(args.port, tid)
    results = []
    failed = []
    quota_hit = False

    try:
        if not page_ready(page, timeout=90):
            log("  ! 页面未就绪。诊断快照已保存。")
            diag_snapshot(page, out_dir, "page-not-ready")
            sys.exit(3)

        if not is_logged_in(page):
            if not wait_login(page, args.login_wait, out_dir, "login-wall"):
                sys.exit(3)

        task_idx = 0

        def fresh_session():
            """每个任务前新建独立会话。

            豆包在「已有对话」里收到新提示词时，可能把它当成对话追问——
            只回文字建议而不出新图（2026-10-24 实测：三次出图任务全部命中，
            表现为持续等待直至超时）。因此每个任务必须从空白会话发起。
            """
            if not doubao_page.new_conversation(page, timeout=60):
                log("  ! 新建会话失败，沿用当前页面继续提交")

        # ── 角色六视图 ──
        for c in characters:
            task_idx += 1
            log(f"\n{'─'*62}")
            log(f"进度 {task_idx}/{total_tasks} · 角色")
            prompt = build_sixview_prompt(c["name"], c["desc"], style_key, char_data=c)
            fresh_session()
            status, path = generate_one(page, prompt, out_dir, c["name"], "六视图")
            results.append({"type": "character", "name": c["name"], "desc": c["desc"],
                            "status": status, "path": path})
            if status == "quota":
                quota_hit = True
                log("\n⛔ 今日免费额度已耗尽。后续任务不再尝试，次日刷新后用下方命令续跑。")
                break
            if status == "failed":
                failed.append(f"{c['name']}_六视图")
                if not args.continue_on_error:
                    log("\n已停止。可加 --continue-on-error 跳过失败项继续。")
                    break

        # ── 道具特写 ──
        if not quota_hit and (not failed or args.continue_on_error):
            for p in props:
                task_idx += 1
                log(f"\n{'─'*62}")
                log(f"进度 {task_idx}/{total_tasks} · 道具")
                prompt = build_prop_prompt(p["name"], p["desc"], style_key, prop_data=p)
                fresh_session()
                status, path = generate_one(page, prompt, out_dir, p["name"], "道具")
                results.append({"type": "prop", "name": p["name"], "desc": p["desc"],
                                "status": status, "path": path})
                if status == "quota":
                    quota_hit = True
                    log("\n⛔ 今日免费额度已耗尽。后续任务不再尝试，次日刷新后用下方命令续跑。")
                    break
                if status == "failed":
                    failed.append(f"{p['name']}_道具")
                    if not args.continue_on_error:
                        log("\n已停止。可加 --continue-on-error 跳过失败项继续。")
                        break

        # ── 汇总 ──
        log(f"\n{'='*62}")
        ok = [r for r in results if r["status"] == "ok"]
        ok_names = [r["name"] for r in ok]
        all_names = [r["name"] for r in results]
        pending = [r["name"] for r in results if r["status"] != "ok"]
        pending += [c["name"] for c in characters[len([r for r in results if r["type"]=="character"]):]]
        pending += [p["name"] for p in props if p["name"] not in {r["name"] for r in results if r["type"]=="prop"}]

        log(f"完成 {len(ok)}/{total_tasks} 张参考图")
        if ok:
            log(f"输出目录: {os.path.abspath(out_dir)}")
        if failed:
            log(f"失败项: {', '.join(failed)}")
        if quota_hit:
            log("中止原因: 免费额度耗尽（明天刷新后续跑）")

        index_path = os.path.join(out_dir, "char_refs_index.json")
        # 2026-10-24 修复：索引必须与已有记录合并，不能整体覆盖。
        # 续跑（--only N）时只生成了部分角色，若直接覆盖，旧的成功记录会丢失，
        # 导致 doubao_gen.py 找不到该角色的参考图（"角色「X」无参考图，跳过"），
        # 人物一致性锚定静默失效。
        prev_results = []
        if os.path.exists(index_path):
            try:
                with open(index_path, encoding="utf-8") as f:
                    prev = json.load(f)
                prev_results = prev.get("results", []) or []
            except (OSError, json.JSONDecodeError):
                prev_results = []

        merged = {}
        for r in prev_results + results:
            key = (r.get("type"), r.get("name"))
            # 新结果覆盖旧结果（同名同类型）
            merged[key] = r
        all_results = list(merged.values())
        # 排序：角色在前，道具在后，保持 characters.json 的顺序
        order = [c["name"] for c in characters] + [p["name"] for p in props]
        all_results.sort(key=lambda r: order.index(r["name"]) if r.get("name") in order else 999)

        ok_all = [r for r in all_results if r.get("status") == "ok"]
        index_data = {
            "project": project,
            "style": style_key,
            "total": len(all_results),
            "completed": len(ok_all),
            "results": all_results,
        }
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        log(f"索引文件: {index_path}（共 {len(all_results)} 项，成功 {len(ok_all)} 项）")

        # 更新 project.json
        if pm:
            rel_index = os.path.relpath(index_path, pm.root)
            pm.update_stage("characters", "completed", output=rel_index)

        if pending and (quota_hit or failed):
            log(f"待补项: {', '.join(pending)}")
            log(f"续跑命令: python skills/doubao-character-ref/scripts/char_ref_gen.py "
                f"{chars_json} -o {out_dir} --style {style_key} --continue-on-error")
        log("=" * 62)

    finally:
        # 只关闭本次自己新建的标签页。复用来的标签页属于用户的会话，
        # 关掉会导致下次运行找不到可复用的页面（且丢失对话上下文）。
        if not args.keep_tab and tid and tid == created_tid:
            page.close_tab()
            log("\n已关闭本次创建的标签页。")
        elif tid:
            log("\n已保留复用的豆包标签页（未关闭）。")


if __name__ == "__main__":
    main()
