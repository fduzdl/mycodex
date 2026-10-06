"""Readers for Codex CLI native session storage (~/.codex/sessions).

Codex persists every conversation as a rollout file:
    ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread_id>.jsonl

Each file is JSONL with three record shapes we care about:
    {"type": "session_meta", "payload": {"session_id", "cwd", "timestamp", ...}}
    {"type": "response_item", "payload": {"type": "message", "role", "content": [...]}}
    {"type": "turn_context", ...}  (also carries cwd)

These helpers power /session, /resume, /continue, /status and the
workspace picker — the "terminal and Feishu share one session" feature.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("mycodex.codex_sessions")


def codex_home() -> Path:
    """Codex data dir — $CODEX_HOME override, default ~/.codex."""
    configured = os.environ.get("CODEX_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def codex_auth_ready() -> bool:
    """True when a ChatGPT login (or API key) is present for codex."""
    return (codex_home() / "auth.json").is_file()


def is_codex_storage_dir(p: Path) -> bool:
    """判断路径是否为 Codex CLI 底层历史会话存储目录（如 ~/.codex 或 ~/.codex/sessions）。"""
    name = p.name.lower()
    if name == ".codex" or (name == "sessions" and p.parent.name.lower() == ".codex"):
        return True
    if (p / "sessions").is_dir() or (p / "auth.json").is_file():
        return True
    try:
        if any(p.glob("rollout-*.jsonl")) or any(p.glob("*/*/rollout-*.jsonl")):
            return True
    except Exception:
        pass
    return False


def _sessions_dir() -> Path:
    try:
        from config.settings import settings
        cfg_dir = settings.get_codex_session_dir()
        if cfg_dir:
            p = Path(cfg_dir).expanduser().resolve()
            if p.is_dir() and is_codex_storage_dir(p):
                if (p / "sessions").is_dir():
                    return p / "sessions"
                return p
    except Exception:
        pass
    return codex_home() / "sessions"


def _iter_rollout_files() -> list[Path]:
    root = _sessions_dir()
    if not root.is_dir():
        return []
    return sorted(root.rglob("rollout-*.jsonl"), key=lambda p: -p.stat().st_mtime)


def _thread_id_from_name(path: Path) -> str:
    # rollout-2026-01-12T11-35-57-019bb046-52fc-7782-b359-42ac48476722.jsonl
    # uuid is the trailing 36 chars (8-4-4-4-12 + 4 dashes)
    stem = path.stem
    return stem[-36:] if len(stem) > 36 else stem


def _read_session_meta(path: Path) -> dict:
    """Read only the first line (session_meta) of a rollout file."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if data.get("type") == "session_meta":
                    return data.get("payload") or {}
                break  # first record is session_meta; anything else = unexpected
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read session_meta %s: %s", path, e)
    return {}


def find_rollout_file(thread_id: str) -> Path | None:
    """Locate the rollout jsonl for a codex thread_id."""
    if not thread_id:
        return None
    for path in _iter_rollout_files():
        if _thread_id_from_name(path) == thread_id:
            return path
    return None


def _message_text(payload: dict) -> str:
    parts = []
    for block in payload.get("content") or []:
        if isinstance(block, dict):
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
    return " ".join(parts).strip()


def _is_noise_message(text: str) -> bool:
    """Skip injected context blocks that codex writes as user messages."""
    if not text:
        return True
    return text.startswith((
        "<environment_context>", "# AGENTS.md instructions",
        "<user_instructions>", "<turn_context>", "<ENVIRONMENT_CONTEXT>",
    ))


def iter_thread_messages(thread_id: str) -> list[dict]:
    """Return [{"role", "text"}] user/assistant messages for one thread.

    Codex-injected environment/instruction blocks are filtered out so the
    count matches what a human would consider conversation turns.
    """
    path = find_rollout_file(thread_id)
    if path is None:
        return []
    messages: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") != "response_item":
                    continue
                payload = data.get("payload") or {}
                if payload.get("type") != "message":
                    continue
                role = payload.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                text = _message_text(payload)
                if role == "user" and _is_noise_message(text):
                    continue
                if not text:
                    continue
                messages.append({"role": role, "text": text})
    except OSError as e:
        logger.warning("Failed to read thread %s: %s", thread_id, e)
    return messages


