#!/usr/bin/env python3
"""
WorkBuddy Token 提取工具（傻瓜版）

全自动检测和安装依赖，用户只需双击运行即可。

功能：
- 自动检测并安装 Python（如未安装）
- 自动安装脚本依赖
- 自动启动 WorkBuddy（调试模式）
- 自动提取 Token 并原子保存到仓库 data/token.json
"""

import os
import sys
import time
import json
import subprocess
import asyncio
from pathlib import Path

from token_storage import atomic_write_json


def run_cmd(cmd, check=True, shell=True):
    """执行命令并返回结果"""
    try:
        result = subprocess.run(
            cmd, shell=shell, capture_output=True, text=True, timeout=300
        )
        if check and result.returncode != 0:
            return False, result.stderr
        return True, result.stdout
    except subprocess.TimeoutExpired:
        return False, "命令执行超时"
    except Exception as e:
        return False, str(e)


def check_python():
    """检查 Python 是否安装"""
    try:
        result = subprocess.run(
            ["python3", "--version"], capture_output=True, text=True
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            print(f"  ✅ Python 已安装: {version}")
            return True
    except FileNotFoundError:
        pass

    print("  ❌ Python 未安装，正在安装...")
    return False


def install_homebrew():
    """安装 Homebrew"""
    print("\n正在安装 Homebrew...")
    print("（这可能需要几分钟，请耐心等待）\n")

    cmd = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    ok, msg = run_cmd(cmd, check=False)

    if ok:
        print("  ✅ Homebrew 安装成功！")
        return True
    else:
        print(f"  ⚠️ Homebrew 安装失败")
        print("\n请手动安装 Homebrew：")
        print("  打开终端，粘贴以下命令：")
        print("  /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
        return False


def install_python_via_installer():
    """通过 python.org 安装包安装 Python（不需要 sudo）"""
    print("\n正在下载 Python 安装包...")
    print("（这可能需要几分钟，取决于网速）\n")

    dmg_url = "https://www.python.org/ftp/python/3.13.0/python-3.13.0-macos11.pkg"
    dmg_path = "/tmp/python-installer.pkg"

    ok, _ = run_cmd(f'curl -L -o "{dmg_path}" "{dmg_url}"', check=False)
    if not ok:
        print("  ❌ 下载失败，请检查网络连接")
        return False

    print("  ✅ 下载完成，正在打开安装程序...")
    print("\n" + "=" * 50)
    print("  请在弹出的安装程序中：")
    print("  1. 点击「继续」")
    print("  2. 阅读许可协议，点击「继续」→「同意」")
    print("  3. 选择安装位置，点击「安装」")
    print("  4. 输入电脑密码（如果有）")
    print("  5. 等待安装完成，点击「关闭」")
    print("=" * 50)
    print("\n安装完成后，回到此窗口按回车键继续...")
    print("（或者输入 q 退出，稍后手动安装）")

    response = input("\n按回车继续，或输入 q 退出: ").strip().lower()
    if response == 'q':
        return False

    run_cmd(f'open "{dmg_path}"', check=False)

    input("\n安装完成后，按回车继续...")
    return True


def install_python():
    """安装 Python"""
    print("\n" + "=" * 50)
    print("正在安装 Python...")
    print("=" * 50)

    success, _ = run_cmd("which brew")
    if not success:
        print("  ❌ 未检测到 Homebrew")

        print("\n尝试通过安装包安装 Python（不需要 sudo）...")
        if install_python_via_installer():
            ok, _ = run_cmd("which python3", check=False)
            if ok:
                print("  ✅ Python 安装成功！")
                return True

        print("\n正在安装 Homebrew 作为备选...")
        if not install_homebrew():
            print("\n⚠️ 无法自动安装 Python")
            print("请手动安装: https://www.python.org/downloads/")
            return False

        print("\n重新检测 Homebrew...")
        success, _ = run_cmd("which brew")
        if not success:
            print("  ❌ Homebrew 安装失败")
            return False

    print("使用 Homebrew 安装 Python...")
    ok, msg = run_cmd("brew install python", check=False)
    if ok:
        print("  ✅ Python 安装成功！")
        return True
    else:
        print(f"  ⚠️ Homebrew 安装 Python 失败")
        print("尝试通过安装包安装...")

        if install_python_via_installer():
            ok, _ = run_cmd("which python3", check=False)
            if ok:
                print("  ✅ Python 安装成功！")
                return True

        return False


def install_dependencies():
    """安装脚本依赖"""
    print("\n正在安装依赖 (httpx, websockets)...")

    deps = ["httpx", "websockets"]
    for dep in deps:
        ok, _ = run_cmd(f"pip3 install {dep}")
        if ok:
            print(f"  ✅ {dep} 安装成功")
        else:
            print(f"  ❌ {dep} 安装失败")
            return False
    return True


def check_workbuddy_installed():
    """检查 WorkBuddy 是否安装"""
    paths = [
        Path("/Applications/WorkBuddy.app"),
        Path.home() / "Applications/WorkBuddy.app",
    ]
    for p in paths:
        if p.exists():
            return True, p
    return False, None


def check_workbuddy_running():
    """检查 WorkBuddy 是否正在运行（调试模式）"""
    try:
        import httpx
        resp = httpx.get("http://127.0.0.1:9222/json", timeout=2)
        if resp.status_code == 200:
            targets = resp.json()
            return any(
                isinstance(target, dict)
                and target.get("type") == "page"
                and "workbench" in str(target.get("url", "")).lower()
                and target.get("webSocketDebuggerUrl")
                for target in targets
            )
    except:
        pass
    return False


def is_workbuddy_process_running():
    """Avoid starting a second WorkBuddy instance without user approval."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "/WorkBuddy.app/"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start_workbuddy_debug():
    """启动 WorkBuddy 调试模式"""
    print("\n正在启动 WorkBuddy（调试模式）...")

    workbuddy_path = "/Applications/WorkBuddy.app/Contents/MacOS/Electron"
    cmd = (
        f'"{workbuddy_path}" --remote-debugging-address=127.0.0.1 '
        "--remote-debugging-port=9222"
    )

    try:
        subprocess.Popen(cmd, shell=True)
        print("  ✅ WorkBuddy 已启动")
        print("\n等待 WorkBuddy 完全加载（5秒）...")
        time.sleep(5)
        return True
    except Exception as e:
        print(f"  ❌ 启动失败: {e}")
        return False


def check_requirements():
    """检查前置条件"""
    print("\n[检查 1/4] 检测 Python...")
    if not check_python():
        if not install_python():
            return False

    print("\n[检查 2/4] 安装依赖...")
    if not install_dependencies():
        return False

    print("\n[检查 3/4] 检测 WorkBuddy...")
    installed, path = check_workbuddy_installed()
    if not installed:
        print("  ❌ WorkBuddy 未安装")
        print("  请先安装 WorkBuddy: https://workbuddy.com/download")
        return False
    print(f"  ✅ WorkBuddy 已安装: {path}")

    print("\n[检查 4/4] 检测 WorkBuddy 运行状态...")
    if check_workbuddy_running():
        print("  ✅ WorkBuddy 已运行（调试模式）")
    elif is_workbuddy_process_running():
        print("  ❌ WorkBuddy 正在运行，但没有可用的 9222 调试端口")
        print("  请保存工作并由你从托盘退出后，再重新运行本工具")
        print("  本工具不会结束或重启 WorkBuddy")
        return False
    else:
        print("  ⚠️ WorkBuddy 未运行，正在启动...")
        if not start_workbuddy_debug():
            return False

    return True


async def extract_token():
    """从 WorkBuddy 提取 Token"""
    import httpx
    import websockets

    print("\n正在连接 WorkBuddy...")

    async with httpx.AsyncClient() as client:
        resp = await client.get("http://127.0.0.1:9222/json", timeout=5)
        targets = resp.json()

    print(f"  ✅ 找到 {len(targets)} 个页面")

    ws_url = None
    for t in targets:
        if t.get("type") == "page" and "workbench" in t.get("url", ""):
            ws_url = t.get("webSocketDebuggerUrl")
            break

    if not ws_url:
        raise Exception("未找到 WorkBuddy 页面")

    print("  ✅ 已找到 WorkBuddy 页面")
    print("\n正在提取 Token...")

    async with websockets.connect(ws_url, ping_timeout=30) as ws:
        cmd = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": """
                    (async () => {
                        try {
                            const s = await window.vscode.ipcRenderer.invoke(
                                'vscode:genie:auth:getSession'
                            );
                            return JSON.stringify(s);
                        } catch(e) {
                            return JSON.stringify({error: e.message});
                        }
                    })()
                """,
                "awaitPromise": True,
                "returnByValue": True,
            },
        }
        await ws.send(json.dumps(cmd))
        result = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))

    value = result.get("result", {}).get("result", {}).get("value", "")
    if not value:
        raise Exception("CDP 返回为空")

    session = json.loads(value)
    if session.get("error"):
        raise Exception("WorkBuddy CDP 未返回可用的登录会话")

    return session


def save_token(access_token, refresh_token):
    """原子保存 Token 到被 Git 忽略的 data/token.json。"""
    output_file = Path(__file__).parent / "data" / "token.json"
    atomic_write_json(
        output_file,
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    return output_file


def main():
    print("=" * 50)
    print("  WorkBuddy Token 提取工具")
    print("=" * 50)
    print("\n正在检测环境...\n")

    if not check_requirements():
        input("\n按回车键退出...")
        sys.exit(1)

    try:
        session = asyncio.run(extract_token())
    except Exception as e:
        print(f"\n❌ 提取失败: {e}")
        print("\n请确保：")
        print("  1. WorkBuddy 已登录")
        print("  2. 重新运行此脚本")
        input("\n按回车键退出...")
        sys.exit(1)

    auth = session.get("auth", session)
    access_token = auth.get("accessToken", "")
    refresh_token = auth.get("refreshToken", "")

    if not access_token:
        print("\n❌ 未获取到 accessToken")
        print("请确保 WorkBuddy 已登录 AI 功能")
        input("\n按回车键退出...")
        sys.exit(1)

    try:
        output_file = save_token(access_token, refresh_token)
    except Exception as e:
        print(f"\n❌ 保存失败: {e}")
        input("\n按回车键退出...")
        sys.exit(1)

    print("\n" + "=" * 50)
    print("✅ 成功！")
    print("=" * 50)
    print(f"\n📄 Token 已安全保存（内容已隐藏）:")
    print(f"   {output_file}")
    print("\n🔑 Token 内容已隐藏，请勿分享保存文件")
    print("\n" + "=" * 50)

    input("\n按回车键退出...")


if __name__ == "__main__":
    main()
