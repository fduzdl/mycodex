"""选择卡 / 审批卡的共享语义处理。

平台事件层解析出动作参数后调用这里的语义处理器。处理器本身是同步的
（沿用原实现：同步校验 + create_task 发起异步后续），返回 toast 文案
供平台渲染轻提示，校验失败抛 SelectionError（平台转为错误提示）。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from app.agent.cli_loop import codex_cli_loop
from app.approval.manager import approval_manager
from app.channel.base import UserTarget
from app.channel.registry import get_channel
from app.dispatch.commands import (
    _check_and_run_pending,
    _run_codex,
    _workspace_selection_payload,
)
from app.dispatch.context import ReplyContext
from app.dispatch.helpers import get_project_meta, predict_continue_session, session_scope_dir
from app.dispatch.sessions import session_manager, skey_for
from app.profiles import VALID_EFFORTS, discover_models
from app.state.preferences import preferences_manager
from config.settings import settings

logger = logging.getLogger("mycodex.selections")


class SelectionError(Exception):
    """校验失败：平台层转为错误 toast / 错误消息。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def handle_workspace_pre(target: UserTarget, skey: str, act: str, target_path: str) -> None:
    """工作区切换第一阶段：校验路径并拉起确认卡片。"""
    target_path = target_path.strip()
    if not target_path:
        raise SelectionError("未检测到路径。您可以直接发送指令：/cd <绝对路径>")

    p = Path(target_path)
    if not p.is_absolute():
        raise SelectionError("错误：请输入绝对路径")

    is_new = False
    if not p.exists():
        if act == "pre_create":
            parent = p.parent
            if not parent.exists():
                raise SelectionError(f"校验失败：父目录 `{parent}` 不存在！")
            is_new = True
        else:
            raise SelectionError(f"错误：路径不存在: `{target_path}`")

    meta = get_project_meta(target_path)
    warning_running = codex_cli_loop.is_running(skey)

    async def _followup() -> None:
        reply = ReplyContext(target)
        await asyncio.sleep(0.2)
        await reply.view(
            "cd_confirm",
            target_path=str(p.resolve()),
            is_new=is_new,
            git_branch=meta["git_branch"],
            agents_md=meta["agents_md"],
            warning_running=warning_running,
        )

    asyncio.get_running_loop().create_task(_followup())


def handle_workspace_confirm(target: UserTarget, skey: str, target_path: str) -> None:
    """工作区切换第二阶段：真正切换。"""
    target_path = target_path.strip()
    if not target_path:
        raise SelectionError("路径信息丢失，请重新选择")

    p = Path(target_path)
    is_new = False
    if not p.exists():
        parent = p.parent
        if not parent.exists():
            raise SelectionError(f"创建失败：父目录 `{parent}` 不存在！")
        try:
            p.mkdir(parents=False, exist_ok=True)
            is_new = True
        except Exception as e:
            raise SelectionError(f"无法创建目录: {e}")

    session = session_manager.get_user_session(skey)
    if not session:
        session = session_manager.create_session(skey, target.chat_id, workspace=str(p.resolve()))
    else:
        session.workspace = str(p.resolve())
        session.workspace_selected = True
        session.codex_thread_id = "__continue__"  # 切换工作区后自动恢复该项目最新 Thread
        session_manager.save_session(session)

    resolved_workspace = str(p.resolve())
    # 工作区严格属于当前会话（群聊/私聊各自独立），不再改写全局默认值
    codex_cli_loop.cancel_by_user(skey)

    pred = predict_continue_session(session.workspace)
    if pred["can_continue"]:
        pred_text = f"\n🔄 **预计关联会话：** 可继续恢复 (`{pred['thread_id']}`)\n💬 **上次对话：** {pred['last_summary']}"
    else:
        pred_text = "\n🆕 **预计关联会话：** 纯净项目 (发送首条消息时自动创建新 Thread)"

    action_msg = "已在新目录新建并切换" if is_new else "已切换"

    pref = preferences_manager.get(target.user_id)
    models = discover_models()
    model_label = models.get(pref.model, {}).get("label", pref.model) or pref.model or "未设置"
    mode_labels = {"h": "🛡️ 严格模式 (h)", "m": "⚖️ 平衡模式 (m)", "l": "⚡ 全自动模式 (l)"}
    mode_label = mode_labels.get(pref.mode, pref.mode or "未设置")

    async def _followup() -> None:
        reply = ReplyContext(target)
        await asyncio.sleep(0.2)
        await reply.text(
            f"📁 {action_msg}工作区：`{session.workspace}`{pred_text}\n"
            f"⚙️ 当前配置：模型 `{model_label}` | 推理强度 `{pref.level or '未设置'}` | 审批模式 `{mode_label}`"
        )
        if session and session.pending_prompt.strip():
            pending = session.pending_prompt.strip()
            session.pending_prompt = ""
            session_manager.save_session(session)
            await _run_codex(pending, target, session)

    asyncio.get_running_loop().create_task(_followup())


def handle_workspace_cancel(target: UserTarget) -> None:
    """取消工作区切换，回到第一阶段选择视图。"""

    async def _followup() -> None:
        reply = ReplyContext(target)
        await asyncio.sleep(0.2)
        await reply.view("workspace_selection", **_workspace_selection_payload())

    asyncio.get_running_loop().create_task(_followup())


