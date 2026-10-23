---
name: doubao-video-skill
description: 豆包漫剧生成技能集。当用户要求"用豆包做漫剧/小说改编漫剧/AI短剧/生成角色设定图/故事分镜"时，先读本文件确定该进入哪个子 skill。负责路由到风格子技能，并提供共享内核与扩展纪律说明。
agent_created: true
---

# 豆包漫剧技能集（路由入口）

把小说章节或故事描述，转成豆包可批量生成的漫剧分镜视频。本仓库是一个技能集：
`shared/` 是平台能力内核，`skills/` 下每个子目录是一个独立子技能。

## 子技能路由

| 需求 | 进入哪个子技能 | 说明 |
|------|--------------|------|
| 小说章节/故事描述 → 漫剧分镜视频（默认 CG写实3D 风格） | [skills/doubao-manju-video/SKILL.md](skills/doubao-manju-video/SKILL.md) | 分镜拆解、提示词生成、逐镜生成下载全流程 |
| 主要人物六视图设定图 / 道具特写参考图 | [skills/doubao-character-ref/SKILL.md](skills/doubao-character-ref/SKILL.md) | 保证跨镜/跨章人物一致性，视频生成前必须先跑 |

漫剧生成前先跑角色参考图（人物一致性依赖它）；两者共用浏览器配置与登录态。

## 目录结构

```
doubao-video-skill/
├── shared/                      # 共享内核（平台能力，与风格无关）
│   ├── cdp_browser.py           #   CDP 客户端 + 浏览器发现/拉起/标签页
│   ├── doubao_page.py           #   豆包页面操作原语（就绪/登录/填入/提交/诊断）
│   ├── imagegen.py              #   图生图引擎（渲染检测/双档下载（无水印原图优先/带水印兜底，不截图）/generate_one）
│   ├── media_unlock.py          #   视频无水印下载（fallback_api 解密链路）
│   ├── style_presets.py         #   画风预设（所有 skill 统一，防风格漂移）
│   └── video_utils.py           #   视频文件工具
├── skills/
│   ├── doubao-manju-video/      # 漫剧生成（分镜+视频全流程）
│   └── doubao-character-ref/    # 角色参考图（六视图+道具）
└── tests/                       # unit / mock_cdp / e2e 三层测试
```

## 平台硬约束（全技能集通用）

- 豆包免费版单视频最长 **15 秒**，每镜单独计 1 次额度，免费版约 5 次/天
- 浏览器自动化必须用专用配置目录（`%LOCALAPPDATA%\doubao-manju`），首次运行需在该窗口登录一次豆包
- 禁止点击「视频生成」切换专属视频模式；直接在普通对话框发提示词，自动确认参数卡
- 详细规则见 `skills/doubao-manju-video/SKILL.md` 与 `references/` 下的指南

## 新增风格子技能的纪律

后续扩展不同风格漫剧（如 2D 国漫、水墨、Q 版）时，遵守"**薄壳原则**"：

1. **先判断差异类型**：只换画风措辞 → 在 `shared/style_presets.py` 加预设条目即可，不建新 skill；分镜策略/时间线/镜头语言有实质差异 → 才建新子 skill
2. **新子 skill 只放差异**：`SKILL.md` + 提示词构建 + 风格配置/措辞覆盖表；流水线与平台能力一律 import `shared/`
3. **禁止 skill 间互相 import**：需要对方能力时下沉到 `shared/`（如图生图引擎 `shared/imagegen.py`）
4. **bug 修复只修一处**：所有风格共用的逻辑变更只改 shared，不许复制代码副本

新增风格预设的画风差异全部收敛在 `style_presets.py`；若风格需要覆盖打斗镜头语言、构图措辞等，以配置注入方式扩展 `split_novel.py` 的映射表，而非复制脚本。
