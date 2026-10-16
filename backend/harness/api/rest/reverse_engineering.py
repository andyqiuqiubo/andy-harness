"""REST API —— 工程强逆向（端到端软件逆向流水线）。

挂载在 ``/api/reverse-engineering`` 前缀下：

- ``GET    /api/reverse-engineering/status``                         工具链体检（七阶段引擎可用性）
- ``POST   /api/reverse-engineering/classify``                       输入分类与处理流程路由（exe/源码/文本）
- ``POST   /api/reverse-engineering/runs``                           启动一次逆向流水线（后台执行，立即返回 run_id）
- ``GET    /api/reverse-engineering/runs``                           运行历史
- ``GET    /api/reverse-engineering/runs/{run_id}``                  运行详情
- ``GET    /api/reverse-engineering/runs/{run_id}/progress``         实时进度（SSE 流式推送阶段/步骤事件）
- ``POST   /api/reverse-engineering/runs/{run_id}/rebuild``          手工重建「功能等价的新 exe」
- ``GET    /api/reverse-engineering/runs/{run_id}/evidence``         Evidence v2 校验报告

流水线共七阶段：REA 静态 / Java class 反编译 / Frida 动态 / Firecracker 沙箱 /
PentAGI 分析 / **功能等价重建（executable 样本自动产出新 exe）** / Evidence v2 证据固化。
（阶段顺序以 :data:`~harness.modules.reverse_engineering.models.STAGE_ORDER` 为准。）

实时进度机制
============

``POST /runs`` 不再同步阻塞等待整条流水线跑完，而是在**后台 asyncio 任务**中执行、
立即返回 ``{"run_id": ..., "status": "running"}``，使前端能立即订阅进度流而不被主流程阻塞。
流水线通过 :class:`ProgressReporter` 把阶段/步骤事件写入 :data:`progress_store` 内存总线，
``GET /runs/{run_id}/progress`` 用 ``StreamingResponse``（SSE）把这些事件流式推给前端：

- 订阅者连接后**先回放**该 run_id 已产生的历史事件，再接收实时增量（迟到连接/重连都安全）；
- 收到 ``run_done`` / ``run_error`` 后流自然关闭。

所有进度推送均为非阻塞内存操作，绝不影响逆向主流程的执行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from harness.api.deps import require_admin
from harness.api.errors import APIError
from harness.modules.reverse_engineering.input_dispatch import (
    InputClassifier,
    InputKind,
    subject_from_path,
)
from harness.modules.reverse_engineering.models import STAGE_JAVA_DECOMPILE, STAGE_ORDER
from harness.modules.reverse_engineering.pipeline import ReverseEngineeringPipeline, _validate_run_id
from harness.modules.reverse_engineering.progress import progress_store

logger = logging.getLogger("harness.api.reverse_engineering")

router = APIRouter(prefix="/api/reverse-engineering", tags=["reverse-engineering"])

# 后台任务引用，避免被事件循环 GC 回收（仅用于在日志中追踪，不参与结果读取）。
_bg_tasks: set[asyncio.Task[Any]] = set()

# 上传体积上限（字节）。
# 逆向样本（安装包 / APK）天然大于普通附件，故默认值给得宽裕；但**绝不能无上限**——
# 否则单个请求持续写盘即可耗尽磁盘（对比 knowledge_base/limits.py 的同类约定）。
# 需要分析更大样本时用 HARNESS_REV_ENG_MAX_UPLOAD 覆盖。
MAX_UPLOAD_BYTES: int = int(os.environ.get("HARNESS_REV_ENG_MAX_UPLOAD", 512 * 1024 * 1024))

_CHUNK_BYTES = 1024 * 1024


def _pipeline() -> ReverseEngineeringPipeline:
    return ReverseEngineeringPipeline()


class RunRequest(BaseModel):
    """启动一次逆向流水线。"""

    subject: str
    config: dict[str, Any] = Field(default_factory=dict)


class ClassifyRequest(BaseModel):
    """输入分类请求：``subject``（磁盘文件路径）与 ``text``（文本内容）二选一。

    - 同时提供时以 ``subject`` 为准（文件优先）；
    - ``filename`` 仅对 ``text`` 生效（作为扩展名提示，如 ``"a.py"``）；
    - ``analyze=False`` 时只返回分类与流程，跳过轻量预分析（更快）。
    """

    subject: str = ""
    text: str = ""
    filename: str = ""
    analyze: bool = True


@router.post("/classify", dependencies=[Depends(require_admin)])
async def classify_subject(req: ClassifyRequest) -> dict[str, Any]:
    """识别输入类型（可执行文件/源代码/文本描述），返回分类依据与对应处理流程。"""
    from harness.modules.reverse_engineering.input_dispatch import (
        InputClassifier,
        subject_from_path,
        subject_from_text,
    )

    if req.subject:
        target = Path(req.subject)
        if not target.is_file():
            raise APIError("REV_ENG_SUBJECT_NOT_FOUND", f"待分类文件不存在：{target}", 404)
        subject = subject_from_path(target)
    elif req.text:
        subject = subject_from_text(req.text, req.filename)
    else:
        raise APIError("REV_ENG_INVALID_CONFIG", "需提供 subject（文件路径）或 text（文本内容）", 400)

    return InputClassifier().route(subject, analyze=req.analyze).to_dict()


def _require_admin_sse(request: Request) -> dict[str, Any] | None:
    """SSE 专用的管理员鉴权依赖。

    ``EventSource`` 无法携带自定义请求头，因此认证启用时改由 ``?token=`` 查询参数
    传递令牌（与 ``/ws/chat`` 的握手方式一致）；其余情况与 :func:`require_admin` 行为一致：

    - 认证未启用（本地单用户）→ 直接放行；
    - 认证启用且 ``Authorization`` 头已带来（理论上 SSE 不支持，但保持兼容）→ 校验管理员；
    - 认证启用且仅 ``?token=`` → 校验令牌与管理员身份。
    """

    user: dict[str, Any] | None = getattr(request.state, "user", None)
    if user is not None:
        if not isinstance(user, dict) or not user.get("is_admin"):
            raise APIError("FORBIDDEN", "需要管理员权限", 403)
        return user

    # 惰性导入，避免模块加载期循环依赖（harness.main 在启动时装配 _services）。
    from harness.main import _services
    from harness.modules.auth_manager.service import AuthService

    auth_service = _services.get(AuthService) if _services is not None and _services.has(AuthService) else None
    if auth_service is None or not getattr(auth_service, "enabled", False):
        return None  # 认证未启用 → 放行（本地单用户默认行为）

    token = request.query_params.get("token", "")
    if not token:
        raise APIError("UNAUTHORIZED", "未认证或登录已过期，请重新登录", 401)
    authed = auth_service.user_from_token(token)
    if authed is None:
        raise APIError("UNAUTHORIZED", "登录已失效，请重新登录", 401)
    if not getattr(authed, "is_admin", False):
        raise APIError("FORBIDDEN", "需要管理员权限", 403)
    # 落到 request.state，保持与中间件一致，供下游逻辑复用
    request.state.user = authed.to_dict()
    authed_user: dict[str, Any] = request.state.user
    return authed_user


@router.get("/status", dependencies=[Depends(require_admin)])
async def status() -> dict[str, Any]:
    return _pipeline().toolchain_status()


async def _run_background(subject: str, config: dict[str, Any], run_id: str) -> None:  # noqa: E501
    """后台执行流水线：异常已在 pipeline 内固化为 FAILED 记录并广播，这里仅兜底。

    仅当 pipeline 尚未广播任何终止事件（如启动前校验就抛错）时才补发 ``run_error``，
    避免同一异常产生两条终止事件扰动订阅端。
    """
    try:
        await ReverseEngineeringPipeline().run(subject, config)
    except Exception as exc:  # noqa: BLE001 —— 编排器层未捕获的异常
        logger.exception("[rev-eng %s] 后台流水线异常", run_id)
        if progress_store.has_terminal(run_id):
            return
        progress_store.emit(
            run_id,
            {
                "type": "run_error",
                "run_id": run_id,
                "error": f"{exc}",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
        )


def _subject_type_from_classification(route: dict[str, Any]) -> str:
    """由分类证据推导出 Evidence v2 使用的 subject_type。"""
    evidence = route.get("classification", {}).get("evidence", {})
    fmt = evidence.get("format", "")
    if fmt == "pe":
        bits = evidence.get("bits", 32)
        return f"pe{bits}"
    if fmt == "elf":
        return "elf"
    if fmt == "macho":
        return "macho"
    if fmt == "java_class":
        return "java_class"
    return "unknown"


def _config_from_classification(route: dict[str, Any], base_cfg: dict[str, Any]) -> dict[str, Any]:
    """根据自动分类结果选择流水线阶段：exe 跑全链、class 仅反编译、源码仅固化分类。"""
    kind = route.get("classification", {}).get("kind", "")
    cfg = dict(base_cfg)
    cfg["input_kind"] = kind
    cfg["plan_flow"] = route.get("plan", {}).get("flow", "")
    cfg["subject_type"] = _subject_type_from_classification(route)
    evidence = route.get("classification", {}).get("evidence", {})
    if "major_version" in evidence:
        cfg["subject_major_version"] = evidence["major_version"]

    if kind == InputKind.EXECUTABLE:
        # 可执行文件：跑完整七阶段流水线（不限制 stages）
        cfg.pop("stages", None)
    elif kind == InputKind.JAVA_CLASS:
        cfg["stages"] = [STAGE_JAVA_DECOMPILE]
    elif kind == InputKind.SOURCE_CODE:
        cfg["stages"] = []
    else:
        # 无法识别的二进制/其它：默认不跑深度阶段，只固化分类事实
        cfg["stages"] = []
    return cfg


@router.post("/runs/upload", dependencies=[Depends(require_admin)])
async def create_run_upload(
    file: UploadFile = File(...),
    notes: str = Form(""),
    config_json: str = Form("{}"),
) -> dict[str, Any]:
    """上传文件并自动识别类型后启动逆向流水线。

    与 ``POST /runs`` 的区别：本端点接收 multipart 文件，保存到
    ``runs/<run_id>/uploads/`` 后调用 ``input_dispatch`` 自动分类，
    并据此决定执行哪些阶段（exe → 全链；class → 仅 java_decompile；
    源码/未知 → 仅 evidence 固化分类事实）。
    """
    try:
        base_cfg = json.loads(config_json)
        if not isinstance(base_cfg, dict):
            raise ValueError  # noqa: TRY301
    except (json.JSONDecodeError, ValueError):
        raise APIError("REV_ENG_INVALID_CONFIG", "config_json 必须是 JSON 对象", 400)

    filename = (file.filename or "upload.bin").replace("\\", "/").split("/")[-1]
    safe_name = "".join(c for c in filename if c.isalnum() or c in "._-") or "upload.bin"
    # 白名单保留了 '.'，故 "." / ".." / "..." 这类纯点名仍会通过上述过滤；
    # 拼进路径后指向的是**目录**，open("wb") 会抛 IsADirectoryError（对外表现为 500）。
    # 显式回退默认名，避免畸形文件名把正常请求打成服务端错误。
    if set(safe_name) == {"."}:
        safe_name = "upload.bin"

    cfg = dict(base_cfg)
    try:
        run_id = _validate_run_id(cfg["run_id"]) if cfg.get("run_id") else uuid.uuid4().hex[:12]
    except ValueError as e:
        raise APIError("REV_ENG_INVALID_CONFIG", str(e), 400)
    cfg["run_id"] = run_id
    cfg["notes"] = notes

    pipe = ReverseEngineeringPipeline()
    run_dir = pipe.runs_dir / run_id
    upload_dir = run_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved_path = upload_dir / safe_name

    total = 0
    try:
        with saved_path.open("wb") as fh:
            while chunk := await file.read(_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    break
                fh.write(chunk)
    except OSError as exc:
        raise APIError("REV_ENG_UPLOAD_FAILED", f"保存上传文件失败：{exc}", 500)
    if total > MAX_UPLOAD_BYTES:
        # 超限：清掉已落盘的残片，避免半个样本被后续流水线当成完整输入。
        saved_path.unlink(missing_ok=True)
        raise APIError(
            "REV_ENG_UPLOAD_TOO_LARGE",
            f"上传文件超过上限（{MAX_UPLOAD_BYTES} 字节）",
            413,
        )

    route = InputClassifier().route(subject_from_path(saved_path), analyze=False).to_dict()
    cfg = _config_from_classification(route, cfg)

    task = asyncio.create_task(_run_background(str(saved_path), cfg, run_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    return {
        "run_id": run_id,
        "status": "running",
        "subject_name": safe_name,
        "subject_path": str(saved_path),
        "classification": route["classification"],
        "plan": route["plan"],
    }


@router.post("/runs", dependencies=[Depends(require_admin)])
async def create_run(req: RunRequest) -> dict[str, Any]:
    subject_path = Path(req.subject)
    if not subject_path.is_file():
        raise APIError("REV_ENG_SUBJECT_NOT_FOUND", f"分析目标不存在：{subject_path}", 404)

    cfg = dict(req.config or {})
    # 同步校验 config（与 pipeline 一致的语义），在后台调度前就返回 400，避免挂起
    try:
        run_id = _validate_run_id(cfg["run_id"]) if cfg.get("run_id") else uuid.uuid4().hex[:12]
    except ValueError as e:
        raise APIError("REV_ENG_INVALID_CONFIG", str(e), 400)
    cfg["run_id"] = run_id
    selected = cfg.get("stages")
    if selected is not None:
        if not isinstance(selected, (list, tuple)):
            raise APIError("REV_ENG_INVALID_CONFIG", "config.stages 必须是阶段名数组", 400)
        unknown = [s for s in selected if s not in STAGE_ORDER]
        if unknown:
            raise APIError("REV_ENG_INVALID_CONFIG", f"未知阶段：{unknown}；可选：{STAGE_ORDER}", 400)

    # 后台执行：立即返回 run_id，进度由 /runs/{run_id}/progress 流式推送（非阻塞主流程）
    task = asyncio.create_task(_run_background(req.subject, cfg, run_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"run_id": run_id, "status": "running"}


@router.get("/runs", dependencies=[Depends(require_admin)])
async def list_runs() -> list[dict[str, Any]]:
    return _pipeline().list_runs()


@router.get("/runs/{run_id}", dependencies=[Depends(require_admin)])
async def get_run(run_id: str) -> dict[str, Any]:
    data = _pipeline().get_run(run_id)
    if data is None:
        raise APIError("REV_ENG_RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    return data


@router.get("/runs/{run_id}/progress", dependencies=[Depends(_require_admin_sse)])
async def run_progress(run_id: str) -> StreamingResponse:
    """SSE：实时推送该运行的阶段/步骤进度事件。

    连接后先回放历史事件、再增量推送；收到 run_done / run_error 后关闭。

    边界守护（后端重启场景）：进度总线是**进程内存**，重启后历史事件全部丢失——

    - run 不存在 → 404（订阅端立刻报错，而不是永久空挂等待）；
    - run 已终结（ok/degraded/failed，含启动时收敛为 interrupted 的遗留记录）→
      直接合成一条终态事件（``run_done`` / ``run_error``）并关闭，
      前端据此立即收尾，不会对着一条死 run 永远"等待中"。
    """

    # POST /runs 返回时后台任务可能尚未落盘 run.json（竞态）：短暂等待记录出现，
    # 最多 ~3s；仍无则确认为不存在的 run → 404（订阅端立刻报错而非永久空挂）。
    rec: dict[str, Any] | None = None
    for _ in range(30):
        rec = _pipeline().get_run(run_id)
        if rec is not None:
            break
        await asyncio.sleep(0.1)
    if rec is None:
        raise APIError("REV_ENG_RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)

    async def event_stream() -> Any:
        # SSE 注释行：建立连接、保活
        yield ": connected\n\n"
        status = str(rec.get("status", ""))
        # 仅当内存总线已无该 run 的任何事件（后端重启后必然为空）才合成终态事件，
        # 让前端立即收尾；同进程内已完成的 run 仍走正常回放（能重看完整过程）。
        if status != "running" and not progress_store.has_events(run_id):
            terminal: dict[str, Any]
            if status in ("failed", "interrupted"):
                terminal = {
                    "type": "run_error",
                    "run_id": run_id,
                    "error": str(rec.get("error") or f"运行未完成（{status}），结果不完整"),
                    "timestamp": str(rec.get("finished_at") or ""),
                }
            else:
                terminal = {"type": "run_done", "run_id": run_id, "record": rec}
            yield f"data: {json.dumps(terminal, ensure_ascii=False)}\n\n"
            return
        async for ev in progress_store.subscribe(run_id):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关闭反向代理缓冲，保证实时性
        },
    )


class RebuildRequest(BaseModel):
    """手工重建请求（可选参数）。"""

    kind: str | None = None  # 强制重建形态；缺省按样本能力特征自动选择


@router.post("/runs/{run_id}/rebuild", dependencies=[Depends(require_admin)])
async def rebuild_run(run_id: str, req: RebuildRequest | None = None) -> dict[str, Any]:
    """对已完成的 run 重建「功能等价的新 exe」（与流水线内自动重建同一实现）。

    返回 200 + ``ok`` 标记：``ok=false`` 时 ``error`` / ``log`` 携带失败原因与完整
    工具输出（编译错误等），便于前端直接展示诊断；仅 run 不存在返回 404。
    """
    from harness.modules.reverse_engineering.rebuild import record_rebuild, run_rebuild

    pipe = _pipeline()
    if pipe.get_run(run_id) is None:
        raise APIError("REV_ENG_RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)

    result = await asyncio.to_thread(
        run_rebuild, run_id, kind=(req.kind if req else None), runs_dir=pipe.runs_dir
    )
    if result.ok:
        # 写回 run.json：详情/前端可见，且重复调用幂等覆盖
        record_rebuild(pipe.runs_dir / run_id, result)
    return result.to_dict()


@router.get("/runs/{run_id}/evidence", dependencies=[Depends(require_admin)])
async def get_evidence(run_id: str) -> dict[str, Any]:
    data = _pipeline().get_run(run_id)
    if data is None:
        raise APIError("REV_ENG_RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    return {
        "run_id": run_id,
        "bundle_id": data.get("evidence_bundle_id"),
        "dir": data.get("evidence_dir"),
        "verify": data.get("evidence_report", {}),
    }
