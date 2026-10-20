---
domain: doubao.com
aliases: [豆包, Doubao, Seedance, 字节AI助手]
updated: 2026-10-14
---

# 豆包网页版漫剧生成 · 站点经验

## 平台特征

- 视频模型：**Seedance 2.0**，网页版支持 4–15 秒精确到 1 秒的时长控制
- **免费版单次生成上限 15 秒**——这是所有拆解工作的前提
- 额度计费：**按生成次数计费，与视频时长无关**，每生成 1 个视频计 1 次；电脑端免费版每日约 5 次
- 输入方式：网页对话框可直接发送视频指令，不强制走「视频生成」功能页
- 登录态：依赖用户浏览器本地 Cookie，CDP 直连天然携带

## 环境前置条件（2026-10-14 第二轮实测，直连 CDP 架构）

`doubao_gen.py` 零依赖直连 CDP：HTTP 端点（`/json/version` `/json/new` `/json/close`）+ 内置 RFC6455 WebSocket 客户端（Runtime.evaluate / Page.captureScreenshot / Network.getAllCookies）。**不依赖任何 proxy 或外部 skill。**

### 为什么必须用专用 --user-data-dir（核心结论）

**Chromium 136+（本机 Edge 155 实测）禁止在「默认用户配置目录」上启用 `--remote-debugging-port`**——启动参数会被静默忽略，端口根本不开，且无任何报错。这是"浏览器打不开/连不上"的最常见根因。

因此脚本使用专用配置目录（默认 `%LOCALAPPDATA%\doubao-manju\edge-profile`）：

- ✅ 远程调试端口可用
- ✅ 与用户日常浏览器实例**并存**，互不干扰
- ⚠️ 登录态独立：首次运行需在专用窗口登录一次豆包，Cookie 持久保存在该目录，之后免登录
- ⚠️ 无法"复用用户日常浏览器里的已有登录"——默认目录开不了调试端口，Cookie 也受 App-Bound 加密保护无法导出

### 端口与实例规则

- 先探测 `--port`（默认 9222）：已有可调试浏览器则直接复用，否则才拉起
- 同一配置目录只允许一个实例：若上次窗口还开着（但端口没开），Popen 会把请求转发给旧实例后退出 → 端口永远起不来。**解法：关掉旧的同配置窗口**
- 9222 被其他程序占用 → `--port 9223` 换端口
- 本地 CDP HTTP/WS 一律绕过系统代理（脚本已内置 `ProxyHandler({})`），装了代理的机器不受影响

### GUI 进程存活（Agent 沙箱环境注意）

脚本以 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 拉起浏览器，正常桌面/远程桌面会话中可常驻。若在**强沙箱 Agent 环境**里浏览器窗口反复消失（启动后 10-20s 被回收），是沙箱行为而非脚本问题——让用户在自己的终端里手动执行脚本，或手动运行：

```
msedge.exe --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\doubao-manju\edge-profile" about:blank
```

窗口稳定存在后再跑脚本（会自动复用 9222 端口）。

### 登录墙处理（已内置，不退出）

未登录判定 = 无输入框 或 有可见「登录」按钮 或 有登录引导文案。命中后脚本提示用户扫码并轮询等待（默认 600s），期间定期截图。**登录完成后自动继续，无需重跑。**

## 有效模式

### 输入框定位

豆包输入区可能同时存在 `textarea`、`input` 和 `contenteditable`（富文本）多个节点。
**取面积最大的可见元素**——那通常是对话主输入框，而非搜索框或隐藏表单。

### 提示词注入

豆包是 React 受控组件，直接 `el.value = text` 不会触发状态更新。必须用**原生 setter**：

```js
const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
            : el.tagName === 'INPUT'    ? HTMLInputElement.prototype
            : Object.getPrototypeOf(el);
const setter = Object.getOwnPropertyDescriptor(proto, 'value');
setter.set.call(el, text);          // 绕过 React 的 value 劫持
el.dispatchEvent(new Event('input', { bubbles: true }));
```

这是最容易踩的坑——赋值成功但界面无反应、发送后内容为空。

### 提交方式

**Enter 键优先于点击按钮**。原因：Enter 是真实用户行为，不易被页面拦截；且发送按钮的 class 名随版本变化，不稳定。

```js
el.dispatchEvent(new KeyboardEvent('keydown', { key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true }));
```

### 视频生成入口

不强制点击「视频生成」按钮。**直接在对话框发送带「时长N秒」的指令即可生成视频**，
这比寻找功能入口更稳定——入口位置随版本调整，而对话框结构相对固定。

### 渲染完成判定

**不要用 HTTP 状态或元素出现次数判定完成**，应轮询 `<video>` 元素的实际时长：

```js
[...document.querySelectorAll('video')]
  .map(v => v.duration)          // 时长 >= 3 视为出片完成
```

若选择器探到视频但 `duration` 为 0 或 `NaN`，说明仍在缓冲，继续轮询。

**历史视频会被虚拟化卸载**：滚动离开视口后旧消息里的 `<video>` 会从 DOM 移除（"视频数 2"过一会变"视频数 0"是正常现象）。
因此**不能用计数对比**判定本镜出片——提交时记录已存在视频的 src 基线，任何"基线外新 src 且时长≥3s"才算本镜结果（脚本已按此实现）。

