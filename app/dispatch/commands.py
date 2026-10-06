"""共享指令分发。

平台事件层（feishu/wecom 的 events）把消息解析成 UserTarget + 文本后
调用 handle_message；所有指令语义、会话/偏好变更、Codex 调度都在这里，
富视图通过 ReplyContext.view(kind) 交给平台渲染。
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import uuid
from pathlib import Path

from app.agent.cli_loop import codex_cli_loop, _kill_process_tree
from app.agent.codex_sessions import codex_auth_ready, detect_cli_thread_update
from app.audit.logger import audit_logger
from app.channel.base import UserTarget
from app.channel.registry import get_channel
from app.dispatch.context import ReplyContext
from app.dispatch.helpers import (
    _decode_output,
    _scan_workspace_files,
    _write_env_value,
    find_all_codex_projects,
    get_project_meta,
    iter_session_messages,
    list_workspace_sessions,
    predict_continue_session,
    session_scope_dir,
)
from app.dispatch.sessions import session_manager, skey_for
from app.models.schemas import Session, TaskStatus
from app.profiles import VALID_EFFORTS, discover_models
from app.state.preferences import preferences_manager
from config.settings import settings

logger = logging.getLogger("mycodex.dispatch")


async def _send_view_after_callback(reply: ReplyContext, kind: str, **payload) -> None:
    """Let the platform callback acknowledgement flush before sending a new view."""
    await asyncio.sleep(0.2)
    await reply.view(kind, **payload)


async def _send_text_after_callback(reply: ReplyContext, content: str) -> None:
    await asyncio.sleep(0.2)
    await reply.text(content)


def _workspace_selection_payload(session: Session | None = None) -> dict:
    if session and session.workspace:
        current = session.workspace
    else:
        current = settings.default_workspace.strip() or "尚未设置"
    return {"current": current, "projects": find_all_codex_projects()}


async def _prompt_next_setup_step(target: UserTarget, prefix_ack: str = "") -> bool:
    """按顺序弹出下一个尚未配置的初始化步骤（每次只发一张卡片/编号列表）。

    顺序：[1/3] Model -> [2/3] Effort -> [3/3] Mode
    若全部已配置则返回 False。
    """
    preferences = preferences_manager.get(target.user_id)
    models = discover_models()
    reply = ReplyContext(target)

    if preferences.model not in models:
        await reply.view(
            "model_selection",
            approval_id=uuid.uuid4().hex[:12],
            models=models,
            current_model="",
            step_title="[1/3] 选择模型 (Model)",
            prefix_ack=prefix_ack,
        )
        return True

    if preferences.level not in VALID_EFFORTS:
        await reply.view(
            "effort_selection",
            approval_id=uuid.uuid4().hex[:12],
            current_effort="",
            step_title="[2/3] 选择推理强度 (Effort)",
            prefix_ack=prefix_ack,
        )
        return True

    if preferences.mode not in ("h", "m", "l"):
        await reply.view(
            "mode_selection",
            approval_id=uuid.uuid4().hex[:12],
            active_mode="",
            step_title="[3/3] 选择执行模式 (Mode)",
            prefix_ack=prefix_ack,
        )
        return True

    return False


async def _check_and_run_pending(target: UserTarget, prev_ack: str = "") -> bool:
    """Check if all initial setup items (Model, Effort, Mode) are complete.
    If still missing items and a setup/reset session is active, pop up the next step.
    If complete, send summary message and trigger _run_codex if needed.
    """
    preferences = preferences_manager.get(target.user_id)
    models = discover_models()

    has_model = preferences.model in models
    has_level = preferences.level in VALID_EFFORTS
    has_mode = preferences.mode in ("h", "m", "l")

    session = session_manager.get_user_session(skey_for(target))

    if not (has_model and has_level and has_mode):
        if session and session.pending_prompt.strip():
            await _prompt_next_setup_step(target, prefix_ack=prev_ack)
        return False

    if session:
        preferences_manager.save_workspace_config(session.workspace, preferences)

    if session and session.pending_prompt.strip():
        pending = session.pending_prompt.strip()
        session.pending_prompt = ""
        session_manager.save_session(session)

        model_label = models.get(preferences.model, {}).get("label", preferences.model)
        mode_labels = {"h": "🛡️ 严格模式 (h)", "m": "⚖️ 平衡模式 (m)", "l": "⚡ 全自动模式 (l)"}
        mode_lbl = mode_labels.get(preferences.mode, preferences.mode)

        reply = ReplyContext(target)
        if pending == "__reset_only__":
            msg_lines = [
                "🎉 初始配置已全部就绪！",
                "",
                "📋 当前运行设置：",
                f"• 模型 (Model)：`{model_label}`",
                f"• 推理强度 (Effort)：`{preferences.level}`",
                f"• 执行模式 (Mode)：`{mode_lbl}`",
                "",
                "💡 现在您可以直接发送消息开始对话。"
            ]
            await reply.text("\n".join(msg_lines))
            return True

        prompt_preview = pending if len(pending) <= 30 else pending[:27] + "..."
        msg_lines = [
            "🎉 初始配置已全部就绪！",
            "",
            "📋 当前运行设置：",
            f"• 模型 (Model)：`{model_label}`",
            f"• 推理强度 (Effort)：`{preferences.level}`",
            f"• 执行模式 (Mode)：`{mode_lbl}`",
            "",
            f"正在全自动为您执行暂存的任务：`{prompt_preview}` ..."
        ]
        await reply.text("\n".join(msg_lines))
        asyncio.get_running_loop().create_task(_run_codex(pending, target, session))
        return True
    return False


# ===== Core dispatch =====


async def handle_message(target: UserTarget, message_id: str, text: str) -> None:
    """入口：一条用户文本消息（已剥 @、已判群/私）。"""
    open_id = target.user_id
    skey = skey_for(target)
    reply = ReplyContext(target)
    channel = get_channel(target.platform)
    if not await channel.is_allowed(target):
        # 个人用自动绑定：个人模式下应用可用范围=仅创建者（自动化已配置并校验），
        # 首个私聊进来的用户即创建者本人——绑定后本条消息直接放行
        auto_bound = False
        if (
            target.platform != "wecom"
            and not target.is_group
            and settings.feishu_allowlist_pending.strip() == "1"
        ):
            from app.feishu.personal_bind import bind_from_message

            auto_bound = await bind_from_message(open_id)
            if auto_bound:
                logger.info("个人用白名单自动绑定生效，当前消息继续正常处理：%s", open_id)
            else:
                await reply.text("⏳ 个人用白名单初始化中：系统正在自动完成配置，请稍后重试。")
                return
        if not auto_bound:
            mode = settings.get_allowed_mode()
            if mode == "groups":
                err_msg = (
                    f"🚫 **权限拦截提醒**\n"
                    f"• 您的用户 ID: `{open_id or '(空)'}`\n"
                    f"• 当前访问模式: `groups`（仅指定群成员可用）\n\n"
                    f"💡 **解决建议**：请先加入被授权的群聊；管理员可在 `.env` 的 "
                    f"`ALLOWED_GROUP_IDS` 中调整群列表。"
                )
            else:
                is_wecom = (target.platform == "wecom")
                key_name = "WECOM_ALLOWED_USERS" if is_wecom else "ALLOWED_USERS"
                allowed_list = settings.get_allowed_users_for(target.platform)
                platform_name = "企业微信" if is_wecom else "飞书"
                err_msg = (
                    f"🚫 **权限拦截提醒**\n"
                    f"• 您的用户 ID: `{open_id or '(空)'}`\n"
                    f"• 当前允许的{platform_name}用户列表: `{', '.join(allowed_list) if allowed_list else '(空，未配置白名单)'}`\n\n"
                    f"💡 **解决建议**：请在 `.env` 中把您的用户 ID 加入 `{key_name}`；"
                    f"如需对所有{platform_name}用户开放，请把 `.env` 中的 `{key_name}` 设为空。"
                )
            await reply.text(err_msg)
            return

    text = text.strip()
    text_lower = text.lower()

    if not codex_auth_ready():
        await reply.text(
            "⚠️ 本机尚未检测到 Codex 登录态（`~/.codex/auth.json` 不存在）。\n"
            "请先在终端运行 `codex login` 完成 ChatGPT 账号登录，再回来发消息。",
        )
        return

    # --- /stop: interrupt current task ---
    if text_lower in ("/stop", "停止"):
        cancelled = await codex_cli_loop.cancel_and_wait(skey)
        if cancelled:
            await reply.text("已中断会话。")
        else:
            await reply.text("没有正在运行的任务。")
        return

    # --- /reset: reset all setup preferences and start sequential setup ---
    if text_lower in ("/reset", "重置"):
        await codex_cli_loop.cancel_and_wait(skey)
        preferences_manager.clear(open_id)
        session = session_manager.get_user_session(skey)
        if not session:
            session = session_manager.create_session(skey, target.chat_id)
        if not session.pending_prompt.strip():
            session.pending_prompt = "__reset_only__"
            session_manager.save_session(session)

        await _prompt_next_setup_step(
            target,
            prefix_ack="🔄 已重置初始运行配置，请按顺序完成 3 项设置（选完一项自动进入下一项）：",
        )
        return

    # --- /status ---
    if text_lower == "/status":
        try:
            session = session_manager.get_user_session(skey)
            preferences = preferences_manager.get(open_id)
            if session:
                models = discover_models()
                ctx_info = ""
                if session.context_tokens > 0:
                    pct = session.context_tokens / session.context_limit * 100
                    ctx_info = (
                        f"上下文用量: `{session.context_tokens:,}` / `{session.context_limit:,}` ({pct:.0f}%)\n"
                    )
                    if pct >= settings.context_critical_percent:
                        ctx_info += "⚠️ 上下文即将耗尽，建议使用 `/new` 开始新会话\n"
                    elif pct >= settings.context_warn_percent:
                        ctx_info += "⚠️ 上下文用量较高，请注意\n"

                ctid = session.codex_thread_id or "未启动"
                msg_count = 0
                if session.codex_thread_id and session.codex_thread_id != "__continue__":
                    msg_count = len(iter_session_messages(session.codex_thread_id))
                elif session.codex_thread_id == "__continue__":
                    ctid = "全自动恢复该项目最新 Thread"

                await reply.text(
                    f"📁 工作区: `{session.workspace}`\n"
                    f"🔑 Codex Thread: `{ctid}`\n"
                    f"🤖 模型: `{models.get(preferences.model, {}).get('label', preferences.model or '未选择')}`\n"
                    f"⚡ 推理强度: `{preferences.level or '未选择'}`\n"
                    f"🛡️ 执行模式: `{preferences.mode or '未选择'}`\n"
                    f"💬 底层推演步数: {msg_count} 步 (含思考/工具调用记录)\n"
                    f"{ctx_info}"
                    f"状态: {session.status.value}",
                )
            else:
                await reply.text("没有活跃会话。用 /new 创建新会话。")
        except Exception as err:
            logger.exception("Error handling /status for user %s", open_id)
            await reply.text(f"状态获取失败：{err}")
        return

    # --- /help ---
    if text_lower == "/help":
        await reply.view("help")
        return

    # --- /model [slug]: choose codex model ---
    if text_lower == "/model" or text_lower.startswith("/model "):
        parts = text.split(None, 1)
        models = discover_models()
        preferences = preferences_manager.get(open_id)

        if len(parts) == 2:
            arg = parts[1].strip()
            if arg in models:
                preferences.model = arg
                preferences_manager.save(open_id, preferences)
                codex_cli_loop.cancel_by_user(skey)
                await reply.text(
                    f"模型已切换为 `{models[arg].get('label', arg)}`，下一条消息生效。",
                )
                return
            await reply.text(
                f"未知模型: `{arg}`\n可用: {'、'.join(models.keys())}",
            )
            return

        await reply.view(
            "model_selection",
            approval_id=uuid.uuid4().hex[:12],
            models=models,
            current_model=preferences.model,
        )
        return

    # --- /effort [low|medium|high|xhigh|max]: choose reasoning effort ---
    if text_lower == "/effort" or text_lower.startswith("/effort "):
        parts = text.split(None, 1)
        preferences = preferences_manager.get(open_id)

        if len(parts) == 2 and parts[1].strip().lower() in VALID_EFFORTS:
            preferences.level = parts[1].strip().lower()
            preferences_manager.save(open_id, preferences)
            codex_cli_loop.cancel_by_user(skey)
            await reply.text(f"推理强度已切换为 `{preferences.level}`。")
            return

        await reply.view(
            "effort_selection",
            approval_id=uuid.uuid4().hex[:12],
            current_effort=preferences.level,
        )
        return

    # --- /mode [h|m|l]: choose execution mode ---
    if text_lower == "/mode" or text_lower.startswith("/mode "):
        parts = text.split(None, 1)
        preferences = preferences_manager.get(open_id)
        if len(parts) == 2 and parts[1].strip().lower() in ("h", "m", "l"):
            old_mode = preferences.mode
            new_mode = parts[1].strip().lower()
            preferences.mode = new_mode
            preferences_manager.save(open_id, preferences)
            mode_labels = {"h": "🛡️ 严格模式", "m": "⚖️ 平衡模式", "l": "⚡ 全自动模式"}
            mode_name = mode_labels.get(new_mode, new_mode)
            if old_mode != new_mode:
                await codex_cli_loop.cancel_and_wait(skey)
                await reply.text(
                    f"执行模式已切换为 {mode_name} (`{new_mode}`)，下一条消息生效。",
                )
            else:
                await reply.text(f"执行模式仍为 {mode_name} (`{new_mode}`)。")
            await _check_and_run_pending(target)
            return

        await reply.view(
            "mode_selection",
            approval_id=uuid.uuid4().hex[:12],
            active_mode=preferences.mode,
        )
        return

    # --- /pwd: print current workspace path ---
    if text_lower == "/pwd" or text_lower.startswith("/pwd "):
        session = session_manager.get_user_session(skey)
        ws = session.workspace if session else settings.get_default_workspace()
        await reply.text(f"📁 当前工作目录：`{ws}`")
        return

    # --- /file: select and send workspace files ---
    if text_lower == "/file" or text_lower.startswith("/file "):
        session = session_manager.get_user_session(skey)
        ws = session.workspace if session else settings.get_default_workspace()
        files = _scan_workspace_files(ws)
        await reply.view("file_selection", workspace=ws, files=files)
        return

    # --- /cd <path>: switch workspace ---
    if text_lower.startswith("/cd"):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(skey)

        if len(parts) < 2 or not parts[1].strip():
            await reply.view("workspace_selection", **_workspace_selection_payload(session))
            return

        new_path = parts[1].strip()
        p = Path(new_path)
        if not p.is_absolute():
            # 2026-10-06：示例文案曾是 Windows 风格，把 mac 用户带偏（漏开头 / 还照抄了反斜杠）。
            # 现按平台给示例，并对"疑似漏掉开头 /"的 mac 输入给出针对性提示。
            if sys.platform != "win32" and not new_path.startswith("/"):
                await reply.text(
                    f"❌ `{new_path}` 是相对路径。macOS/Linux 绝对路径以 `/` 开头，"
                    f"你是不是想切换到 `/{new_path}`？确认请发送 `/cd /{new_path}`"
                )
                return
            example = "/cd /Users/mac/projects/myapp" if sys.platform != "win32" else "/cd D:\\projects\\myapp"
            await reply.text(f"错误：请使用绝对路径，例如 `{example}`")
            return

        if not p.exists():
            parent = p.parent
            if not parent.exists():
                await reply.text(f"❌ 切换失败：父目录 `{parent}` 在磁盘上不存在！")
                return
            target_path = str(p.resolve())
            meta = get_project_meta(target_path)
            await reply.view(
                "cd_confirm",
                target_path=target_path,
                is_new=True,
                git_branch=meta["git_branch"],
                agents_md=meta["agents_md"],
                warning_running=codex_cli_loop.is_running(skey),
            )
            return

        if not session:
            session = session_manager.create_session(skey, target.chat_id, workspace=str(p.resolve()))
        else:
            session.workspace = str(p.resolve())
            session.workspace_selected = True
            session.codex_thread_id = "__continue__"
            session_manager.save_session(session)

        resolved_workspace = str(p.resolve())
        codex_cli_loop.cancel_by_user(skey)

        pred = predict_continue_session(session.workspace)
        if pred["can_continue"]:
            pred_text = f"\n🔄 **预计关联会话：** 可继续恢复 (`{pred['thread_id']}`)\n💬 **上次对话：** {pred['last_summary']}"
        else:
            pred_text = "\n🆕 **预计关联会话：** 纯净项目 (发送首条消息时自动创建新 Thread)"

        pref = preferences_manager.get(open_id)
        models = discover_models()
        model_label = models.get(pref.model, {}).get("label", pref.model) or pref.model or "未设置"
        mode_labels = {"h": "🛡️ 严格模式 (h)", "m": "⚖️ 平衡模式 (m)", "l": "⚡ 全自动模式 (l)"}
        mode_label = mode_labels.get(pref.mode, pref.mode or "未设置")

        await reply.text(
            f"📁 工作区已切换：`{session.workspace}`{pred_text}\n"
            f"⚙️ 当前配置：模型 `{model_label}` | 推理强度 `{pref.level or '未设置'}` | 审批模式 `{mode_label}`"
        )
        return

    # --- /session [thread_id]: list codex threads in current workspace ---
    if text_lower == "/session" or text_lower.startswith("/session "):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(skey)
        if not session:
            session = session_manager.create_session(skey, target.chat_id)
        ws = session.workspace or settings.get_default_workspace()

        # /session <id>: 直接走 /resume 同款路径
        if len(parts) == 2 and parts[1].strip():
            from app.dispatch import selections

            try:
                msg = selections.handle_session_resume(target, skey, parts[1].strip())
            except selections.SelectionError as e:
                msg = e.message
            await reply.text(msg)
            return

        # /session (无参): 弹卡片选择
        sessions = list_workspace_sessions(ws)
        cur = session.codex_thread_id or ""
        if cur == "__continue__":
            cur = ""
        await reply.view(
            "session_selection", workspace=ws, sessions=sessions, current_thread_id=cur,
        )
        return

    # --- /resume <thread_id>: switch to one exact native thread ---
    if text_lower.startswith("/resume"):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(skey)
        if len(parts) < 2 or not parts[1].strip():
            cur = session.codex_thread_id if session else ""
            await reply.text(
                f"当前 Codex Thread: `{cur or '无'}`\n\n用法: `/resume <thread_id>`",
            )
            return
        if not session:
            session = session_manager.create_session(skey, target.chat_id)
        await codex_cli_loop.cancel_and_wait(skey)
        session.codex_thread_id = parts[1].strip()
        session.context_tokens = 0
        # 固定会话目录（CODEX_SESSION_DIR）下恢复的会话属于那个项目，需同步切换工作区
        scope_ws = session_scope_dir(session.workspace)
        if settings.get_codex_session_dir() and scope_ws and scope_ws != session.workspace:
            session.workspace = scope_ws
        session_manager.save_session(session)
        pref = preferences_manager.get(open_id)
        models = discover_models()
        model_label = models.get(pref.model, {}).get("label", pref.model) or pref.model or "未设置"
        mode_labels = {"h": "🛡️ 严格模式 (h)", "m": "⚖️ 平衡模式 (m)", "l": "⚡ 全自动模式 (l)"}
        mode_label = mode_labels.get(pref.mode, pref.mode or "未设置")

        await reply.text(
            f"🔑 已切换到 Codex Thread `{session.codex_thread_id}`。\n"
            f"⚙️ 当前配置：模型 `{model_label}` | 推理强度 `{pref.level or '未设置'}` | 审批模式 `{mode_label}`"
        )
        return

    # --- /continue [prompt]: resume most recent thread ---
    if text_lower.startswith("/continue"):
        parts = text.split(None, 1)
        session = session_manager.get_user_session(skey)
        if not session:
            session = session_manager.create_session(skey, target.chat_id)

        arg = parts[1].strip() if len(parts) > 1 else ""
        prompt = arg if arg else "继续上次的任务"

        # Force __continue__ even if mycodex has no recorded thread: codex
        # resolves the newest thread in the workspace at spawn time.
        if not session.codex_thread_id:
            session.codex_thread_id = "__continue__"
            session_manager.save_session(session)
            preferences_manager.clear(open_id)
        # 配置了固定会话目录时，/continue 固定恢复该目录下最近的会话
        if settings.get_codex_session_dir():
            scope_ws = session_scope_dir(session.workspace)
            if scope_ws and scope_ws != session.workspace:
                session.workspace = scope_ws
                session_manager.save_session(session)
        await _run_codex(prompt, target, session, skip_classify=True)
        return

    # --- /new: reset native thread and runtime preferences ---
    if text_lower == "/new" or text_lower.startswith("/new ") or text_lower.startswith("/new\n"):
        new_task = "" if text_lower == "/new" else text[len("/new"):].strip()
        await codex_cli_loop.cancel_and_wait(skey)
        session = session_manager.reset_user_session(skey)

        # /new <任务描述>：重置后直接把剩余文字作为新会话首条任务执行
        if new_task:
            preview = new_task if len(new_task) <= 60 else new_task[:57] + "..."
            await reply.text(f"✨ **新会话已就绪**，开始执行：`{preview}`")
            await _run_codex(new_task, target, session)
            return

        pref = preferences_manager.get(open_id)
        models = discover_models()
        model_label = models.get(pref.model, {}).get("label", pref.model) or pref.model or "未设置"
        mode_labels = {"h": "🛡️ 严格模式 (h)", "m": "⚖️ 平衡模式 (m)", "l": "⚡ 全自动模式 (l)"}
        mode_label = mode_labels.get(pref.mode, pref.mode or "未设置")

        await reply.text(
            f"✨ **新会话已就绪**\n"
            f"📁 工作区：`{session.workspace}`\n"
            f"⚙️ 当前配置：模型 `{model_label}` | 推理强度 `{pref.level or '未设置'}` | 审批模式 `{mode_label}`\n\n"
            f"💡 全局配置已生效，直接发送消息即可开始对话。"
        )
        return

    # --- /clean: remove stale state files of the caller's own context ---
    if text_lower == "/clean":
        session = session_manager.get_user_session(skey)
        if session:
            session_manager.clean_old_sessions(skey)
            await reply.text(f"已清理旧会话文件，当前会话: `{session.session_id}`")
        else:
            await reply.text("没有活跃会话。")
        return

    # --- /compact: not supported by codex ---
    if text_lower == "/compact":
        session = session_manager.get_user_session(skey)
        if not session or not session.codex_thread_id:
            await reply.text(
                "没有活跃的 Codex 会话。\n"
                "先发一条消息启动会话；上下文不足时用 `/new` 开新会话。",
            )
            return
        await reply.text(
            "ℹ️ Codex 暂不支持上下文压缩。建议发送 `/new` 开启新会话"
            "（旧会话仍可用 `/resume <thread_id>` 找回）。",
        )
        return

    # --- /mem: workspace memo in AGENTS.md ---
    # `/mem` → show; `/mem <text>` → append; `/mem clear` → wipe
    if text_lower == "/mem" or text_lower.startswith("/mem "):
        arg = text[4:].strip()
        session = session_manager.get_user_session(skey)
        workspace = session.workspace if session else settings.get_default_workspace()
        memory_md = Path(workspace) / "AGENTS.md"

        if not arg:
            try:
                if memory_md.exists():
                    body = memory_md.read_text("utf-8")
                else:
                    await reply.text(
                        f"📝 当前工作区 `{workspace}` 还没有 AGENTS.md。\n"
                        f"用法：`/mem <内容>` 追加；`/mem clear` 清空。",
                    )
                    return
                char_count = len(body)
                PREVIEW_LIMIT = 4000
                if char_count <= PREVIEW_LIMIT:
                    preview = body
                    trunc_note = ""
                else:
                    preview = body[:PREVIEW_LIMIT]
                    trunc_note = f"\n\n…（共 {char_count} 字符，仅显示前 {PREVIEW_LIMIT}）"
                await reply.text(
                    f"📝 `{workspace}` 的 AGENTS.md（{char_count} 字符）：\n```\n{preview}```{trunc_note}",
                )
            except Exception as e:
                await reply.text(f"读取失败: {e}")
            return

        if arg.lower() == "clear":
            try:
                if memory_md.exists():
                    memory_md.unlink()
                    await reply.text(f"已清空 `{workspace}` 下的 AGENTS.md。")
                else:
                    await reply.text(f"`{workspace}` 下没有 AGENTS.md，无需清空。")
            except Exception as e:
                await reply.text(f"清空失败: {e}")
            return

        content = arg
        try:
            if memory_md.exists():
                existing = memory_md.read_text("utf-8").rstrip("\n")
                memory_md.write_text(existing + "\n" + content + "\n", "utf-8")
            else:
                memory_md.write_text(content + "\n", "utf-8")
            await reply.text(f"已记录到 `{workspace}` 下的 AGENTS.md")
        except Exception as e:
            await reply.text(f"写入失败: {e}")
        return

    # --- /notes: write output or text to notes.md ---
    if (
        text_lower == "/notes"
        or text_lower.startswith("/notes ")
        or text_lower.startswith("/notes:")
        or text_lower.startswith("/notes：")
    ):
        if text_lower.startswith("/notes:") or text_lower.startswith("/notes："):
            raw_arg = text[7:].strip()
        else:
            raw_arg = text[6:].strip()

        clean_arg = raw_arg.lstrip(":：").strip()
        if clean_arg.lower().startswith("task:") or clean_arg.lower().startswith("task_id:"):
            clean_arg = clean_arg.split(":", 1)[1].strip()

        if not clean_arg:
            await reply.text(
                "📝 用法：\n"
                "• `/notes last` — 追加上一次任务执行完成的最终输出到 `notes.md`\n"
                "• `/notes <task_id>` — 追加特定 task_id 的执行完成卡片内容到 `notes.md`\n"
                "• `/notes <内容>` — 将自定义文本追加写入 `notes.md`",
            )
            return

        session = session_manager.get_user_session(skey)
        workspace = session.workspace if session else settings.get_default_workspace()
        notes_md = Path(workspace) / "notes.md"

        content_to_write = ""
        source_desc = ""

        if clean_arg.lower() == "last":
            last_res = codex_cli_loop.get_last_result(target.user_id)
            if not last_res or not last_res.text:
                await reply.text("⚠️ 未找到上一次执行完成的结果记录。")
                return
            content_to_write = last_res.text
            source_desc = f"上一次任务 (`{last_res.task_id}`)"
        else:
            task_res = codex_cli_loop.get_task_result(target.user_id, clean_arg)
            if task_res and task_res.text:
                content_to_write = task_res.text
                source_desc = f"任务 (`{task_res.task_id}`)"
            elif re.match(r"^[a-fA-F0-9]{12}$", clean_arg):
                await reply.text(
                    f"⚠️ 未找到任务 ID 为 `{clean_arg}` 的历史执行记录。\n"
                    f"💡 提示：服务重启或重置会清空内存记录；您可使用 `/notes last` 追加最近一次任务结果。",
                )
                return
            else:
                content_to_write = raw_arg
                source_desc = "自定义笔记内容"

        try:
            notes_md.parent.mkdir(parents=True, exist_ok=True)
            if notes_md.exists():
                existing = notes_md.read_text("utf-8")
                if existing and not existing.endswith("\n"):
                    existing += "\n"
                new_content = existing + content_to_write + "\n"
            else:
                new_content = content_to_write + "\n"

            notes_md.write_text(new_content, "utf-8")

            preview = content_to_write[:200] + ("..." if len(content_to_write) > 200 else "")
            await reply.text(
                f"✅ 已将{source_desc}追加写入到工作区下的 `notes.md`：\n\n"
                f"📁 文件：`{notes_md}`\n"
                f"📝 内容预览：\n```\n{preview}\n```",
            )
        except Exception as e:
            logger.exception("Failed to write to notes.md")
            await reply.text(f"❌ 写入 `notes.md` 失败：{e}")
        return

    # --- /sh <command>: execute shell command in workspace ---
    if text_lower.startswith("/sh "):
        cmd = text[4:].strip()
        if not cmd:
            await reply.text("用法: `/sh <command>`，例如 `/sh mkdir ZhiWang`")
            return
        session = session_manager.get_user_session(skey)
        workspace = session.workspace if session else settings.get_default_workspace()
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
                start_new_session=(sys.platform != "win32"),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            out = _decode_output(stdout).strip()
            err = _decode_output(stderr).strip()
        except asyncio.TimeoutError:
            _kill_process_tree(proc)
            await reply.text(f"⏱️ 命令超时(30s): `{cmd}`")
            return
        except Exception as e:
            await reply.text(f"执行失败: `{e}`")
            return
        parts = [f"📁 `{workspace}`", f"▶ `{cmd}`"]
        if out:
            display = out if len(out) <= 4000 else out[:3950] + "\n... (输出已截断)"
            parts.append(f"```\n{display}\n```")
        if err:
            display = err if len(err) <= 1000 else err[:950] + "\n..."
            parts.append(f"⚠️ stderr:\n```\n{display}\n```")
        parts.append(f"退出码: {proc.returncode}")
        await reply.text("\n".join(parts))
        return

    # --- Default: run Codex CLI ---
    await _run_codex(text, target)


# 命令层已实现的指令名（供无效斜杠输入的提示区分“用法错误”与“未识别”）。
# 与上方各 `text_lower ==` / `startswith` 分支保持同步。
_KNOWN_COMMANDS = frozenset({
    "/help", "/status", "/stop", "/new", "/clean", "/compact", "/continue",
    "/notes", "/model", "/mode", "/effort", "/pwd", "/file",
    "/cd", "/session", "/resume", "/mem", "/sh",
})


async def _run_codex(
    prompt: str,
    target: UserTarget,
    session: Session | None = None,
    skip_classify: bool = False,
    resume_thread_id: str | None = None,
) -> None:
    """Run Codex CLI with optional setup cards for first message.

    resume_thread_id: if set, start codex with ``resume <id>`` to
    continue a specific thread (used by /resume <id>). When None, the
    codex_thread_id on the session drives thread continuity.
    """
    skey = skey_for(target)
    open_id = target.user_id
    chat_id = target.chat_id if target.is_group else ""
    audit_logger.log_command_received(open_id, prompt, "started")
    reply = ReplyContext(target)

    # 拦截“以 / 开头但未被命令层处理”的输入：原样转发可能被 CLI 当斜杠命令吞掉，
    # 0 次模型调用静默返回 "(no content)"（2026-10-06 myclaw 实测，两项目行为对齐）。
    # 内部合成调用（skip_classify=True）不受此拦截影响。
    if not skip_classify:
        stripped_prompt = prompt.lstrip()
        if stripped_prompt.startswith("/"):
            parts = stripped_prompt.split()
            head = parts[0] if parts else "/"
            if head.lower() in _KNOWN_COMMANDS:
                await reply.text(
                    f"⚠️ 指令 `{head}` 不支持这种用法（多余的参数不会被识别）。\n"
                    f"发送 `/help` 查看该指令的正确用法。"
                )
            else:
                await reply.text(
                    f"⚠️ 未识别的指令 `{head}`，已拦截未执行（原样转发只会得到空结果）。\n"
                    f"• 发送 `/help` 查看全部可用指令\n"
                    f"• 如需发送以 / 开头的普通内容（如路径），请在消息最前面加一个空格"
                )
            return
    try:
        if not session:
            session = session_manager.get_user_session(skey)
        if not session:
            session = session_manager.create_session(skey, chat_id)

        # Workspace heal: a previously /cd'ed directory may have been deleted
        # or renamed since — fall back to the default workspace (and reset the
        # thread, which belongs to the old workspace) instead of hard-failing.
        # Skipped when no valid default is configured (the gate below asks).
        if not Path(session.workspace).is_dir():
            default_ws = settings.get_default_workspace()
            if settings.default_workspace.strip() and Path(default_ws).is_dir():
                old_workspace = session.workspace
                session.workspace = default_ws
                session.workspace_selected = False
                session.codex_thread_id = ""
                session_manager.save_session(session)
                await reply.text(
                    f"⚠️ 之前的工作区 `{old_workspace}` 已不存在，"
                    f"已自动切回默认工作区 `{session.workspace}` 并继续执行任务。"
                )

        # First-run workspace gate: DEFAULT_WORKSPACE is not configured and
        # this user hasn't picked a workspace yet — ask via the selection
        # view instead of silently running tasks in an arbitrary directory.
        if not session.workspace_selected and not settings.default_workspace.strip():
            session.pending_prompt = prompt
            session_manager.save_session(session)
            await reply.view("workspace_selection", **_workspace_selection_payload(session))
            await reply.text(
                "💡 尚未设置工作区。请在上方卡片选择一个项目目录"
                "（或发送 `/cd <绝对路径>`），选定后任务将自动开始执行。"
            )
            return

        preferences = preferences_manager.get(open_id)
        models = discover_models()

        need_model = preferences.model not in models
        need_effort = preferences.level not in VALID_EFFORTS
        need_mode = preferences.mode not in ("h", "m", "l")

        if need_model or need_effort or need_mode:
            session.pending_prompt = prompt
            session_manager.save_session(session)
            prompt_preview = prompt if len(prompt) <= 30 else prompt[:27] + "..."
            await _prompt_next_setup_step(
                target,
                prefix_ack=f"💡 任务已安全暂存：`{prompt_preview}`\n请按顺序完成初始配置，全部就绪后将自动为您执行任务：",
            )
            return

        # 自动感知并对齐电脑端最新 CLI 会话（电脑端创建/更新会话向飞书端同步）
        if not resume_thread_id and session.workspace:
            try:
                update_info = detect_cli_thread_update(session.workspace, session.codex_thread_id)
                if update_info.get("has_update"):
                    old_tid = session.codex_thread_id
                    new_tid = update_info["latest_thread_id"]
                    logger.info("Detected newer Codex CLI thread %s (old: %s) for user %s", new_tid, old_tid, open_id)
                    if codex_cli_loop.is_running(skey):
                        await codex_cli_loop.cancel_and_wait(skey)
                    session.codex_thread_id = new_tid
                    session_manager.save_session(session)
                    summary_hint = f"（最新电脑端对话：`{update_info['summary'][:30]}`）" if update_info.get("summary") else ""
                    await reply.text(
                        f"ℹ️ 感知到电脑端本地更新了会话，已自动为您对齐最新会话上下文{summary_hint}。"
                    )
            except Exception as e:
                logger.warning("Failed to check CLI thread update: %s", e)

        agent_result = await codex_cli_loop.send_and_wait(
            prompt=prompt,
            target=target,
            workspace=session.workspace,
            model=preferences.model,
            effort=preferences.level,
            approval_mode=preferences.mode,
            codex_thread_id=session.codex_thread_id or None,
            resume_thread_id=resume_thread_id,
        )
        # Auto-heal: if the run failed because the recorded thread id no
        # longer exists on disk, fall back to a fresh thread and retry once.
        err_msg = (agent_result.error or "") + (agent_result.text or "")
        err_lower = err_msg.lower()
        if (
            session.codex_thread_id
            and session.codex_thread_id != "__continue__"
            and agent_result.status != "completed"
            and any(k in err_lower for k in (
                "no session", "not found", "no such", "does not exist",
                "failed to resume", "invalid session",
            ))
        ):
            logger.warning("Thread ID invalid/lost for user %s, auto-healing...", open_id)
            session.codex_thread_id = ""
            session_manager.save_session(session)
            await reply.text(
                "ℹ️ 检测到历史会话 ID 已在磁盘上失效，已全自动为您重新生成会话并执行任务...",
            )
            await _run_codex(prompt, target, session, skip_classify=True)
            return

        # Process was killed (switch/stop/new) — caller already notified user
        if agent_result.status == "cancelled":
            return

        # Spawn-time failure: no thread was ever created, so the progress card
        # (and its error display) never came into being — tell the user here
        # instead of failing silently.
        if agent_result.status == "failed" and not agent_result.thread_id and agent_result.error:
            await reply.text(
                f"❌ 任务启动失败：\n```\n{agent_result.error[:500]}\n```",
            )

        # Preserve the last known-good thread on success. /new and workspace
        # changes are the explicit reset operations.
        if agent_result.thread_id:
            session.codex_thread_id = agent_result.thread_id
        if agent_result.input_tokens > 0:
            session.context_tokens = agent_result.input_tokens

        session_manager.save_session(session)

        try:
            session.status = TaskStatus(agent_result.status)
        except ValueError:
            session.status = TaskStatus.COMPLETED

        audit_logger.log_command_received(open_id, prompt, agent_result.status)

    except Exception as e:
        logger.exception("Dispatch error for user %s", open_id)
        try:
            await reply.text(f"处理指令时出错：{type(e).__name__}: {e}")
        except Exception:
            pass
