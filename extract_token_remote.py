#!/usr/bin/env python3
"""
从远程 WorkBuddy 实例提取 Token。

支持两种模式：
1. 本地模式：自动检测本机 WorkBuddy
2. 远程模式：连接远程 Mac 的 CDP 端口

用法:
    # 本机自动检测
    python extract_token_remote.py

    # 远程 Mac（需要先在目标机器启动 WorkBuddy 调试模式）
    python extract_token_remote.py --host 192.168.1.100 --port 9222 --save

前置条件（远程 Mac）：
    /Applications/WorkBuddy.app/Contents/MacOS/Electron --remote-debugging-port=9222

    注意：可能需要配置防火墙允许 9222 端口入站。
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

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
    base = f"http://{cdp_host}:{cdp_port}"

    print(f"正在连接 CDP ({base})...")

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{base}/json", timeout=10)
        except httpx.ConnectError as e:
            print(f"❌ 无法连接 CDP: {e}")
            print(f"\n请确保远程 Mac 已启动 WorkBuddy 调试模式：")
            print(f"  /Applications/WorkBuddy.app/Contents/MacOS/Electron --remote-debugging-port={cdp_port}")
            return None
        targets = resp.json()

    ws_url = None
    for t in targets:
        if t.get("type") == "page" and "workbench" in t.get("url", ""):
            ws_url = t.get("webSocketDebuggerUrl")
            if ws_url:
                if cdp_host != "127.0.0.1" and cdp_host != "localhost":
                    ws_url = ws_url.replace("127.0.0.1", cdp_host)
                print(f"✅ 找到 Workbench 页面")
                break

    if not ws_url:
        for t in targets:
            if t.get("type") == "page":
                ws_url = t.get("webSocketDebuggerUrl")
                if ws_url:
                    if cdp_host != "127.0.0.1" and cdp_host != "localhost":
                        ws_url = ws_url.replace("127.0.0.1", cdp_host)
                    print(f"⚠️  未找到 Workbench，找到通用页面")
                    break

    if not ws_url:
        print("❌ 未找到可用的 CDP 页面目标")
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
                            const providers = window.__GENIE_DEFAULT_APP_PROVIDERS__;
                            if (providers?.auth?.getToken) {
                                const token = await providers.auth.getToken();
                                return JSON.stringify({
                                    accessToken: typeof token === 'string'
                                        ? token
                                        : (token?.accessToken || token?.token || ''),
                                    refreshToken: typeof token === 'object'
                                        ? (token?.refreshToken || '')
                                        : ''
                                });
                            }
                            if (window.vscode?.ipcRenderer?.invoke) {
                                return JSON.stringify(await window.vscode.ipcRenderer.invoke(
                                    'vscode:genie:auth:getSession'
                                ));
                            }
                            throw new Error('No supported WorkBuddy auth API found');
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
        print(f"❌ CDP 错误: {session['error']}")
        return None

    return session


def main():
    parser = argparse.ArgumentParser(
        description="从 WorkBuddy 提取 Token（支持远程）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 本机自动检测
  python extract_token_remote.py

  # 远程 Mac
  python extract_token_remote.py --host 192.168.1.100 --save

  # 自定义端口
  python extract_token_remote.py --host 192.168.1.100 --port 9223 --save
        """
    )
    parser.add_argument(
        "--host", "-H",
        default="127.0.0.1",
        help="CDP 主机地址 (默认: 127.0.0.1)"
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
        print(f"\n🔗 远程模式")
        print(f"   目标: {args.host}:{args.port}")
        print(f"   注意: 确保目标 Mac 已启动 WorkBuddy 调试模式\n")
    else:
        print(f"\n🔗 本地模式\n")

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
    print(f"   accessToken:  extracted (len={len(access_token)})")
    print(f"   refreshToken: extracted={bool(refresh_token)} "
          f"(len={len(refresh_token) if refresh_token else 0})")

    if args.save or args.output:
        import time as time_module
        out_path = Path(args.output) if args.output else Path(__file__).parent / "data" / "token.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "saved_at": time_module.strftime("%Y-%m-%d %H:%M:%S"),
            "source_host": args.host if is_remote else "localhost",
            "source_port": args.port,
        }, indent=2))
        print(f"\n💾 已保存到: {out_path}")

    if is_remote:
        print(f"\n📋 接下来在本地 Mac 上：")
        print(f"   1. 将生成的 data/token.json 复制到本项目")
        print(f"   2. 启动代理: python server.py")


if __name__ == "__main__":
    main()