**历史会话回放卡在 iframe 里**：重新打开旧会话时，主文档 0 个 `<video>`、视频播放器渲染在 iframe 内（跨 frame 取回需 CDP 附着 iframe target）。
而**刚生成完成的视频直接以 `<video>` 挂载在主文档**，主流程的检测与下载不受影响；但"从历史会话挽回视频"成本高，宁可重生成。

### 「视频生成参数确认」卡（2026-10-14 实测，关键流程）

**豆包（Seedance 2.0 Mini）收到视频提示词后不会直接渲染**，而是先回复一张参数确认卡：

> 视频生成参数确认
> 模型：Seedance 2.0 Mini / 时长：10 秒 / 比例：9:16 竖屏 / 画面方向 / 场景内容 / 镜头运动 / 光影质感 / 声音 / 画面文字
> **确认后我再开始生成视频。**

必须**再发送一次确认**，渲染才会启动。脚本的处理（回复驱动状态机，见 `wait_render`）：

1. 用 `lastIndexOf('视频生成参数确认')` 截取**最新一张卡**的文本（避免被历史消息干扰）
2. 解析 模型/时长/比例 并与镜头目标时长核对（不一致时日志警告，不阻塞）
3. 确认动作：优先点卡片上的确认类按钮，无按钮则回复"确认，开始生成视频"消息
4. 每镜**只确认 1 次**（确认后卡片仍留在会话历史，`lastIndexOf` 会持续命中旧卡，文本检测无法区分新旧卡；重复确认有触发重复生成、双倍耗额度的风险）
5. 同时监控页面错误文案（额度用尽/审核未通过/生成失败），与提交前基线比对，只认**新增**错误——命中立即失败，不空等 7 分钟超时

## 已知陷阱

| 陷阱 | 现象 | 规避 |
|------|------|------|
| **单次超 15 秒失败** | 提交后报错或生成极短片段 | 每镜时长必须 ≤15s，脚本已强制 |
| **只回参数卡不渲染** | 提交后豆包回复「视频生成参数确认」卡，无 video 元素 | 必须二次确认才开始渲染，脚本已自动处理，见「有效模式」同名章节 |
| **免费额度耗尽** | 确认后回复"今日视频生成免费次数用完了，你明天再来找我继续吧"，全程无渲染活动 | 额度文案是**全局致命错误**（不看基线快照，旧文案也触发）：当镜立即失败并**中断整批**，汇总输出待补镜号与续跑命令；次日额度刷新后用 `--only` 补齐，切勿继续提交（实测措辞：免费次数用完/明天再来/额度） |
| **并发触发限流** | 连续快速提交后被限流 | 逐镜串行，等上一镜渲染完成再发下一镜 |
| **对白被拦腰截断** | 提示词里出现 `"你终于回来了。。` 这类断句 | 拆解时做引号感知断句，`split_novel.py` 已处理 |
| **文字转语音串扰** | 视频里出现 AI 配音，与后期配音冲突 | 提示词中避免「旁白」「他说」等词，用纯画面描述 |
| **角色跨镜漂移** | 同一角色每镜长相不同 | 每镜重复固定外形描述，避免使用代词 |
| **角标不可去除** | 右下角有半透明「AI生成」角标 | 后期裁切或放大画布，非平台侧可解 |
| **默认配置目录开不了调试端口** | 启动参数带不上 `--user-data-dir` 时端口被静默忽略 | 必须用专用 `--user-data-dir`，脚本已内置，见环境前置条件 |
| **GUI 进程被沙箱回收** | 强沙箱 Agent 环境下浏览器窗口启动后 10-20s 消失 | 用户在自己的终端手动运行脚本或手动拉起浏览器（命令见环境前置条件） |
| **同配置目录二次实例失效** | 旧窗口开着但端口没开，新进程请求被转发后退出 | 关掉旧的同配置窗口再跑 |

## 选择器失效排查流程

豆包前端类名带哈希后缀，**不要硬编码 class 名**。脚本已采用多策略语义探测。
若仍失效，按序排查：

1. **保存 DOM 快照**
   ```js
   // 提取所有可交互元素的语义信息
   [...document.querySelectorAll('button,input,textarea,[role=button],[contenteditable]')]
     .slice(0,120)
     .map(e => ({ tag:e.tagName, text:(e.innerText||'').slice(0,50),
                  aria:e.getAttribute('aria-label'), testid:e.getAttribute('data-testid') }))
   ```
   优先用 `aria-label` / `data-testid` / `innerText` 定位，它们比 class 稳定。

2. **截图看真实渲染态**
   某些元素存在于 DOM 但不可见（`offsetParent === null`），筛选时必须加可见性判断。

3. **Shadow DOM 穿透**
   若主文档找不到，递归遍历：
   ```js
   const walk = root => [...root.querySelectorAll('*')]
     .flatMap(e => [e, ...(e.shadowRoot ? walk(e.shadowRoot) : [])]);
   ```

4. **在 `references/` 下记录新发现**
   追加到本文件的「有效模式」，标注发现日期，作为下次加速用。

## 提示：注入失败时改用真实输入事件

豆包对程序化构造 URL 敏感，但正常 UI 操作不受限。当 `Runtime.evaluate` 注入连续失败时，
优先检查是否不可见元素被误选（`offsetParent === null` 过滤），再考虑改用 CDP
`Input.dispatchKeyEvent` / `Input.dispatchMouseEvent` 派发真实输入事件——
一次成功交互既能完成任务，也能反向揭示页面真实行为。