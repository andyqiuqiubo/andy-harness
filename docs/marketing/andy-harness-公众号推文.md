# 大模型很会聊天，但一让它干活就翻车？我写了一个开源的 Agent 底座

> **备选标题 1**：《一个人、一台旧电脑、950+ 个测试用例：我把"管住大模型"做成了一门工程》
> **备选标题 2**：《别再往上下文里硬塞了：一个开源 Agent 底座的省钱设计与踩坑记录》

先说一个我自己的反差经历。

让大模型写周报、改文案，体验是真好，话说得漂亮，反应也快。可一旦让它干"实事"——读一个两万行的日志、把桌面上的文件归类、隔天还记得昨天的项目背景——就开始翻车：上下文说爆就爆，token 费用看得心惊；让它操作桌面，它每轮都带截图，同一张桌面反复计费；最尴尬的是聊到第三十轮，它把第一轮的需求忘得一干二净。

我的结论是：这不是模型不行，是缺一个"底座"。提示词决定模型怎么想，底座决定模型怎么把活安全、稳定、可观测地干完。想清楚这点之后，我花了几个月把这个底座做了出来——**andy-harness**，MIT 协议开源。

它不是又一个聊天界面，而是一套插件化 Agent Harness（智能体底座）：后端 FastAPI + SQLite，前端 Vue 3，外加一个 Tauri 桌面壳，一个原生窗口跑齐前后端，双击即用。

【配图：andy-harness 首页（数字雨背景 + 「开始解决问题 / 制定个性化」两个入口按钮）；来源：仓库 docs/manual/screenshots/01-home.png，原始 1440×900，建议裁剪为 900×506】

## 一、定位：一个"管"大模型的框架，不是聊天机器人

如果只用一句话介绍：andy-harness 把"让大模型干活"这件事工程化了。具体是四件事——

- **插件化**：工具、技能、UI 扩展、接入渠道全是插件，内核零业务逻辑；
- **可观测**：每次对话的模型调用、工具调用都落成 span 链，"哪里慢、哪里贵、哪里错"一眼看出来；
- **省 context**：工具超大输出自动落盘，长期记忆按相关性注入；
- **可控**：工具分只读 / 写入 / 危险三级，危险操作（比如执行代码）默认弹窗等你点「允许」。

默认形态是单用户、本机访问，数据全落在自己机器的 SQLite 和文件里，不依赖任何云服务。这也是我做它的初衷：我的项目资料、API Key、桌面文件，不想随便交出去。

【配图：对话页实际运行截图，需包含「执行过程」工具调用卡片、Markdown 渲染、回答下方 token 用量胶囊；来源：docs/manual/screenshots/03-chat-conversation.png，建议 1080×608】

## 二、一切皆插件：内核是真的"空"

内核代码在 `backend/harness/kernel/` 下，只做五件事：插件发现、清单校验、加载、激活/停用、服务注册与事件总线。没有任何业务逻辑。

加一个工具插件有多简单？看计算器插件的清单，一个 JSON 加一个入口类，齐活：

```json
{
  "id": "tool_calculator",
  "name": "计算器 Calculator",
  "version": "0.1.0",
  "type": "tool",
  "entry": "plugins.tool_calculator.main:CalculatorPlugin",
  "core_api": ">=0.1.0 <1.0.0"
}
```

模型怎么知道什么时候该用它？靠 description。装好后 AI 自己判断要不要调用，不用你写"如果用户说计算就触发"这种硬规则。

仓库内置 37 个插件，覆盖 Provider、工具、渠道、服务四大类，启动时自动扫描加载。装新能力有三条路：内置市场一键装、从 zip / git 装第三方包、或者照抄结构自己写一个。从 UI 装的插件即时生效，不用重启后端。

【配图：设置页「插件管理」标签截图（含插件市场入口、安装 / 启停按钮、系统插件标识）；来源：docs/manual/screenshots/04-settings-plugins.png，建议 1080×608】

## 三、省 token 是门功课：三个设计和一次报错

这是我在项目里花心思最多的地方。大模型干活贵，贵在上下文。下面三个设计，全是现实毒打出来的。

**1. Skills 三级渐进式披露。** 技能用 SKILL.md 描述，分三级加载：目录常驻（L1，每个技能只占约 100 token）、模型判断命中才加载正文（L2）、正文里提到的脚本和参考文档按需才读（L3）。装几十个技能，上下文也不会被撑爆——因为模型平时看到的永远只是一张"目录"。

