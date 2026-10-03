"""Computer Use Agent 循环。

职责（对应需求第 2、3 点）：
- 工具 Schema：screenshot / mouse_* / keyboard_* / file_read / file_write /
  command_exec / finish
- 模型调用循环：系统提示 -> 消息拼接 -> 调 DeepSeek V4.1 Flash ->
  解析 content + tool_calls -> 执行结果回填（截图以多模态图像注入）-> 多轮迭代
- 终止条件：模型不再调用工具 / 调用 finish / 达到最大轮数 / 整体超时
- 稳定性：单步命令超时、整体超时、失败重试、结果截断、异常兜底

所有「手/眼」操作都经 SafetyPolicy 评估，未授权或不合规一律拦截，
从源头避免模型失控。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
from typing import Any

from .client import ComputerUseModelError, DeepSeekClient
from .config import ComputerUseConfig
from .desktop import DesktopController, DesktopUnavailableError
from .safety import SafetyPolicy

logger = logging.getLogger("harness.computer_use.agent")

_SYSTEM_PROMPT = (
    "你是一个运行在用户本机上的 Computer Use（计算机操作）智能体，"
    "基于 DeepSeek V4.1 Flash 驱动。你的目标：用最少的步数完成桌面任务。\n\n"
    "可用工具：\n"
    "- screenshot：截取主屏幕，返回图像**及屏幕分辨率 (宽x高)**，用于关键节点观察。\n"
    "- mouse_move / mouse_click / mouse_drag：控制鼠标（屏幕绝对像素坐标）。\n"
    "- keyboard_type / keyboard_press：输入文本或组合键（如 'ctrl+l'、'enter'）。\n"
    "- file_read / file_write：在允许的工作目录内读写文本文件。\n"
    "- command_exec：在受控工作目录中执行一条 shell 命令。\n"
    "- wait：暂停若干秒，等待程序启动或网页加载完成。\n"
    "- finish：任务完成时调用，给出与用户提问语种一致的简明总结"
    "（内部思考仍可用中文）。\n\n"
    "═══ 工作守则（务必遵守，否则极易耗尽迭代轮数）═══\n"
    "1. **优先用键盘完成导航，尽量避免坐标点击**：\n"
    "   - 启动程序/打开网页：用 command_exec 执行 `start <程序名>`（如浏览器、记事本等）\n"
    "     启动；随后用 keyboard_press('ctrl+l') 聚焦地址栏 -> keyboard_type 输入完整网址\n"
    "     -> keyboard_press('enter')。**绝不要用鼠标去点地址栏、搜索框或任何输入框**。\n"
    "   - 在任何已打开的程序里填内容：先确保焦点在目标输入区，再 keyboard_type 输入。\n"
    "2. **【导航铁律】访问网址时，地址栏里 keyboard_type 的内容必须是\n"
    "   『以 http(s):// 开头的完整网址』**（例如 https://www.example.com/foo ）。\n"
    "   **严禁只输裸域名**（如 `example.com`）——裸域名常被浏览器\n"
    "   重定向到首页/推广页，且不同站点行为不一，极易失败。\n"
    "   **要在某网站内搜索：优先自己构造『该站点的搜索 URL』走地址栏输入，\n"
    "   而不是用鼠标去点搜索框/搜索按钮**（搜索框定位脆弱、易错位）。\n"
    "   **验证：网址回车后必须 screenshot 一次，确认页面已正确加载**；\n"
    "   若仍停在首页/推广页/旧页面，立刻用 ctrl+l 重输完整网址再回车。\n"
    "3. **启动程序或跳转网页后，先 wait(2~4) 等加载，再 screenshot 观察**；\n"
    "   不要在同一步骤里启动/跳转后立刻截图（看到的还是旧画面）。\n"
    "4. screenshot 结果会给出屏幕分辨率，点击坐标必须基于该分辨率推算；\n"
    "   若一次点不中，最多调整重试 1 次，仍不行就改用键盘方式。\n"
    "5. 每完成一个里程碑（页面打开、输入框就绪）截一张图验证即可，不要每步都截。\n"
    "6. **同一个动作若执行后画面无变化，不要反复用相同参数重试**，应换一种方法\n"
    "   或调用 finish 说明受阻原因。\n"
    "7. 任何被安全策略拦截（返回 [安全拦截] 或 [需要人工授权]）的操作，不要重试，\n"
    "   改方案或停止。\n"
    "8. 任务达成后务必调用 finish 结束，并给出简明中文总结。\n"
    "9. Windows 启动 GUI 程序务必用 `start <程序名>`（如 `start chrome`），\n"
    "   不要直接执行程序名（会被命令白名单拦截）。\n"
    "10. **command_exec 的命令首词必须是白名单内的命令本身**（如 echo、python、\n"
    "   dir、mkdir），**严禁加 `cmd /c`、`bash -c` 等外壳前缀**——白名单按首词\n"
    "   匹配，`cmd` 不在白名单内会被直接拦截。写文件用\n"
    "   `echo 内容 > 路径`（首词 echo 即可），不要写成 `cmd /c echo ...`。\n\n"
    "═══ 文本输入铁律（最高优先级，违反会直接失败）═══\n"
    "**keyboard_type 的 text 参数，永远只填『你要写进当前输入框里的那个内容本身』**，\n"
    "绝不要填：任务指令原文、操作步骤、你的分析或思考。\n"
    "- 动手前先想清楚当前焦点在哪里：ctrl+l 之后焦点在『地址栏』（只输网址）；\n"
    "  点击某输入框之后焦点在『该输入框』（只输对应的那一点内容）。\n"
    "- 错误示例：用户让你『在搜索框输入刘德华并搜索』，你却调用\n"
    "  keyboard_type(text='打开浏览器，输入刘德华，点击搜索') —— 这是错的，\n"
    "  会把整句指令当成文本填进框里。\n"
    "- 正确示例：keyboard_type(text='刘德华')（只填真正要输入的内容本身）。\n"
    "如果你不确定该填什么，优先用第 2 条的『地址栏直接输完整网址』方式，"
    "彻底绕开界面上的输入框，最不容易出错。\n\n"
    "═══ 步骤合并（减少模型调用次数 = 省钱）═══\n"
    "本任务按「计划 -> 执行」两阶段进行，请严格照计划走，不要临场加戏。\n"
    "执行阶段请尽量把**连续的、顺序相关的步骤合并到同一个回复里**，用多个 "
    "tool_calls 一起给出（按执行先后排列），例如：\n"
    "  keyboard_press('ctrl+l') -> keyboard_type(text='https://www.example.com/foo') "
    "-> keyboard_press('enter') 三个一起返回；\n"
    "  wait(3) -> screenshot 也可以一起返回。\n"
    "这样能显著减少来回调用次数（每一次模型调用都要计费，务必省着用）。\n"
    "但注意：凡是『必须先执行、等结果出来才能决定下一步』的步骤"
    "（如刚启动程序、要先截图确认页面是否打开），就先只发那一步，"
    "等下一轮看到截图/结果后再继续，不要盲目合并。\n\n"
    "═══ 语言策略 ═══\n"
    "你的内部思考、推理可用中文进行；但面向用户的**最终总结（finish 内容）**"
    "必须与用户本条消息所用语种一致：用户用中文提问就用中文总结，用英文提问就用英文总结，"
    "其他语种同理。\n"
)

# 暴露给模型的工具定义（OpenAI function-calling 格式）
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "截取当前主屏幕并返回图像，用于观察桌面当前状态。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_move",
            "description": "将鼠标移动到屏幕绝对坐标 (x, y)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "横坐标（像素）"},
                    "y": {"type": "integer", "description": "纵坐标（像素）"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_click",
            "description": "在屏幕坐标 (x, y) 处点击鼠标。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "横坐标（像素）"},
                    "y": {"type": "integer", "description": "纵坐标（像素）"},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "鼠标键，默认 left",
                    },
                    "clicks": {
                        "type": "integer",
                        "description": "点击次数，默认 1",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mouse_drag",
            "description": "从 (x1, y1) 拖拽到 (x2, y2)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer"},
                    "y1": {"type": "integer"},
                    "x2": {"type": "integer"},
                    "y2": {"type": "integer"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                },
                "required": ["x1", "y1", "x2", "y2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keyboard_type",
            "description": "在当前焦点处输入一段文本。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要输入的文本"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keyboard_press",
            "description": "按下一个按键或组合键，如 'enter'、'ctrl+a'、'alt+f4'。",
            "parameters": {
                "type": "object",
                "properties": {"keys": {"type": "string", "description": "按键，组合用 + 连接"}},
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "读取工作目录内的文本文件内容。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径（须在工作目录内）"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "向工作目录内的文本文件写入内容（覆盖）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "写入内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "command_exec",
            "description": "在受控工作目录中执行一条 shell 命令，返回其输出。",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "要执行的命令"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "暂停等待若干秒，用于等待程序启动或网页加载完成后再截图/操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "等待秒数，建议 2~5",
                    }
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "任务完成时调用，给出中文总结。",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string", "description": "任务结果总结"}},
                "required": ["summary"],
            },
        },
    },
]


# ── 导航护栏说明 ─────
# 本插件定位是「Computer Use（操控整个本机桌面）」，浏览器操作（browser use）
# 只是其中一个子集。因此这里**不**为任何具体 App / 网站（如某浏览器、某搜索引擎）
# 硬编码特殊逻辑或专属 URL——那样会把通用 Agent 变成「浏览器专用」工具。
# 取而代之的是一套**对一切桌面任务都成立**的通用护栏（见 _SYSTEM_PROMPT）：
#   ① keyboard_type 的 text 只能填「输入框里的内容本身」，不得填指令/思考；
#   ② 地址栏/导航须输入「以 http(s):// 开头的完整网址」，不得裸域名；
#   ③ 启动/导航后必须 wait + screenshot 验证；
#   ④ 优先键盘、尽量不靠坐标点。
# 具体走哪条路径（点搜索框还是地址栏输 URL、用哪个程序）由模型自行决定。


class ComputerUseAgent:
    """Computer Use 自主操作循环。"""

    def __init__(
        self,
        config: ComputerUseConfig,
        safety: SafetyPolicy,
        desktop: DesktopController,
        client: DeepSeekClient,
        service: Any,
        run_approved: bool,
    ) -> None:
        self.config = config
        self.safety = safety
        self.desktop = desktop
        self.client = client
        self.service = service
        self.run_approved = run_approved
        self._goal = ""  # 任务目标，供 keyboard_type 纠偏使用（run 时填充）
        self._llm_calls = 0  # 模型调用次数计数（成本闸门）
        self._pending_obs: dict[str, Any] | None = None  # 仅保留最新截图作一次性观察
        self._desktop_used = False  # 本次任务是否实际触发了桌面写操作（用于结果状态头）

    # ── 入口 ──────────────────────────────────────────
    async def run(self, goal: str, run_id: str) -> str:
        """执行一轮 Computer Use 任务，返回最终总结文本。"""
        self._goal = goal  # 供 keyboard_type 纠偏（防止把整段指令当输入内容）
        self._llm_calls = 0
        self._pending_obs = None
        self._desktop_used = False

        # ── 计划阶段（可选）：先用一次调用产出步骤清单，让执行更线性、少走弯路 ──
        plan_text = ""
        if self.config.plan_first:
            plan_text = await self._make_plan(goal)
            if plan_text:
                logger.info("Computer Use 执行计划:\n%s", plan_text)

        # 初始消息：系统提示 + 目标（+ 计划上下文，要求模型严格照计划并合并步骤）
        goal_msg = goal
        if plan_text:
            goal_msg = (
                f"{goal}\n\n【参考执行计划】\n{plan_text}\n"
                "请严格按上述计划执行；能合并的连续步骤尽量在一个回复里用多个 "
                "tool_calls 一起给出（按执行顺序）。"
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": goal_msg},
        ]

        start = time.time()
        iterations = 0
        # 最近若干轮「实质操作」签名，用于卡死检测
        recent_signatures: list[str] = []

        while self._llm_calls < self.config.max_llm_calls:
            # 整体超时：到点立即安全中止，返回已收集信息
            if time.time() - start > self.config.overall_timeout:
                return self._finalize(self._abort_summary(messages, iterations, "整体超时"), plan_text)

            # 构造本轮请求：持久消息 + 上一轮产生的最新截图（一次性观察，不累积）
            # —— 关键省钱点：截图只作为「下一次调用的一次性观察」发出，
            #    处理完即丢弃，绝不留在历史里被后续每次调用反复重发。
            payload = list(messages)
            if self._pending_obs is not None:
                payload.append(self._pending_obs)
                self._pending_obs = None  # 已消费，丢弃

            try:
                resp = await self.client.complete(payload, TOOL_SCHEMAS)
            except ComputerUseModelError as e:
                return self._finalize(f"模型调用失败，任务已中止：{e}", plan_text)

            self._llm_calls += 1
            content = resp.get("content") or ""
            tool_calls = resp.get("tool_calls") or []

            # 追加 assistant 消息（带 tool_calls，供后续 tool 消息配对）
            messages.append(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": tool_calls,
                }
            )

            # 无工具调用 -> 终答
            if not tool_calls:
                return self._finalize(content or "任务已完成（模型给出终答）。", plan_text)

            # 执行本轮所有工具调用。模型可在一次回复里返回多个 tool_calls，
            # 循环按返回顺序顺序执行 → 因此可把连续的、顺序相关的步骤合并到一轮。
            tool_messages: list[dict[str, Any]] = []
            screenshot_images: list[str] = []
            hit_approval = False
            approval_text = ""
            # 记录本轮的「实质性操作」签名（不含 screenshot/wait），用于卡死检测
            action_sigs: list[str] = []

            for tc in tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = fn.get("name", "")
                args = self._parse_args(fn.get("arguments", ""))

                # 非观察类操作计入卡死检测（screenshot/wait 不计入）
                if name not in ("screenshot", "wait", "finish"):
                    action_sigs.append(f"{name}:" + json.dumps(args, sort_keys=True, ensure_ascii=False))

                # finish：立即结束并返回总结（优先于同轮其它调用）
                if name == "finish":
                    return self._finalize(str(args.get("summary", content or "任务已完成。")), plan_text)

                # 安全评估：未授权/不合规一律拦截，不执行
                decision = self.safety.evaluate(name, args, self.run_approved)
                if not decision.allowed:
                    if decision.requires_approval:
                        hit_approval = True
                        approval_text = decision.reason
                        # 记录待审批，供人工在审批接口中放行
                        self.service.mark_pending(run_id, decision.signature, decision.reason)
                        result_text = (
                            f"[需要人工授权] {decision.reason}（run_id={run_id}）。"
                            f"请通过审批接口批准后，使用 resume_run_id='{run_id}' 重新执行。"
                        )
                    else:
                        result_text = f"[安全拦截] {decision.reason}"
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result_text,
                        }
                    )
                    continue

                # 执行（异常兜底，不让单步失败拖垮整个任务）
                try:
                    result_text, image = await self._dispatch(name, args)
                    # 实际触发了桌面写操作（鼠标/键盘），用于结果状态头，
                    # 防止外层 Agent 误以为「桌面从未被操作」。
                    if name in (
                        "mouse_move",
                        "mouse_click",
                        "mouse_drag",
                        "keyboard_type",
                        "keyboard_press",
                    ):
                        self._desktop_used = True
                except DesktopUnavailableError as e:
                    result_text, image = f"[桌面不可用] {e}", None
                except Exception as e:  # noqa: BLE001
                    logger.exception("工具 %s 执行异常", name)
                    result_text, image = f"[执行异常] {e}", None

                tool_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result_text,
                    }
                )
                if image:
                    screenshot_images.append(image)

            # 回填工具消息（文本，永久保留；截图图像不进 messages，只作一次性观察）
            messages.extend(tool_messages)

            # 本轮截图作为「最新观察」暂存，仅供下一轮一次性查看；处理完即丢弃
            if screenshot_images:
                parts: list[dict[str, Any]] = [{"type": "text", "text": "以下是本轮截图（观察）："}]
                for img in screenshot_images:
                    parts.append({"type": "image_url", "image_url": {"url": img}})
                self._pending_obs = {"role": "user", "content": parts}

            # ── 卡死检测：连续 N 轮执行完全相同的实质操作且无变化 ──
            # 避免模型在「点不中 / 页面没反应」时反复重试，把轮数烧光。
            if action_sigs:
                recent_signatures.append("|".join(action_sigs))
                if (
                    len(recent_signatures) >= self.config.stagnation_limit
                    and len(set(recent_signatures[-self.config.stagnation_limit :])) == 1
                ):
                    return self._finalize(
                        self._abort_summary(
                            messages,
                            iterations + 1,
                            f"检测到连续 {self.config.stagnation_limit} 轮执行了完全相同的操作且未见进展"
                            "（疑似卡死），已提前中止以免耗尽轮数",
                        ),
                        plan_text,
                    )

            iterations += 1

            # 命中待授权：本 run 无法继续，返回提示等待人工授权
            if hit_approval:
                return self._finalize(
                    f"{approval_text}\n\n（run_id={run_id}，等待人工授权后重试）",
                    plan_text,
                )

        # 达到模型调用预算上限：强制收尾，避免悬空与超支
        return self._finalize(
            self._abort_summary(messages, self._llm_calls, "达到模型调用次数预算上限"),
            plan_text,
        )

    # ── 计划阶段 ──────────────────────────────────────
    async def _make_plan(self, goal: str) -> str:
        """计划先行：用一次『禁用工具』的调用让模型产出步骤清单。

        仅消耗 1 次调用，却能让后续执行更线性、少走弯路，并给用户成本透明度
        （用户可在执行前先看计划再决定是否继续）。
        """
        plan_sys = (
            _SYSTEM_PROMPT + "\n\n你当前处于『规划阶段』：只输出一份简明、可执行的步骤清单"
            "（每步一行，注明会用到的工具与关键参数），不要调用任何工具。"
            "规划要贯彻：键盘优先、尽量少截图、能用键盘+地址栏/焦点直接输入完整内容"
            "就别去点界面元素。"
        )
        try:
            resp = await self.client.complete(
                [
                    {"role": "system", "content": plan_sys},
                    {"role": "user", "content": goal},
                ],
                [],  # tools=[] 禁用工具，强制只输出文本计划
            )
            self._llm_calls += 1
            return (resp.get("content") or "").strip()
        except ComputerUseModelError as e:
            logger.warning("Computer Use 计划阶段失败（跳过规划）: %s", e)
            return ""

    def _finalize(self, text: str, plan: str) -> str:
        """在任务结果前附上『事实状态头』+ 执行计划，提升透明度并锚定外层 Agent。

        状态头如实声明桌面是否真的被操作过，防止外层主对话 Agent 把
        『任务没确认成功』误读成『桌面自动化不可用』而脑补出假的失败原因。
        """
        if self.desktop.available and self._desktop_used:
            status = "【本机桌面自动化已实际执行（鼠标/键盘/程序已真实操作）】"
        elif not self.desktop.available:
            status = "【本机桌面自动化不可用（缺 pyautogui/mss 或无显示器），仅执行了受限步骤】"
        else:
            status = "【本任务未实际触发桌面写操作】"
        head = f"{status}\n\n"
        if plan:
            return f"{head}【执行计划】\n{plan}\n\n【执行结果】\n{text}"
        return f"{head}{text}"

    # ── 工具分发 ──────────────────────────────────────
    async def _dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, str | None]:
        """执行单个工具，返回 (文本结果, 截图 data-url 或 None)。"""
        if name == "screenshot":
            png = await self.desktop.screenshot()
            # 解析屏幕分辨率回传给模型，使其能基于分辨率推算点击坐标
            try:
                from PIL import Image

                with Image.open(io.BytesIO(png)) as im:
                    w, h = im.size
            except Exception:  # noqa: BLE001
                w = h = 0
            data_url, saved = self._encode_screenshot(png)
            dim = f"（屏幕分辨率 {w}x{h}）" if w and h else ""
            return f"已截图，保存于 {saved}{dim}（图像已作为观察提供）。", data_url

        if name == "mouse_move":
            x, y = int(args["x"]), int(args["y"])
            await self.desktop.mouse_move(x, y)
            return f"鼠标已移动到 ({x}, {y})。", None

        if name == "mouse_click":
            x, y = int(args["x"]), int(args["y"])
            button = str(args.get("button", "left"))
            clicks = int(args.get("clicks", 1))
            await self.desktop.mouse_click(x, y, button=button, clicks=clicks)
            return f"已在 ({x}, {y}) 点击 {button} 键 {clicks} 次。", None

        if name == "mouse_drag":
            x1, y1, x2, y2 = (
                int(args["x1"]),
                int(args["y1"]),
                int(args["x2"]),
                int(args["y2"]),
            )
            await self.desktop.mouse_drag(x1, y1, x2, y2, button=str(args.get("button", "left")))
            return f"已从 ({x1}, {y1}) 拖拽到 ({x2}, {y2})。", None

        if name == "keyboard_type":
            text = str(args.get("text", ""))
            # 纠偏：模型误把「任务指令原文」当成要输入的内容填入输入框。
            # 直接拦截并返回纠正提示，避免把整段指令当搜索词/文本填进去，
            # 同时把纠正信息回灌给模型让其重填正确内容（self._goal 在 run 时设置）。
            goal_norm = self._goal.strip()
            text_norm = text.strip()
            if goal_norm and len(text_norm) > 12 and (text_norm == goal_norm or goal_norm in text_norm):
                return (
                    "[纠正] 你刚才把『任务指令原文』当成了要输入的文本，"
                    "系统已拒绝执行。keyboard_type 的 text 必须只填『要写进当前"
                    "输入框里的那个内容本身』，绝不能填任务目标、操作步骤或你的思考。\n"
                    "例如要搜索某词，text 应只填那个词本身；"
                    "更稳妥的做法是用 keyboard_press('ctrl+l') 在地址栏输入"
                    "『以 http(s):// 开头的完整网址』后回车，绕开界面输入框。请重新调用。"
                ), None
            await self.desktop.keyboard_type(text)
            return f"已输入文本: {text!r}。", None

        if name == "keyboard_press":
            keys = str(args.get("keys", ""))
            await self.desktop.keyboard_press(keys)
            return f"已按下按键: {keys}。", None

        if name == "file_read":
            return self._read_file(str(args.get("path", ""))), None

        if name == "file_write":
            return (
                self._write_file(str(args.get("path", "")), str(args.get("content", ""))),
                None,
            )

        if name == "command_exec":
            return await self._run_command(str(args.get("command", ""))), None

        if name == "wait":
            # 等待页面加载/程序启动，避免「启动后立刻截图看到旧画面」导致的误判重试
            try:
                secs = float(args.get("seconds", 2))
            except (TypeError, ValueError):
                secs = 2.0
            await asyncio.sleep(min(secs, 30.0))
            return f"已等待 {secs}s。", None

        return f"未知工具: {name}", None

    # ── 命令执行（单步超时 + 重试 + 截断） ──────────
    async def _run_command(self, command: str) -> str:
        last_err: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=self.config.working_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.config.step_timeout)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    return f"[超时] 命令在 {self.config.step_timeout}s 内未完成，已被终止。"
                text = out.decode("utf-8", errors="ignore")
                return self._truncate(f"[exit={proc.returncode}]\n{text}")
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < self.config.max_retries:
                    await asyncio.sleep(2**attempt)
        return f"[执行失败] {last_err}"

    # ── 文件读写（作用域已在 SafetyPolicy 校验） ─────
    def _read_file(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                return self._truncate(f"[文件 {path}]\n{fh.read()}")
        except Exception as e:  # noqa: BLE001
            return f"[读文件失败] {e}"

    def _write_file(self, path: str, content: str) -> str:
        try:
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            return f"[已写入] {path}（{len(content)} 字符）"
        except Exception as e:  # noqa: BLE001
            return f"[写文件失败] {e}"

    # ── 截图编码（缩放 + JPEG 压缩 + 落盘） ─────────
    def _encode_screenshot(self, png_bytes: bytes) -> tuple[str, str]:
        """将 PNG 缩放/压缩为 JPEG data-url，并保存原图供用户查看。"""
        try:
            from PIL import Image

            img: Image.Image = Image.open(io.BytesIO(png_bytes))
            max_dim = self.config.max_image_dimension
            if max(img.size) > max_dim:
                scale = max_dim / float(max(img.size))
                img = img.resize((int(img.size[0] * scale), int(img.size[1] * scale)))
            buf = __import__("io").BytesIO()
            img.save(buf, format="JPEG", quality=self.config.jpeg_quality)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            data_url = f"data:image/jpeg;base64,{b64}"
        except Exception as e:  # noqa: BLE001
            logger.warning("截图编码失败（退回原图）: %s", e)
            data_url = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"

        # 保存原图到工作目录，便于用户事后查看
        saved = ""
        try:
            os.makedirs(self.config.working_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            saved = os.path.join(self.config.working_dir, f"screenshot_{ts}.png")
            with open(saved, "wb") as fh:
                fh.write(png_bytes)
        except Exception as e:  # noqa: BLE001
            logger.warning("截图落盘失败: %s", e)
        return data_url, saved

    # ── 辅助 ─────────────────────────────────────────
    @staticmethod
    def _parse_args(raw: str) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}

    def _truncate(self, text: str) -> str:
        """结果体积截断，避免超长输出撑爆上下文。"""
        limit = self.config.max_output_chars
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n...[输出已截断，原长 {len(text)} 字符]"

    def _abort_summary(self, messages: list[dict[str, Any]], iterations: int, reason: str) -> str:
        """中止时的统一收尾：返回原因 + 已完成步数。"""
        return (
            f"任务被终止：{reason}（已迭代 {iterations} 轮）。"
            "可缩小目标范围后重试，或在 interactive 模式下先完成人工授权。"
        )
