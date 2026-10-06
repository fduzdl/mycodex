#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MyCodex 跨平台交互配置向导。
负责：工作空间确认、环境自检（Node.js / Codex CLI）、ChatGPT 授权与全功能配置中心（飞书/企微/白名单）。
"""

import os
import sys
import json
import time
import shutil
import subprocess
from pathlib import Path

# 确保 Windows 下控制台 Unicode 安全输出
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        if sys.stdin and hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parent.parent
# 以 python scripts/setup_wizard.py 方式启动时 sys.path[0] 是 scripts/ 目录，
# `from scripts.import_feishu_group import ...` 需要项目根目录在搜索路径上
sys.path.insert(0, str(ROOT_DIR))

RESERVED_COMMANDS = {
    "reset", "help", "stop", "clear", "cd", "session", "mode", "model", "effort", "status", "diff"
}


def validate_bot_name(name: str) -> tuple[bool, str]:
    """校验机器人名称是否合法（严禁空格、特殊符号与系统指令）。"""
    if not name or not name.strip():
        return False, "名称不能为空"
    trimmed = name.strip()
    if any(c.isspace() for c in name) or "\u3000" in name:
        return False, "名称严禁包含任何空格或换行（会导致群聊 @ 识别失效）"
    if len(trimmed) < 2:
        return False, "名称过短，至少需要 2 个字符"
    if len(trimmed) > 20:
        return False, "名称过长，建议在 20 个字符以内（避免移动端群聊 @ 被截断）"
    if "@" in trimmed or "＠" in trimmed:
        return False, "名称不能包含「@」符号"
    if "/" in trimmed or "\\" in trimmed:
        return False, "名称不能包含斜杠「/」或反斜杠「\\」（与系统指令冲突）"
    import re
    if re.search(r"[:;,'\"`|<>&\uff01-\uff0f\uff1a-\uff20]", trimmed):
        return False, "名称不能包含特殊标点符号（如冒号、逗号、引号、尖括号等）"
    if trimmed.lower() in RESERVED_COMMANDS:
        return False, f"「{trimmed}」为系统内置保留指令，不能作为机器人名称"
    if not re.match(r"^[\w\u4e00-\u9fa5-]+$", trimmed):
        return False, "名称仅支持中文、英文字母、数字、下划线「_」或短横线「-」"
    return True, ""


def run_cmd(cmd: list[str], cwd: Path | None = None, check: bool = False, env: dict | None = None) -> int:
    """运行子命令并实时透传标准输入输出。"""
    c_env = os.environ.copy()
    if env:
        c_env.update(env)
    # 在 Windows 下如果命令是 npm 或 npx，需开启 shell=True 或解析为 npm.cmd
    use_shell = sys.platform == "win32" and cmd[0] in ("npm", "npx")
    res = subprocess.run(cmd, cwd=cwd or ROOT_DIR, env=c_env, shell=use_shell)
    if check and res.returncode != 0:
        sys.exit(res.returncode)
    return res.returncode


def get_env_value(key: str) -> str:
    """读取 .env 中的指定配置项。"""
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return ""
    try:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip("'\"")
    except Exception:
        pass
    return ""


def upsert_env_key(key: str, value: str):
    """更新或插入 .env 文件中的配置项。"""
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        example_file = ROOT_DIR / "config" / "examples" / "env.example"
        if example_file.exists():
            shutil.copy(example_file, env_file)
        else:
            env_file.write_text(f"{key}={value}\n", encoding="utf-8")
            return

    try:
        lines = env_file.read_text(encoding="utf-8", errors="replace").splitlines()
        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("#") and "=" in stripped:
                k, _ = stripped.split("=", 1)
                if k.strip() == key:
                    new_lines.append(f"{key}={value}")
                    found = True
                    continue
            new_lines.append(line)

        if not found:
            new_lines.append(f"{key}={value}")

        env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[!] 写入 .env 失败: {e}")


def get_recommended_workspace() -> Path:
    """获取默认初始运行目录：默认为项目根目录下的 workspace 独立沙盒目录。"""
    ws = ROOT_DIR / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws.resolve()


def infer_codex_session_dir() -> Path:
    """自动推断电脑端 Codex CLI 的历史会话存储目录。"""
    candidates: list[Path] = []
    env_home = os.environ.get("CODEX_HOME", "").strip()
    if env_home:
        home_path = Path(env_home).expanduser()
        candidates.extend([home_path / "sessions", home_path])

    home_codex = Path.home() / ".codex"
    candidates.extend([home_codex / "sessions", home_codex])

    for cand in candidates:
        try:
            if cand.exists() and cand.is_dir():
                return cand.resolve()
        except Exception:
            continue

    return (home_codex / "sessions").resolve()


def confirm_directories():
    """【Step 2/4】初始运行目录与 Codex 历史会话目录确认。"""
    from config.settings import cross_platform_path

    print("\n" + "=" * 60)
    print("   【Step 2/4】初始运行目录与 Codex 历史会话目录确认")
    print("=" * 60)
    print()
    print(f"  MyCodex 程序运行目录 (只读): {ROOT_DIR.resolve()}")
    print()

    # 确保 .env 基础文件存在
    env_file = ROOT_DIR / ".env"
    example_file = ROOT_DIR / "config" / "examples" / "env.example"
    if not env_file.exists():
        if example_file.exists():
            shutil.copy(example_file, env_file)
            print("[OK] 已初始化生成 .env 配置文件。\n")
        else:
            env_file.touch()

    def ask_path(label: str, recommended: Path, hint: str = "") -> Path:
        print(f"  {label}")
        print(f"      当前推荐: {recommended}")
        if hint:
            print(f"      >> {hint}")
        choice = input("[?] 是否直接采用？[Y/N] (直接回车 = 采用): ").strip().lower()
        final = recommended
        if choice == "n":
            while True:
                custom = input("请输入目录绝对路径: ").strip().strip("'\"")
                if not custom:
                    print("[!] 路径不能为空，请重新输入。")
                    continue
                if cross_platform_path(custom):
                    print("[!] 这是其他平台的路径格式，请输入本机路径。")
                    continue
                try:
                    final = Path(custom).expanduser().resolve()
                    break
                except Exception as e:
                    print(f"[!] 路径格式无效 ({e})，请重新输入。")
        print()
        return final

    # 1. 初始运行目录：初始任务临时在此运行，后续可用/cd 命令切换至目标目录
    configured = get_env_value("DEFAULT_WORKSPACE")
    if configured and cross_platform_path(configured):
        print(f"[!] .env 中的 DEFAULT_WORKSPACE（{configured}）是其他平台的路径，已忽略并改用本机推荐值。")
        configured = ""
    rec_ws = Path(configured).expanduser() if configured else get_recommended_workspace()
    final_ws = ask_path(
        "[1] 初始运行目录：初始任务临时在此运行，后续可用/cd 命令切换至目标目录",
        rec_ws,
    )
    try:
        final_ws.mkdir(parents=True, exist_ok=True)
        print(f"[OK] 初始运行目录已就绪: {final_ws}")
    except Exception as e:
        print(f"[!] 创建目录遇到问题 ({e})，但已记录该路径。")
    upsert_env_key("DEFAULT_WORKSPACE", str(final_ws))

    # 2. Codex 历史会话目录：读取电脑端的历史会话
    sess_configured = get_env_value("CODEX_SESSION_DIR")
    if sess_configured and cross_platform_path(sess_configured):
        print(f"[!] .env 中的 CODEX_SESSION_DIR（{sess_configured}）是其他平台的路径，已忽略并改用本机推荐值。")
        sess_configured = ""
    rec_sess = Path(sess_configured).expanduser() if sess_configured else infer_codex_session_dir()
    final_sess = ask_path(
        "[2] Codex 历史会话目录：读取电脑端的历史会话",
        rec_sess,
    )
    try:
        final_sess.mkdir(parents=True, exist_ok=True)
        print(f"[OK] 历史会话目录已就绪: {final_sess}")
    except Exception as e:
        print(f"[!] 创建目录遇到问题 ({e})，但已记录该路径。")
    upsert_env_key("CODEX_SESSION_DIR", str(final_sess))


def check_environment():
    """【Step 1/4】基础运行环境检测 (Environment)。"""
    print("\n" + "=" * 60)
    print("          【Step 1/4】运行环境自检与依赖 (Environment)")
    print("=" * 60)
    print()

    # 1. 检查 Node.js
    node_path = shutil.which("node")
    if not node_path:
        print("[!] 警告: 未检测到 Node.js 环境（auto_feishu 需要 Node.js v20+）。")
        print("    建议前往 https://nodejs.org 下载安装 Node.js LTS 版本。")
    else:
        try:
            ver = subprocess.check_output(["node", "-v"], text=True).strip()
            print(f"[OK] Node.js 运行时环境: {ver}")
        except Exception:
            print("[OK] 检测到 Node.js 运行时。")

    # 2. 检查 Codex CLI
    codex_path = shutil.which("codex")
    if not codex_path:
        print("[!] 提示: 未检测到全局 Codex CLI。")
        choice = input("[?] 是否立即通过国内镜像全局安装 Codex CLI？[Y/N] (默认 N): ").strip().lower()
        if choice == "y":
            print("[*] 正在安装 @openai/codex ...")
            run_cmd(["npm", "install", "-g", "@openai/codex", "--registry=https://registry.npmmirror.com"])
    else:
        print("[OK] Codex CLI 已就绪。")


def configure_initial_preferences():
    """交互式配置初始运行偏好：Model / Effort / Mode，并写入 config/global_preferences.json。"""
    from app.profiles import discover_models
    from app.state.preferences import UserPreferences, preferences_manager

    current = preferences_manager.get_global()
    models = discover_models()
    model_keys = list(models.keys()) if models else ["gpt-6-luna", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
    default_model = current.model if current.model in model_keys else model_keys[0]

    print("\n" + "-" * 60)
    print("  【初始运行偏好配置】(Model / Effort / Mode)")
    print("-" * 60)

    # 1. Model（必须显式选择，直接回车不生效）
    print(f"\n  [1/3] 选择默认模型 (Model) [当前生效: {default_model}]:")
    for idx, k in enumerate(model_keys, 1):
        m_info = models.get(k)
        label = (m_info.get("label", k) if isinstance(m_info, dict) else getattr(m_info, "label", k)) if m_info else k
        print(f"    {idx}. {k} - {label}")
    chosen_model = ""
    while True:
        raw_m = input("  请选择序号或模型名称: ").strip()
        if raw_m.isdigit() and 1 <= int(raw_m) <= len(model_keys):
            chosen_model = model_keys[int(raw_m) - 1]
            break
        if raw_m:
            chosen_model = raw_m
            break
        print("[!] 无效输入，请输入列表中的序号或模型名称。")

    # 2. Effort (low / medium / high / xhigh / max)
    effort_options = [
        ("medium", "Medium（默认平衡）"),
        ("high", "High（深度思考）"),
        ("xhigh", "XHigh（超强推理）"),
        ("max", "Max（极限思考）"),
        ("low", "Low（快速响应）"),
    ]
    valid_efforts = tuple(k for k, _ in effort_options)
    default_effort = current.level if current.level in valid_efforts else "medium"
    print(f"\n  [2/3] 选择默认思考力度 (Effort) [当前默认: {default_effort}]:")
    for idx, (val, desc) in enumerate(effort_options, 1):
        mark = " (默认)" if val == default_effort else ""
        print(f"    {idx}. {val} - {desc}{mark}")
    raw_e = input(f"  请选择 [1-5] 或名称 (直接回车 = {default_effort}): ").strip().lower()
    chosen_effort = default_effort
    if raw_e:
        if raw_e.isdigit() and 1 <= int(raw_e) <= len(effort_options):
            chosen_effort = effort_options[int(raw_e) - 1][0]
        elif raw_e in valid_efforts:
            chosen_effort = raw_e

    # 3. Mode (m / h / l)
    mode_options = [
        ("m", "平衡模式 (m) - 工作区读写放行，高风险命令需审批（推荐）"),
        ("h", "严格模式 (h) - 严格安全全审批，所有工具调用均需人工确认"),
        ("l", "全自动模式 (l) - 自动放行全部工具调用，全自动运行无需人工确认"),
    ]
    default_mode = current.mode if current.mode in ("m", "h", "l") else "m"
    print(f"\n  [3/3] 选择默认权限审批模式 (Mode) [当前默认: {default_mode}]:")
    for idx, (val, desc) in enumerate(mode_options, 1):
        mark = " (默认)" if val == default_mode else ""
        print(f"    {idx}. {desc}{mark}")
    raw_mode = input(f"  请选择 [1-3] 或 m/h/l (直接回车 = {default_mode}): ").strip().lower()
    chosen_mode = default_mode
    if raw_mode:
        if raw_mode.isdigit() and 1 <= int(raw_mode) <= len(mode_options):
            chosen_mode = mode_options[int(raw_mode) - 1][0]
        elif raw_mode in ("m", "h", "l"):
            chosen_mode = raw_mode

    prefs = UserPreferences(
        model=chosen_model,
        level=chosen_effort,
        mode=chosen_mode,
    )
    preferences_manager.save_global(prefs)
    print(
        f"\n[OK] 初始运行配置已保存: Model={chosen_model} "
        f"| Effort={chosen_effort} | Mode={chosen_mode}"
    )


def check_or_setup_models():
    """【Step 3/4】Codex 认证状态与初始偏好配置 (Auth / Model / Effort / Mode)。"""
    print("\n" + "=" * 60)
    print("  【Step 3/4】Codex 账号认证与初始运行配置 (Model/Effort/Mode)")
    print("=" * 60)
    print()

    auth_file = Path.home() / ".codex" / "auth.json"
    has_auth = auth_file.exists() and auth_file.stat().st_size > 10

    if has_auth:
        print("[OK] Codex ChatGPT 登录态已就绪 (~/.codex/auth.json)")
        c = input("[?] 是否需要重新登录或切换 ChatGPT 账号？[y/N] (直接回车 = 保持当前): ").strip().lower()
        if c == "y":
            run_cmd(["codex", "login"])
    else:
        print("[!] 提示: 未检测到 Codex ChatGPT 登录态。")
        print("    MyCodex 运行依赖本机 ChatGPT 登录授权（无需填 API Key）。")
        c = input("[?] 是否立即在终端执行 'codex login' 进行登录？[Y/N] (直接回车 = 是): ").strip().lower()
        if c in ("", "y"):
            run_cmd(["codex", "login"])

    configure_initial_preferences()


def ensure_playwright_chromium(auto_feishu_dir: Path) -> bool:
    """快速检测 Playwright Chromium 驱动，已就绪则秒级跳过，未就绪则镜像加速安装。"""
    # 1. 尝试检测 Chromium 可执行文件是否已存在
    check_script = "const { chromium } = require('playwright'); const p = chromium.executablePath(); process.exit(require('fs').existsSync(p) ? 0 : 1);"
    try:
        res = subprocess.run(["node", "-e", check_script], cwd=auto_feishu_dir, capture_output=True, text=True)
        if res.returncode == 0:
            print("[OK] Playwright Chromium 浏览器驱动已就绪。")
            return True
    except Exception:
        pass

    # 2. 若未就绪，清理潜在的残留死锁目录 __dirlock
    try:
        cache_dirs = []
        if sys.platform == "darwin":
            cache_dirs.append(Path.home() / "Library" / "Caches" / "ms-playwright")
        elif sys.platform == "win32":
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                cache_dirs.append(Path(local_appdata) / "ms-playwright")
        else:
            cache_dirs.append(Path.home() / ".cache" / "ms-playwright")

        for cd in cache_dirs:
            dirlock = cd / "__dirlock"
            if dirlock.exists():
                shutil.rmtree(dirlock, ignore_errors=True)
    except Exception:
        pass

    # 3. 注入国内镜像加速执行安装
    print("[*] 正在安装 Playwright Chromium 组件（已启用国内镜像加速）...")
    install_env = {
        "PLAYWRIGHT_DOWNLOAD_HOST": os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST", "https://npmmirror.com/mirrors/playwright")
    }
    code = run_cmd(["npx", "playwright", "install", "chromium"], cwd=auto_feishu_dir, env=install_env)
    return code == 0


def fetch_app_creator_open_id(app_id: str, app_secret: str) -> str:
    """用应用自身凭据查创建者 open_id（application/v6/applications 的 creator_id）。

    返回的 open_id 与消息事件里发送者的 open_id 同为该应用作用域，可直接进白名单。
    """
    import urllib.request

    def call(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> dict:
        import urllib.error

        h = {"Content-Type": "application/json; charset=utf-8"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body else None,
            headers=h,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # 飞书的 4xx 带 JSON 错误体，读出来才有 code/msg 可判
            try:
                return json.loads(e.read())
            except Exception:
                raise

    last_info: dict = {}
    for attempt in range(2):
        tok = call(
            "POST",
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret},
        ).get("tenant_access_token", "")
        # 该接口必须带 lang 参数，否则直接 400
        info = call(
            "GET",
            f"https://open.feishu.cn/open-apis/application/v6/applications/{app_id}?lang=zh_cn",
            headers={"Authorization": "Bearer " + tok},
        )
        last_info = info
        if info.get("code") == 0:
            return (info.get("data", {}).get("app", {}) or {}).get("creator_id", "")
        if info.get("code") == 99991672 and attempt < 1:
            print(f"[*] 等待飞书应用权限(application:application:self_manage)同步生效 ({attempt + 1}/2)...")
            time.sleep(2)
            continue
        break

    raise RuntimeError(f"code={last_info.get('code')} {last_info.get('msg')}")


def apply_personal_allowlist():
    """个人用模式收尾：把应用创建者的 open_id 写入 ALLOWED_USERS（名单里明确只放本人）。"""
    app_id = get_env_value("FEISHU_APP_ID")
    app_secret = get_env_value("FEISHU_APP_SECRET")
    if not app_id or not app_secret or app_id.startswith("cli_x"):
        print("[!] .env 中缺少有效的飞书凭据，未改动 ALLOWED_USERS。")
        return

    # 优先用自动化会话内解析好的创建者（浏览器关闭前已带重试验证权限生效）
    creator = ""
    try:
        result_file = ROOT_DIR / "auto_feishu" / "feishu-app-result.json"
        data = json.loads(result_file.read_text(encoding="utf-8"))
        if data.get("appId") == app_id:
            creator = data.get("creatorOpenId") or ""
    except Exception:
        creator = ""
    if creator:
        print(f"[OK] 已从自动化结果读取应用创建者 {creator}。")
    else:
        try:
            creator = fetch_app_creator_open_id(app_id, app_secret)
        except Exception as e:
            from scripts.import_feishu_group import upsert_env as _upsert
            _upsert("FEISHU_ALLOWLIST_PENDING", "1")
            print(f"[!] 创建者解析暂未成功（{e}）。")
            print("[OK] 已进入自动绑定模式：重启服务后系统会自动等待飞书权限生效，")
            print("     自动解析创建者并写入 ALLOWED_USERS，完成后机器人会主动通知，无需人工操作。")
            return
    if not creator:
        from scripts.import_feishu_group import upsert_env as _upsert
        _upsert("FEISHU_ALLOWLIST_PENDING", "1")
        print("[!] 未获取到应用创建者 open_id，已进入自动绑定模式（重启服务后自动补齐并主动通知）。")
        return

    from scripts.import_feishu_group import upsert_env
    upsert_env("ALLOWED_USERS", creator)
    upsert_env("FEISHU_ALLOWLIST_PENDING", "")
    print(f"[OK] 个人用模式：ALLOWED_USERS 已写入应用创建者 {creator}（名单里明确仅本人可用）。")
    print("     若服务正在运行，请重启服务使配置生效。")


def offer_restart(reason: str):
    """凭据/白名单变更收尾：询问是否立即重启服务使 .env 生效。"""
    print(f"\n[*] {reason}，重启服务后生效。")
    try:
        ans = input("[?] 是否立即重启服务？[Y/N] (默认 Y): ").strip().lower()
    except EOFError:
        ans = "n"
    if ans not in ("", "y", "yes"):
        print("[OK] 已跳过重启；稍后可运行 launcher 目录里的 Restart 脚本。")
        return
    if sys.platform == "win32":
        bats = sorted((ROOT_DIR / "launcher_windows").glob("*-Restart.bat"))
        if bats:
            # 新窗口执行：Restart.bat 为 GBK 输出且自带 pause，在向导的 UTF-8
            # 控制台里会乱码并卡住向导；独立窗口代码页正确且不打断向导流程
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "Restart Service", str(bats[0])],
                cwd=str(ROOT_DIR),
            )
            print(f"[OK] 已在新窗口执行重启（{bats[0].name}），进度在该窗口显示，完成后按任意键关闭即可。")
            return
    else:
        sh = ROOT_DIR / "scripts" / "restart_mac.sh"
        if sh.exists():
            run_cmd(["bash", str(sh)], cwd=ROOT_DIR)
            return
    print("[!] 未找到重启脚本，请手动重启服务。")


def setup_feishu(mode: str):
    """直接使用 npm 直驱执行 auto_feishu 自动化，无中间层，杜绝二次询问与参数丢失。"""
    auto_feishu_dir = ROOT_DIR / "auto_feishu"
    if not auto_feishu_dir.exists():
        print("[ERROR] 未找到 auto_feishu 自动化目录。")
        return

    mode_label = "个人用" if mode == "personal" else "公用"
    print(f"\n[*] 准备飞书自动化配置环境（已选定: {mode_label} 模式）...")
    print("    [提示] 机器人自命名严禁包含空格（否则群聊 @ 识别失效），建议使用 2-20 位中文、英文或数字。")

    # 1. 确保 auto_feishu npm 依赖
    node_modules = auto_feishu_dir / "node_modules"
    playwright_pkg = node_modules / "playwright"
    if not node_modules.exists() or not playwright_pkg.exists():
        print("[*] 依赖缺失或未完全安装，正在安装依赖 (npm install)...")
        run_cmd(["npm", "install", "--no-audit", "--no-fund"], cwd=auto_feishu_dir)

    # 2. 确保 Playwright Chromium 浏览器组件（已就绪秒级跳过，未就绪镜像加速）
    ensure_playwright_chromium(auto_feishu_dir)

    # 3. 按选定模式直驱飞书配置脚本
    print(f"[*] 正在执行飞书自动化配置（{mode_label}）...")
    custom_env = {"FEISHU_DEPLOY_MODE": mode}
    npm_cmd = ["npm", "run", "feishu:setup"]
    if mode == "personal":
        npm_cmd.extend(["--", "--personal"])

    code = run_cmd(npm_cmd, cwd=auto_feishu_dir, env=custom_env)
    if code == 0:
        print(f"[OK] 飞书{mode_label}模式自动化配置完成。")
        if mode == "personal":
            apply_personal_allowlist()
            print("[*] 新建/换用应用后，飞书发送权限对 OpenAPI 生效可能延迟几分钟到几十分钟；")
            print("    期间发消息可能暂时无回复，就绪后会自动送达，无需任何操作。")
        else:
            # 公用=全员可用：清掉可能残留的个人用白名单，避免拦掉其他用户
            from scripts.import_feishu_group import upsert_env as _upsert
            if get_env_value("ALLOWED_USERS"):
                _upsert("ALLOWED_USERS", "")
                _upsert("FEISHU_ALLOWLIST_PENDING", "")
                print("[OK] 公用模式：已清空残留的个人用白名单（全员可用）。")
        offer_restart("飞书配置已写入 .env")
    else:
        print(f"[!] 飞书自动化配置退出，返回码: {code}")


def apply_wecom_personal_allowlist():
    """企微个人用模式收尾：把从后台 getAIRobotDetail 提取的创建者 userid 写入 WECOM_ALLOWED_USERS。"""
    creator = ""
    try:
        result_file = ROOT_DIR / "auto_wecom" / "wecom-bot-result.json"
        if result_file.exists():
            data = json.loads(result_file.read_text(encoding="utf-8"))
            creator = (data.get("creatorUserId") or "").strip()
    except Exception:
        creator = ""

    if creator:
        upsert_env_key("WECOM_ALLOWED_USERS", creator)
        upsert_env_key("WECOM_ALLOWED_CHATS", "")
        print(f"[OK] 个人用模式：已从企微后台自动识别创建者 userid ({creator}) 并写入 WECOM_ALLOWED_USERS（仅本人可用）。")
        return

    existing = get_env_value("WECOM_ALLOWED_USERS")
    try:
        ans = input(f"[?] 未能自动提取创建者 ID，请输入你的企业微信 userid 限定仅本人可用{f' [当前: {existing}]' if existing else ''}: ").strip()
    except EOFError:
        ans = ""
    final_uid = ans or existing
    if final_uid:
        upsert_env_key("WECOM_ALLOWED_USERS", final_uid)
        upsert_env_key("WECOM_ALLOWED_CHATS", "")
        print(f"[OK] 个人用模式：WECOM_ALLOWED_USERS 已写入 {final_uid}。")


def ask_wecom_group_import():
    """企业微信公用模式下的特定群白名单授权交互（方案 A）。"""
    print("\n" + "=" * 46)
    print("        企业微信公用模式 - 白名单配置")
    print("=" * 46)
    print(">> 默认模式：WECOM_ALLOWED_USERS 与 WECOM_ALLOWED_CHATS 保持为空，企业内全员均可访问。")
    print()
    choice = input("[?] 是否需要将特定企业微信群授权为白名单（仅该群成员可用）？[Y/N] (默认 N): ").strip().lower()
    if choice == "y":
        run_cmd([sys.executable, str(ROOT_DIR / "scripts" / "import_wecom_group.py")])
    else:
        upsert_env_key("WECOM_ALLOWED_USERS", "")
        upsert_env_key("WECOM_ALLOWED_CHATS", "")
        print("[OK] 已将企业微信设为全员开放模式（WECOM_ALLOWED_USERS / WECOM_ALLOWED_CHATS 已清空）。")


def setup_wecom_auto(mode: str = "personal"):
    """企业微信自动化配置：auto_wecom 浏览器自动化（扫码登录→复用/创建机器人→设置使用方式→读凭据）+ WS 长连接实测。"""
    auto_dir = ROOT_DIR / "auto_wecom"
    if not auto_dir.exists():
        print("[ERROR] 未找到 auto_wecom 自动化目录。")
        return

    mode_label = "个人用" if mode == "personal" else "公用"
    print(f"\n[*] 准备企业微信自动化配置环境（已选定: {mode_label} 模式）...")
    print("    [提示] 机器人自命名严禁包含空格（否则群聊 @ 识别失效），建议使用 2-20 位中文、英文或数字。")

    node_modules = auto_dir / "node_modules"
    if not (node_modules / "playwright").exists():
        print("[*] 依赖缺失，正在安装依赖 (npm install)...")
        run_cmd(["npm", "install", "--no-audit", "--no-fund"], cwd=auto_dir)
    ensure_playwright_chromium(auto_dir)

    print(f"[*] 正在执行企业微信自动化配置（{mode_label}，会打开浏览器，需用企业微信 App 扫码登录管理后台）...")
    custom_env = {"WECOM_DEPLOY_MODE": mode}
    npm_cmd = ["npm", "run", "wecom:setup", "--", f"--{mode}"]
    code = run_cmd(npm_cmd, cwd=auto_dir, env=custom_env)
    if code != 0:
        print(f"[!] 企业微信自动化配置退出，返回码: {code}")
        return

    if mode == "personal":
        apply_wecom_personal_allowlist()
    else:
        upsert_env_key("WECOM_ALLOWED_USERS", "")
        upsert_env_key("WECOM_ALLOWED_CHATS", "")

    bot_id = get_env_value("WECOM_BOT_ID")
    secret = get_env_value("WECOM_SECRET")
    if bot_id and secret:
        # WS 长连接实测（复用 setup_wecom.py 的 test_connection；会短暂挤掉旧连接，服务自动重连）
        import asyncio

        from scripts.setup_wecom import test_connection
        print("[*] 正在实测长连接（订阅 + 心跳）...")
        ok, detail = asyncio.run(test_connection(bot_id, secret))
        if ok:
            print(f"[OK] 长连接实测通过：{detail}")
            print("     若服务正在运行，请重启服务使企微通道生效，然后在企业微信里给机器人发 /help 验证。")
        else:
            print(f"[!] 长连接实测失败：{detail}（请核对机器人详情页配置方式为长连接）")
    else:
        print("[!] 未在 .env 中检测到 WECOM_BOT_ID/WECOM_SECRET，跳过长连接实测。")

    if mode == "public":
        ask_wecom_group_import()

    if bot_id and secret:
        offer_restart("企业微信凭据已写入 .env")


def ask_group_import():
    """飞书公用模式下的白名单导入交互。"""
    print("\n" + "=" * 46)
    print("          飞书公用模式 - 白名单配置")
    print("=" * 46)
    print(">> 默认模式：ALLOWED_USERS 保持为空，企业内全员均可直接访问。")
    print()
    choice = input("[?] 是否需要将特定群聊的所有用户ID一键导入为白名单？[Y/N] (默认 N): ").strip().lower()
    if choice == "y":
        run_cmd([sys.executable, str(ROOT_DIR / "scripts" / "import_feishu_group.py")])
    else:
        from scripts.import_feishu_group import upsert_env
        upsert_env("ALLOWED_USERS", "")
        print("[OK] 已将 ALLOWED_USERS 设为全员开放模式。")


def configure_autostart():
    """配置/管理开机自启（每次开机自动静默后台运行）。"""
    if sys.platform == "win32":
        autostart_script = ROOT_DIR / "scripts" / "setup_autostart.bat"
        if not autostart_script.exists():
            print("[!] 未找到 scripts/setup_autostart.bat。")
            return
        run_cmd(["cmd.exe", "/c", str(autostart_script)])
    else:
        autostart_script = ROOT_DIR / "scripts" / "setup_autostart_mac.sh"
        if not autostart_script.exists():
            print("[!] 未找到 scripts/setup_autostart_mac.sh。")
            return
        run_cmd(["bash", str(autostart_script)])


def collect_init_status() -> dict:
    """收集前三步初始化状态（运行环境 / 目录 / Codex 认证与初始偏好）。"""
    from app.state.preferences import preferences_manager

    workspace = get_env_value("DEFAULT_WORKSPACE")
    session_dir = get_env_value("CODEX_SESSION_DIR")

    node_version = ""
    if shutil.which("node"):
        try:
            node_version = subprocess.check_output(["node", "-v"], text=True).strip()
        except Exception:
            node_version = "(已安装)"
    codex_ready = shutil.which("codex") is not None

    auth_file = Path.home() / ".codex" / "auth.json"
    auth_ready = auth_file.exists() and auth_file.stat().st_size > 10

    prefs = preferences_manager.get_global()

    return {
        "workspace": workspace,
        "session_dir": session_dir,
        "workspace_done": bool(workspace.strip()) and bool(session_dir.strip()),
        "node_version": node_version,
        "codex_ready": codex_ready,
        "environment_done": bool(node_version) and codex_ready,
        "auth_ready": auth_ready,
        "pref_model": prefs.model,
        "pref_effort": prefs.level,
        "pref_mode": prefs.mode,
        "prefs_complete": prefs.complete,
        "model_done": auth_ready and prefs.complete,
    }


def run_init_steps(force: bool = False):
    """执行前三步初始化。force=True 全部重跑；否则只补跑未完成的步骤。"""
    status = collect_init_status()
    steps = [
        ("environment", "运行环境", check_environment),
        ("workspace", "运行目录与会话目录", confirm_directories),
        ("model", "Codex 认证与初始配置", check_or_setup_models),
    ]
    for key, label, fn in steps:
        if force or not status[f"{key}_done"]:
            fn()
        else:
            print(f"[OK] {label}已完成初始化，跳过。")


def show_init_config():
    """查看前三步初始化配置信息，可选择重置并重跑。"""
    from app.state.preferences import preferences_manager

    status = collect_init_status()
    print("\n" + "=" * 60)
    print("        初始化配置信息（Step 1-3 初始化设置）")
    print("=" * 60)
    print()
    print("  【Step 1 运行环境】")
    print(f"    Node.js         : {status['node_version'] or '未检测到（auto_feishu 需要 v20+）'}")
    print(f"    Codex CLI       : {'已就绪' if status['codex_ready'] else '未检测到'}")
    print("  【Step 2 目录】")
    print(f"    初始运行目录 (DEFAULT_WORKSPACE) : {status['workspace'] or '(未配置)'}")
    print(f"    历史会话目录 (CODEX_SESSION_DIR) : {status['session_dir'] or '(未配置，自动读取 ~/.codex/sessions)'}")
    print("  【Step 3 Codex 账号认证与初始运行配置】")
    print(f"    ChatGPT 登录态 (~/.codex/auth.json) : {'已就绪' if status['auth_ready'] else '未检测到'}")
    print(f"    默认模型     (Model)  : {status['pref_model'] or '(未配置)'}")
    print(f"    默认思考力度 (Effort) : {status['pref_effort'] or '(未配置)'}")
    print(f"    默认审批模式 (Mode)   : {status['pref_mode'] or '(未配置)'}")
    print()

    if status["workspace_done"] and status["environment_done"] and status["model_done"]:
        print("  [OK] 初始化设置已全部完成。")
    else:
        print("  [!] 存在未完成的初始化项。")
    choice = input("[?] 是否重置并重新运行初始化设置（Step 1-3）？[y/N] (直接回车 = 否): ").strip().lower()
    if choice == "y":
        preferences_manager.clear("")
        run_init_steps(force=True)


def main_menu():
    """【Step 4/4】消息平台接入与配置中心。"""
    while True:
        print("\n" + "=" * 60)
        print("    【Step 4/4】消息平台接入与配置中心 (Platform Access)")
        print("=" * 60)
        print("\n【飞书接入】")
        print(" 1. 配置飞书 - 个人：仅创建者可用")
        print(" 2. 配置飞书 - 公用：全部成员可用")
        print(" 3. └─ 一键授权飞书群成员：基于2，限制仅特定群成员可用")
        print("\n【企业微信接入】")
        print(" 4. 配置企业微信 - 个人：仅创建者可用")
        print(" 5. 配置企业微信 - 公用：全部成员可用")
        print(" 6. └─ 一键授权企微特定群：基于5，限制仅特定群成员可用")
        print("\n【账号与初始运行配置】")
        print(" 7. Codex 认证与初始运行配置（登录切换 / Model / Effort / Mode）")
        print("\n【系统】")
        print(" 8. 配置开机自启（每次开机自动静默后台运行）")
        print(" 9. 查看/重置初始化配置（工作空间/运行环境/Codex 认证与初始偏好）")
        print("\n【退出】")
        print(" 0. 退出向导（完成并显示启动说明）")
        print()

        try:
            choice = input("请选择 [0-9]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已退出。")
            break

        if choice == "1":
            setup_feishu("personal")
        elif choice == "2":
            setup_feishu("public")
            ask_group_import()
        elif choice == "3":
            run_cmd([sys.executable, str(ROOT_DIR / "scripts" / "import_feishu_group.py")])
        elif choice == "4":
            setup_wecom_auto("personal")
        elif choice == "5":
            setup_wecom_auto("public")
        elif choice == "6":
            run_cmd([sys.executable, str(ROOT_DIR / "scripts" / "import_wecom_group.py")])
        elif choice == "7":
            c = input("[?] 是否需要重新执行 'codex login' 登录/切换账号？[y/N] (直接回车 = 跳过登录仅改偏好): ").strip().lower()
            if c == "y":
                run_cmd(["codex", "login"])
            configure_initial_preferences()
        elif choice == "8":
            configure_autostart()
        elif choice == "9":
            show_init_config()
        elif choice in ("0", "q", "exit"):
            finish_setup()
            break
        else:
            print("[!] 无效选项，请重新输入。")


def finish_setup():
    """完成向导并展示运行说明。"""
    print("\n" + "=" * 50)
    print("[SUCCESS] MyCodex 安装与配置完成！")
    print("=" * 50)
    if sys.platform == "win32":
        print("  启动服务      : 双击 launcher_windows\\MyCodex.bat（重启用 launcher_windows\\MyCodex-Restart.bat）")
        print("  重新配置      : 双击 launcher_windows\\MyCodex-Setup.bat 重跑向导")
    else:
        print("  启动服务      : 双击 launcher_macos/MyCodex.command（或运行 bash scripts/restart_mac.sh）")
        print("  重新配置      : 双击 launcher_macos/MyCodex-Setup.command 重跑向导")
    print("  开机自启      : 配置中心选 8")
    print("  健康检查      : curl http://127.0.0.1:8090/health")
    print("=" * 50)


if __name__ == "__main__":
    os.chdir(ROOT_DIR)
    # 前三步为一次性初始化：已完成则跳过，直接进入 Step 4 配置中心
    status = collect_init_status()
    if status["workspace_done"] and status["environment_done"] and status["model_done"]:
        print("[OK] 初始化设置已完成（工作空间 / 运行环境 / Codex 认证），跳过 Step 1-3。")
        print("     （如需查看或重新配置初始化项：配置中心选 9）")
    else:
        run_init_steps()
    main_menu()