**2. 大输出自动落盘。** 工具返回动辄几万字符？超过阈值（默认 6000 字符，约 1.5~2K token）就自动落盘成"制品"，上下文里只留摘要和路径。模型想看全文，用 `read_artifact` 分页回读，不会二次爆炸。

**3. compaction v2：不调 LLM 的压缩。** 常见的上下文压缩是"让模型总结旧消息"——费一次 token，还可能编。我改成确定性折叠：旧消息按「请求 / 工具调用 / 结果 / 结论」抽成结构化事件日志，零额外成本，零幻觉。

这里踩过一个真坑：裁剪边界如果切断 `assistant(tool_calls)` 和它后面的 tool 消息，DeepSeek 直接返回 400。所以代码里有个 `_safe_boundary`，专门把裁剪点挪到 tool 组之外：

```python
def _safe_boundary(self, messages: list[dict[str, Any]]) -> int:
    """计算一个不会切断 tool 组的最近区起始下标。"""
    start = max(0, len(messages) - self.keep_recent_messages)
    while start < len(messages):
        if messages[start].get("role") == "tool":
            start += 1
            continue
        if start > 0 and messages[start - 1].get("role") == "assistant" and messages[start - 1].get("tool_calls"):
            start += 1
            continue
        break
    return start
```

还有个更隐蔽的：DeepSeek 推理模型的 `reasoning_content`（思维链）在多轮对话里必须原样回传，否则报 "The reasoning_content in the thinking mode must be passed back to the API"。这个错不自己踩一次，光看文档根本注意不到。

【配图：左右拼接图——左侧「制品」标签页（落盘工件列表），右侧「轨迹」泳道图（模型/工具调用耗时与 token）；来源：docs/manual/screenshots/04-settings-artifacts.png 与 04-settings-tracing.png，各裁一半拼为 1080×608】

## 四、零依赖的语义检索：256 维哈希向量

长期记忆最早用 SQL LIKE 子串匹配，搜"猫"搜不到"喵星人"。上真向量检索又要引模型、引依赖，对一个"本机跑"的项目来说太重。

我的折中是写一个零依赖的 `HashingEmbedder`——blake2b 哈希把分词映射到 256 维桶，带正负号累加，余弦相似度是纯函数，离线完全可复现：

```python
def _hashes(self, token: str) -> tuple[int, float]:
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(h[:4], "big") % self._dim
    # 用另一组字节决定符号，减少碰撞相互抵消的偏差
    sign = 1.0 if h[4] & 1 else -1.0
    return bucket, sign
```

中文按单字、英文按词分词，词项重叠就能召回，比 LIKE 鲁棒得多。想要真语义（同义词、改写）？配一个环境变量 `HARNESS_EMBEDDING_MODEL` 就走 OpenAI 兼容 `/embeddings`；没配、没网、没命中，自动回退 LIKE，旧数据懒回填向量。主流程永远不被检索拖住。

【配图：设置页「记忆」标签截图（长期记忆列表 + 手动增删改查入口）；来源：docs/manual/screenshots/04-settings-memory.png，建议 1080×608】

## 五、让 AI 操作桌面，但别让它烧钱

Computer Use 是这项目最"野"的功能：截图、鼠标、键盘、文件读写、命令执行，全套 OS 级原语，模型"计划先行"再执行。

做的时候第一个坎就是钱：截图是图像 token，每轮都带，同一张桌面反复计费。我的解法在 `agent.py` 里——截图只当"一次性观察"：

```python
# 回填工具消息（文本，永久保留；截图图像不进 messages，只作一次性观察）
messages.extend(tool_messages)

# 本轮截图作为「最新观察」暂存，仅供下一轮一次性查看；处理完即丢弃
if screenshot_images:
    parts = [{"type": "text", "text": "以下是本轮截图（观察）："}]
    for img in screenshot_images:
        parts.append({"type": "image_url", "image_url": {"url": img}})
    self._pending_obs = {"role": "user", "content": parts}
```

文本结果永久留在对话里，截图只够下一轮看一眼，看完就扔。模型还容易"点不中就一直点"，所以加了卡死检测：连续 N 轮执行完全相同的实质操作（screenshot / wait 不算），自动中止并总结，不烧完轮数不收手。危险操作默认拦截，等你人工批准再 resume。

【配图：Computer Use 一次桌面任务的执行过程 GIF（计划 → 截图 → 点击 → 验证闭环）；仓库暂无对应素材，建议新录 15 秒 GIF，暂可用 gif/screenshot_2026-10-12_20-40-59.gif 顶替，建议 720×405】

## 六、多 Agent 协作：会派活的底座，才跑得动复杂任务

