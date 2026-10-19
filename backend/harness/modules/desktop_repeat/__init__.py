"""andy-repeat：桌面操作录制与确定性回放（录一遍，跑千遍）。

核心包 `desktop_repeat` 不 import fastapi / harness.api / plugins.*（设计 §2.3 硬约束），
允许依赖 harness.infra（atomic_write）与 harness.modules.artifact_store（仅路径解析）。

模块布局（对应详细设计 §2.1 分层）：
- platform/     L0 平台适配层（Windows 实现 + 协议）
- recorder/     L1 采集层（仅录制器子进程内运行）
- store/        L2 bundle 读写 / 清单 / 迁移
- interpreter/  L3 编译管线（事件流 → 步骤）
- locators/     L4 定位器注册表与实现
- executor/     L4 回放执行器（RepeatRunner）
- safety_gate.py 动作前置门
- service.py    对外唯一门面 DesktopRepeatService
- skill/        工作流 → SKILL.md
- cli.py        命令行入口
"""