def handle_file_send(target: UserTarget, path_str: str) -> str:
    """发送工作区文件给用户。返回成功 toast 文案。"""
    target_path = path_str.strip()
    if not target_path:
        raise SelectionError("错误：未选择任何文件")

    p = Path(target_path)
    if not p.exists() or not p.is_file():
        raise SelectionError(f"文件不存在：{p.name}")

    channel = get_channel(target.platform)
    max_mb = getattr(channel, "max_file_mb", 30)
    if p.stat().st_size > max_mb * 1024 * 1024:
        raise SelectionError(
            f"文件过大 ({p.stat().st_size / (1024 * 1024):.1f}MB)，上限 {max_mb}MB"
        )

    reply = ReplyContext(target)

    async def _upload_and_send_task() -> None:
        try:
            await reply.text(f"⏳ 正在上传并发送文件：`{p.name}` ...")
            await reply.file(p)
        except Exception as e:
            logger.exception("Failed to send file to %s: %s", target.user_id, e)
            await reply.text(f"❌ 发送文件失败：{e}")

    asyncio.get_running_loop().create_task(_upload_and_send_task())
    return f"已开始发送文件：{p.name}"


def handle_mode_switch(target: UserTarget, mode: str) -> str:
    if mode not in ("h", "m", "l"):
        mode = "m"
    preferences = preferences_manager.get(target.user_id)
    old_mode = preferences.mode
    preferences.mode = mode
    preferences_manager.save(target.user_id, preferences)

    mode_labels = {"h": "严格模式 (h)", "m": "平衡模式 (m)", "l": "全自动模式 (l)"}
    lbl = mode_labels.get(mode, mode)

    async def _apply() -> None:
        if old_mode and old_mode != mode:
            await codex_cli_loop.cancel_and_wait(skey_for(target))
        await _check_and_run_pending(target, prev_ack=f"✅ 已选择执行模式：`{lbl}`")

    asyncio.get_running_loop().create_task(_apply())
    return f"已设置执行模式: {lbl}"


def handle_effort_switch(target: UserTarget, effort: str) -> str:
    if effort not in VALID_EFFORTS:
        effort = "medium"
    preferences = preferences_manager.get(target.user_id)
    preferences.level = effort
    preferences_manager.save(target.user_id, preferences)
    codex_cli_loop.cancel_by_user(skey_for(target))
    asyncio.get_running_loop().create_task(
        _check_and_run_pending(target, prev_ack=f"✅ 已选择推理强度：`{effort}`")
    )
    return f"已切换推理强度: {effort}"


def handle_model_switch(target: UserTarget, skey: str, model: str) -> str:
    models = discover_models()
    label = models.get(model, {}).get("label", model)
    if model not in models:
        return f"切换失败: 未找到 {label} 模型"

    preferences = preferences_manager.get(target.user_id)
    preferences.model = model
    preferences_manager.save(target.user_id, preferences)
    codex_cli_loop.cancel_by_user(skey)
    asyncio.get_running_loop().create_task(
        _check_and_run_pending(target, prev_ack=f"✅ 已选择模型：`{label}`")
    )
    return f"模型已切换为 {label}"


def handle_session_resume(target: UserTarget, skey: str, thread_id: str) -> str:
    thread_id = thread_id.strip()
    if not thread_id:
        raise SelectionError("未选择会话")

    session = session_manager.get_user_session(skey)
    if not session:
        session = session_manager.create_session(skey, target.chat_id)
    session.codex_thread_id = thread_id
    session.context_tokens = 0
    scope_ws = session_scope_dir(session.workspace)
    if settings.get_codex_session_dir() and scope_ws and scope_ws != session.workspace:
        session.workspace = scope_ws
    session_manager.save_session(session)
    codex_cli_loop.cancel_by_user(skey)
    asyncio.get_running_loop().create_task(codex_cli_loop.cancel_and_wait(skey))
    return f"已切换到会话 {thread_id}（下一条消息将 resume）"


def handle_reuse_confirm(target: UserTarget, skey: str, reuse: bool) -> str:
    session = session_manager.get_user_session(skey)
    pending = session.pending_prompt.strip() if session else ""
    if session:
        session.pending_reuse_confirm = False
        session_manager.save_session(session)
    if not reuse:
        # User opted to re-pick: wipe preferences so next _run_codex shows setup views.
        preferences_manager.clear(target.user_id)
    # Re-trigger the queued prompt with the (preserved or cleared) prefs.
    if pending and session:
        session.pending_prompt = ""
        session_manager.save_session(session)
        asyncio.get_running_loop().create_task(_run_codex(pending, target, session))
    return "沿用上次设置" if reuse else "已清空，重新选择"


def handle_ws_config_reuse(target: UserTarget, skey: str, reuse: bool) -> str:
    session = session_manager.get_user_session(skey)
    workspace = session.workspace if session else settings.get_default_workspace()

    if reuse:
        ws_config = preferences_manager.load_workspace_config(workspace)
        if ws_config and ws_config.complete:
            preferences_manager.save(target.user_id, ws_config)
            asyncio.get_running_loop().create_task(_check_and_run_pending(target))
            return "✅ 已成功沿用工作区配置！"

    # Wiped or user chose reset
    preferences_manager.clear(target.user_id)
    if session:
        asyncio.get_running_loop().create_task(
            _run_codex(session.pending_prompt or "", target, session)
        )
    return "已重置偏好，请重新选择配置"


def decide_approval(target: UserTarget, approval_id: str, approved: bool) -> str:
    """审批卡 允许/拒绝 决策。返回给用户的确认文案。"""
    asyncio.get_running_loop().create_task(
        approval_manager.handle_decision(approval_id, target.user_id, approved)
    )
    return "已允许" if approved else "已拒绝"
