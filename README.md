# doubao-video-skill

把小说章节或故事描述，转成豆包可批量生成的漫剧分镜视频。

本仓库是一个技能集：`shared/` 是共享内核，`skills/` 下每个子目录是独立子技能。

它不调用豆包 API，而是通过 CDP 驱动本地 Chromium 浏览器打开**豆包网页版**（doubao.com），像真人一样在对话框输入提示词、上传参考图、等待生成并下载结果。因此不需要申请 API Key，也不需要安装任何浏览器插件，生成脚本零第三方 pip 依赖。

---

## 能力

- **章节拆解** — 按字数密度与叙事节拍拆成 ≤15s 的镜头，自动分集并在集尾留悬念
- **角色参考图** — 生成人物六视图与道具特写，保证跨镜人物一致
- **关键帧生成** — 以角色图、道具图为参考，图生图生成每镜画面
- **视频生成** — 自动填提示词、确认参数、轮询渲染、下载 mp4，逐镜串行

输入支持小说章节（txt/md），也支持故事梗概 JSON（`--from-plan`）。

## 演示

示例项目《烟雨剑归》前两镜（约 27 秒，含跨镜衔接），下图为前 6 秒：

![烟雨剑归演示](tests/e2e_project_mode/烟雨剑归/output/preview.gif)