def list_workspace_threads(workspace: str) -> list[dict]:
    """All codex threads whose cwd == workspace, newest first.

    Returns [{"thread_id", "mtime", "last_summary", "message_count"}].
    """
    if not workspace:
        return []
    # os.path.normcase: Windows 下统一大小写与反斜杠；macOS/Linux 原样
    # （同机写入的路径分隔与大小写一致，直接可比）。
    ws_key = os.path.normcase(os.path.normpath(str(Path(workspace).resolve())))
    out: list[dict] = []
    for path in _iter_rollout_files():
        meta = _read_session_meta(path)
        cwd = str(meta.get("cwd") or "")
        if not cwd or os.path.normcase(os.path.normpath(cwd)) != ws_key:
            continue
        thread_id = meta.get("session_id") or _thread_id_from_name(path)
        messages = iter_thread_messages(thread_id)
        last_summary = ""
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                last_summary = msg["text"][:60]
                break
        if not last_summary and messages:
            last_summary = messages[-1]["text"][:60]
        out.append({
            "thread_id": thread_id,
            "mtime": path.stat().st_mtime,
            "last_summary": last_summary or "(无文本)",
            "message_count": len(messages),
        })
    out.sort(key=lambda x: -x["mtime"])
    return out


def latest_thread_for_workspace(workspace: str) -> tuple[str, str] | None:
    """Newest (thread_id, last_summary) for a workspace, or None."""
    threads = list_workspace_threads(workspace)
    if not threads:
        return None
    best = threads[0]
    return best["thread_id"], best["last_summary"]


def find_all_codex_workspaces() -> list[str]:
    """Every workspace dir that has at least one codex session, newest first."""
    workspaces: dict[str, float] = {}
    for path in _iter_rollout_files():
        meta = _read_session_meta(path)
        cwd = meta.get("cwd") or ""
        if not isinstance(cwd, str) or not cwd:
            continue
        p = Path(cwd)
        if not p.is_absolute() or not p.is_dir():
            continue
        resolved = str(p.resolve())
        workspaces[resolved] = max(workspaces.get(resolved, 0), path.stat().st_mtime)
    return sorted(workspaces, key=lambda w: (-workspaces[w], w.lower()))


def detect_cli_thread_update(workspace: str, current_thread_id: str | None) -> dict:
    """检测当前工作区在电脑端是否有更新的 Codex CLI 会话产生。

    若检测到本地最新会话与当前不同且修改时间晚于当前会话，返回 has_update=True 及会话信息。
    """
    res = {
        "has_update": False,
        "latest_thread_id": "",
        "latest_mtime": 0.0,
        "summary": "",
    }
    if not workspace:
        return res

    threads = list_workspace_threads(workspace)
    # 对齐目标必须有实质对话内容（至少一轮问答）：桌面端建而未用的空 rollout
    # mtime 最新，会赢过真实会话把用户绑到空线程上（与 myclaw born 会话防御同口径）。
    threads = [t for t in threads if t.get("message_count", 0) >= 2]
    if not threads:
        return res

    best = threads[0]
    best_tid = best["thread_id"]
    best_mtime = best["mtime"]
    best_summary = best["last_summary"]

    if not current_thread_id or current_thread_id == "__continue__":
        res.update({
            "has_update": True,
            "latest_thread_id": best_tid,
            "latest_mtime": best_mtime,
            "summary": best_summary,
        })
    elif best_tid != current_thread_id:
        cur_mtime = 0.0
        for t in threads:
            if t["thread_id"] == current_thread_id:
                cur_mtime = t["mtime"]
                break
        if best_mtime > cur_mtime + 2.0:
            res.update({
                "has_update": True,
                "latest_thread_id": best_tid,
                "latest_mtime": best_mtime,
                "summary": best_summary,
            })

    return res