单代理干不了的大事，交给子代理。`task` 工具把自包含的子任务丢给一个"上下文干净"的独立会话，跑完只把结论摘要带回主对话——中间翻了几十页文档的过程，主上下文完全无感。

再往上是编排：`parallel` 让多个独立子任务并发（默认并发 4，按输入顺序返回，单任务炸了不拖累旁人）；`pipeline` 让"研究员 → 写作者 → 评审"这类角色串行接力，上一阶段产出经 `{input}` 传给下一阶段，失败即停。护栏写死了：一次最多 10 个任务、8 个阶段，子代理里禁用这两个工具，防无限递归。

【配图：工作流编排（Workflow Studio）画布截图——开始 / 结束 / LLM / 代码 / 条件分支等八类节点与连线；需实拍 /workflows 页面，建议 1080×608】

## 七、不想花一分钱，先把系统跑起来

不少开源 Agent 项目卡在"没有 API Key 就没法启动"。andy-harness 接入了 OortCloud 的免费额度：设置页点「免费领取」，跳统一登录页授权，后端自动领 10 元免费模型额度（NewAPI 令牌），自动建好 Provider，会话里直接选用。密钥本地加密存储，领取流程带 state 防重放。一杯奶茶钱的量，足够把全部功能试一遍。

【配图：设置页 Providers 标签或「智连器」面板中的 OortCloud 免费领取卡片截图；来源：docs/manual/screenshots/04-settings-providers.png（若无该卡片需实拍），建议 1080×608】

## 八、质量这块，我用数字说话

一个人开发的项目，最怕"看起来能跑"。我的门禁是这样的：

- 后端 pytest：953 passed / 3 skipped（2026-10-13 全量复跑），测试隔离问题已根除；
- 后端 mypy --strict：125 个源文件全过；
- 前端 vitest：108 个用例；eslint、vue-tsc、vite build 全绿；
- CI 两个 job（后端 / 前端）均为绿。

比数字更值钱的是踩坑表。挑四个最"典"的：

| 坑 | 现象 | 原因 |
|---|---|---|
| 127.0.0.1 打不开前端 | 地址栏输入 `127.0.0.1:5173` 连不上 | Vite 默认绑 localhost，Windows 上解析成 IPv6 `[::1]` |
| Node 22 装了还起不来 | 版本号明明"22+" | Vite 8 真实要求是 `^20.19.0` 或 `>=22.12.0`，22.0~22.11 不满足 |
| 干净克隆后 pytest 挂 1 个 | 一个桌面壳测试 failed | 它断言 `frontend/dist` 存在——先 `pnpm build` 再 `pytest` |
| C 盘悄悄涨几百 MB | 项目装在 D/E 盘也没用 | uv / pnpm / npm 缓存默认都落 C 盘用户目录，得显式重定向 |

这些全部写进了 README 的「实测运行指南」，含每条命令的耗时基线（`uv sync` 16 分钟、54 个包）和磁盘占用（约 2.1 GB）。抄作业不用猜。

【配图：CI Actions 全绿截图 + 本地 pytest 结尾「passed」终端截图，左右拼接 1080×608】

## 小结

回看这几个月的开发，最大的收获不是写出了多少功能，而是想明白一件事：大模型时代，"管"比"教"重要。提示词决定上限，底座决定下限——上下文不爆、token 不烧、危险操作有人看着、每次运行可回放，模型才真正从"会聊天的 Demo"变成"能干活的工具"。

andy-harness 现在处于 v1.0.0 开发者预览版，全部代码和文档开源。接下来你可以：

1. **想直接用**：clone 仓库，配一个 DeepSeek Key（或领 OortCloud 免费额度），双击 `start-all.bat` 跑起来；
2. **想学习**：读 `backend/harness/kernel` 和 `engine`，几千行核心代码讲清一个 Agent 底座怎么搭；
3. **想二次开发**：照抄 `tool_calculator` 写你的第一个插件，或用 DevKit 工作台生成脚手架。

仓库地址：https://github.com/andyqiuqiubo/andy-harness
配套使用手册（含 16 张界面截图）：`docs/andy-harness-v1.0.0用户使用手册.md`

⚠️ 最后提醒一句：本项目带 Computer Use 桌面操控能力，AI 真的会动你的鼠标键盘。跑之前请读一遍 `DISCLAIMER.md`，理解权限分级和风险边界，再用它干活。

有问题、有想法，欢迎来 GitHub 提 Issue——我一个人维护，但每一条都会看。

【配图：仓库 README 首页截图（功能特性列表 + CI 徽章）或项目主页；建议 900×506】
