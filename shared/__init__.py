# -*- coding: utf-8 -*-
"""
共享内核模块 —— 所有豆包漫剧 skill 共用的基础设施。

包含：
  cdp_browser   CDP WebSocket 客户端 + 浏览器发现/拉起/标签页管理
  doubao_page   豆包页面操作原语（就绪/登录/填入/提交/截图/诊断）
  style_presets 画风预设（所有 skill 统一，防止风格漂移）
  imagegen      图生图引擎（渲染检测/五级无水印下载/参考图上传/单张生成 generate_one）
  media_unlock  视频无水印下载（fallback_api 解密链路）
  video_utils   视频文件工具

每个 skill 的脚本只需在头部加：

  import os, sys
  _ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
  if _ROOT not in sys.path:
      sys.path.insert(0, _ROOT)
  from shared import cdp_browser, doubao_page, style_presets

扩展纪律：风格子 skill 只放差异（SKILL.md + 提示词构建 + 风格配置），
流水线/平台能力一律进 shared/，禁止在 skill 间互相 import。
"""
