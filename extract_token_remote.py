#!/usr/bin/env python3
"""
通过本机回环地址或 SSH 本地端口转发提取 WorkBuddy Token。

支持两种模式：
1. 本地模式：自动检测本机 WorkBuddy
2. SSH 隧道模式：先把远程 Mac 的 CDP 转发到本机回环端口

用法:
    # 本机自动检测
    python extract_token_remote.py

    # 远程 Mac：先建立 SSH 本地转发，再连接本机端口
    ssh -N -L 9223:127.0.0.1:9222 user@remote-mac
    python extract_token_remote.py --host 127.0.0.1 --port 9223 --save

前置条件（远程 Mac）：
    /Applications/WorkBuddy.app/Contents/MacOS/Electron \
        --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222

安全约束：CDP 不提供可靠认证，禁止把 9222 直接暴露到局域网或公网。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from token_storage import atomic_write_json

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import httpx
except ImportError:
    sys.exit("请先安装依赖: pip install httpx")

try:
    import websockets
except ImportError:
    sys.exit("请先安装依赖: pip install websockets")


async def extract(cdp_host: str = "127.0.0.1", cdp_port: int = 9222) -> dict | None:
    if cdp_host not in ("127.0.0.1", "localhost"):
        print("❌ 拒绝直连远程 CDP；请先建立 SSH 本地端口转发")
        return None
    base = f"http://{cdp_host}:{cdp_port}"

    print(f"正在连接 CDP ({base})...")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{base}/json", timeout=10)
        except httpx.ConnectError as e:
            print(f"❌ 无法连接 CDP: {e}")
            print(f"\n请确保远程 Mac 已启动 WorkBuddy 调试模式：")
            print(
                "  /Applications/WorkBuddy.app/Contents/MacOS/Electron "
                f"--remote-debugging-address=127.0.0.1 --remote-debugging-port={cdp_port}"
            )
            return None
        resp.raise_for_status()
        targets = resp.json()

    ws_url = None
    for t in targets:
        if t.get("type") == "page" and "workbench" in t.get("url", ""):
            ws_url = t.get("webSocketDebuggerUrl")
            if ws_url:
                print(f"✅ 找到 Workbench 页面")
                break

    if not ws_url:
        print("❌ 该端口没有 WorkBuddy workbench；可能被其他程序占用")
        return None

    print(f"正在通过 WebSocket 提取 Token...")

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
        print("❌ CDP 返回为空")
        return None

    session = json.loads(value)
    if session.get("error"):
        print("❌ WorkBuddy CDP 未返回可用的登录会话")
        return None

    return session


def main():
    parser = argparse.ArgumentParser(
        description="从 WorkBuddy 提取 Token（远程时仅支持 SSH 本地转发）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 本机自动检测
  python extract_token_remote.py

  # 远程 Mac（另一个终端保持 SSH 隧道运行）
  ssh -N -L 9223:127.0.0.1:9222 user@remote-mac
  python extract_token_remote.py --host 127.0.0.1 --port 9223 --save

  # 自定义端口
  python extract_token_remote.py --host 127.0.0.1 --port 9223 --save
        """
    )
    parser.add_argument(
        "--host", "-H",
        default="127.0.0.1",
        help="CDP 主机地址；安全起见只允许 127.0.0.1/localhost"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=9222,
        help="CDP 端口 (默认: 9222)"
    )
    parser.add_argument(
        "--save", "-s",
        action="store_true",
        help="保存到 data/token.json"
    )
    parser.add_argument(
        "--output", "-o",
        help="保存到指定文件 (默认: data/token.json)"
    )
    args = parser.parse_args()

    is_remote = args.host not in ("127.0.0.1", "localhost")

    if is_remote:
        parser.error(
            "拒绝直连远程 CDP：请用 SSH -L 转发到 127.0.0.1 后再运行"
        )
    else:
        print(f"\n🔗 本机/SSH 本地转发模式\n")

    session = asyncio.run(extract(args.host, args.port))
    if not session:
        sys.exit(1)

    auth = session.get("auth", session)
    access_token = auth.get("accessToken", "")
    refresh_token = auth.get("refreshToken", "")

    if not access_token:
        print("❌ 未获取到 accessToken")
        sys.exit(1)

    print(f"\n✅ Token 提取成功！")
    print("   Token 内容已隐藏")

    if args.save or args.output:
        import time as time_module
        out_path = Path(args.output) if args.output else Path(__file__).parent / "data" / "token.json"
        atomic_write_json(out_path, {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "saved_at": time_module.strftime("%Y-%m-%d %H:%M:%S"),
            "source_host": "localhost",
            "source_port": args.port,
        })
        print(f"\n💾 已保存到: {out_path}")

if __name__ == "__main__":
    main()