[观看完整视频](https://github.com/rookiebug1216/doubao-video-skill/releases/download/v1.0.0/demo-full.mp4)（约 87MB，托管在 Releases）

## 环境要求

| 项 | 要求 |
|----|------|
| 系统 | Windows 10/11（macOS/Linux 可跑，需自行确认浏览器路径） |
| Python | 3.10+ |
| 浏览器 | Edge / Chrome / Brave / Vivaldi / Opera / Chromium 任一 |
| 豆包账号 | 豆包网页版（doubao.com），免费版即可 |
| 网络 | 能正常访问 doubao.com |
| 依赖 | 无；跑测试需 `pip install -r requirements-dev.txt` |

豆包免费版单镜最长 15 秒，每镜计 1 次额度，每日约 5 次（额度指豆包账号，与所用 AI 智能体无关）。

## 安装与使用

把下面这句话发给 AI 智能体（如 Trae），它会自动克隆并安装：

> 帮我安装这个 skill：https://github.com/rookiebug1216/doubao-video-skill
> 安装后先读 `SKILL.md`，按其中的路由进入对应子技能执行。
> 开始前先问我项目存放在哪里；我没指定就用技能目录下的 `workspace\<项目名>`。
> 需要登录豆包时停下来让我扫码；消耗豆包额度前，先告诉我这一步会生成多少张图或多少个镜头。
> 只用脚本写入项目目录的文件，不点击豆包页面上的任何下载/保存/导出按钮。
> 不安装豆包桌面版或任何第三方桌面软件。
> 下载结果以脚本日志和 `-p`/`-o` 目录下的文件为准，不要到系统下载目录找。

也可以跳过智能体，手动克隆后直接执行：

```bash
# 1) 拆解章节
python skills/doubao-manju-video/scripts/split_novel.py 章节.txt -p ./projects/烟雨剑归

# 2) 提取角色定义
python skills/doubao-character-ref/scripts/extract_characters.py 章节.txt -o ./projects/烟雨剑归/storyboard

# 3) 生成六视图与道具参考图
python skills/doubao-character-ref/scripts/char_ref_gen.py -p ./projects/烟雨剑归

# 4) 预演视频提示词
python skills/doubao-manju-video/scripts/doubao_gen.py -p ./projects/烟雨剑归 --dry-run

# 5) 实跑
python skills/doubao-manju-video/scripts/doubao_gen.py -p ./projects/烟雨剑归
```

第 2 步产出的角色定义会写入 `storyboard/`，第 3 步自动读取。

首次运行会拉起专用配置的浏览器窗口，请在窗口内登录豆包一次，登录态持久保存。

使用 `-p` 时产物自动归档到项目下的 `storyboard/`、`characters/`、`keyframes/`、`videos/`，进度记录在 `project.json`。不传 `-p` 时退回 `-o` 平铺模式（默认目录见各脚本 `-h`），脚本会自动查找 `*.shots.json` 与参考图索引。

常用续跑命令：

```bash
python skills/doubao-manju-video/scripts/doubao_gen.py -p ./projects/烟雨剑归 --only 3 --continue-on-error
python skills/doubao-manju-video/scripts/doubao_gen.py -p ./projects/烟雨剑归 --keyframes-only   # 只生成关键帧
python skills/doubao-manju-video/scripts/doubao_gen.py -p ./projects/烟雨剑归 --skip-keyframes   # 用已有帧直接出视频
```

## 工作流

1. **拆解** — 按约 4.6 字/秒估算容量，结合节拍（打斗/动作/转折/情绪/对白/氛围）分配时长；打斗镜头加密到 2–4 秒/拍并注入动作镜头语言。默认每 5 镜一集，非完结章每集停在高潮未解决处。
2. **角色图** — 六视图设定图 + 道具特写，后续所有画面以它们为参考。
3. **关键帧** — 首镜生成首帧与尾帧，其余镜头只生成尾帧。
4. **视频** — 逐镜提交；从第二镜起把上一镜尾帧图作为本镜首帧参考上传，保证衔接；先确认参考图全部上传成功再提交，读取并自动确认豆包的「视频生成参数确认」卡后开始渲染（豆包若再次要求确认，会继续自动确认，最多 3 轮）。

拆解产出四个文件：`*.storyboard.md`（人读分镜表）、`*.prompts.txt`（逐镜提示词）、`*.voiceover.md`（口播稿 + SRT）、`*.shots.json`（供视频脚本消费）。

## 常用参数

三个脚本都支持 `-h` 查看完整参数。`split_novel.py` 为离线脚本，只涉及 `-p`/`-o`；另外两个还要操作浏览器，公共参数为 `--browser`（默认 `edge`）、`--port`（默认 9222）、`--profile-dir`、`--login-wait`、`--probe`、`--dry-run`、`--keep-tab`、`--continue-on-error`。

**split_novel.py**

| 参数 | 说明 |
|------|------|
| `--style` | 画风，默认 `3d-realistic` |
| `--ratio` | 画幅，默认 `16:9` |
| `--episode-shots` | 每集镜头数，默认 5；`0` = 不分集 |
| `--is-final` | 完结章，末集正常收尾 |
| `--from-plan` | 从故事梗概 JSON 拆解 |
| `--characters-json` | 角色定义，用于标注每镜出场角色 |

**char_ref_gen.py**

| 参数 | 说明 |
|------|------|
| `--style` | 画风，默认读 JSON 的 style 字段 |
| `--only` | 只生成指定角色编号，如 `1,2` |
| `--also-props` | 与 `--only` 连用时也生成道具 |
| `--only-props` | 只生成道具 |
| `--probe` | 只检查浏览器与登录态 |

**doubao_gen.py**

| 参数 | 说明              |
|------|---------------|
| `--probe` | 只检查浏览器与登录态      |
| `--only` / `--start` / `--end` | 指定镜号或区间         |
| `--keyframes-only` | 只生成关键帧，不出视频     |
| `--skip-keyframes` | 跳过关键帧，直接出视频     |
| `--allow-paid` | 豆包提示免费额度用完时，自动点「确认生成」消耗付费额度继续；默认跳过本镜并记录待补 |
| `--no-unlock` | 禁用视频无水印下载（默认开启）；图片始终优先无水印原图，无开关 |

## 画风与画幅

| style | 风格 |
|-------|------|
| `3d-realistic` | CG 写实 3D，UE5 渲染，PBR 材质（默认） |
| `3d-cartoon` | 3D 卡通 |
| `guoman-2d` | 二维国漫 |
| `anime` | 日式动漫 |
| `cinematic` | 真人电影质感 |
| `ink-wash` | 水墨国风 |

画幅可选 `16:9`（默认）、`9:16`、`1:1`、`4:3`、`21:9`。画风与画幅写在提示词层，修改后需重新拆解。

## 浏览器

仅支持 Chromium 内核。默认 `edge`，指定浏览器找不到时按 `chrome → edge → brave → vivaldi → chromium` 降级。

每种浏览器使用独立配置目录 `%LOCALAPPDATA%\doubao-manju\<kind>-profile`，与日常浏览器互不干扰。若 9222 端口已被其他实例占用，脚本会复用该实例，必要时用 `--port 9223` 切换。

## 无水印

只有两档下载方式：无水印原图/原片、带水印页面资源。**不使用截图，也不点击页面上的「下载/保存/导出」按钮。**

- **图片（角色图 / 道具图 / 关键帧图）**：始终优先下载无水印原图，没有开关。按本镜文件 ID 从 `_ROUTER_DATA` 提取 `image_ori` 签名直链（首轮未命中会重导航会话页重试），以及生成流 fetch/XHR 钩子捕获的原图直链；全部失败才降级为带水印页面资源。两档均做分辨率守卫（最长边 ≥1280），自动拦截缩略图。
- **视频**：默认启用无水印链路——拦截生成流 fallback_api 并解密，取最高码率版本（`shared/media_unlock.py`）；`doubao_gen.py` 加 `--no-unlock` 可退回带水印页面资源（https 直链 / blob fetch）。无水印失败自动降级为带水印，不中断流程。

所有文件由脚本直接写入 `-p` 项目目录（`characters/`、`keyframes/`、`videos/`）或 `-o` 目录，不经过浏览器默认下载目录。

该功能操作的是你自己的浏览器与账号，请自行评估平台条款与合规风险。

## 常见问题

| 现象 | 处理 |
|------|------|
| 提示免费额度用完 | 默认跳过本镜并记录待补，次日用 `--only N` 续跑；加 `--allow-paid` 则自动确认消耗付费额度继续 |
| 渲染超时 | 加 `--keep-tab` 保留标签页人工查看 |
| 豆包改版导致定位失败 | 脚本会保存 DOM 快照，对照 `references/doubao-selector-guide.md` 校准 |
| 视频带水印 | 默认已启用无水印链路；若仍是带水印，说明无水印链路失败已自动降级，可查看日志；加 `--no-unlock` 可显式只要带水印版 |
| 下载到了豆包的 PC 安装包 / `doubao*.exe` | 脚本从不点击页面按钮，所有产物由脚本直接写盘。出现安装包说明手动点了页面或浏览器下载入口，请删除，以 `-p`/`-o` 目录内文件为准 |
| 找不到生成的图片/视频 | 到 `-p` 项目目录下的 `characters/`、`keyframes/`、`videos/` 或 `-o` 目录找，不要到系统「下载」目录找 |
| 画面在镜头间跳变 | 检查分镜场景字段，方位词不应被当作新场景 |
| 单镜超过 10 秒失败 | 不要点击对话框底部「视频生成」模式，直接在普通对话框发送提示词 |

## 扩展风格

遵守薄壳原则：

1. 只换画风措辞 → 在 `shared/style_presets.py` 增加预设，不新建子技能
2. 分镜策略或镜头语言有实质差异 → 新建子技能，只放 `SKILL.md`、提示词模板与配置覆盖表
3. 流水线与平台能力一律复用 `shared/`，技能之间禁止互相 import

## 测试

```bash
pip install -r requirements-dev.txt

python -m pytest tests/unit/ -q        # 纯逻辑
python -m pytest tests/mock_cdp/ -q    # CDP mock 回归
python -m pytest tests/e2e/ -m e2e -q  # 端到端，需已登录豆包，会真实消耗豆包额度（不加 -m e2e 会全部跳过）
```

## 免责声明

使用本项目前请确认你已理解并接受以下内容：

- **账号风险**：本项目通过 CDP 操作用户本机浏览器和用户自己的豆包账号，不提供、不托管任何账号。此类自动化可能违反豆包（字节跳动）的用户协议，可能导致限流、功能限制或账号封禁，风险及后果由使用者承担。
- **内容责任**：输入文本的改编权、生成内容的版权与合规性由使用者负责。不得用本项目生成或传播违法、侵权、暴力、色情等内容。
- **无水印功能**：默认开启（图片与视频都优先无水印）；`--no-unlock` 仅退回视频带水印。启用后可能涉及平台条款与版权问题，由使用者自行评估并承担全部责任。
- **无担保**：本项目按 AGPLv3 以“现状”提供，不附带任何明示或暗示的担保，作者不对使用造成的任何直接或间接损失负责。
- **不认可即勿用**：如不同意上述条款，请停止使用本项目。

## 许可证

[GNU Affero General Public License v3.0](LICENSE)
