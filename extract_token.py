#!/usr/bin/env python3
"""
从运行中的 WorkBuddy 提取 Token（通过 CDP）。

用法:
    python extract_token.py                     # 默认 CDP 端口 9222
    python extract_token.py --port 9223         # 自定义端口
    python extract_token.py --save              # 提取并保存到 data/token.json

前提: WorkBuddy 以 --remote-debugging-port=9222 启动。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import httpx
except ImportError:
    sys.exit("请先安装依赖: pip install httpx")


async def extract(cdp_port: int = 9222) -> dict | None:
    base = f"http://127.0.0.1:{cdp_port}"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{base}/json", timeout=5)
        except httpx.ConnectError:
            print(f"无法连接 CDP ({base})，请确认 WorkBuddy 已用调试模式启动")
            return None
        targets = resp.json()

    ws_url = None
    for t in targets:
        if t.get("type") == "page" and "workbench" in t.get("url", ""):
            ws_url = t.get("webSocketDebuggerUrl")
            break
    if not ws_url:
        for t in targets:
            if t.get("type") == "page":
                ws_url = t.get("webSocketDebuggerUrl")
                break
    if not ws_url:
        print("未找到可用的 CDP 页面目标")
        return None

    try:
        import websockets
    except ImportError:
        sys.exit("请安装 websockets: pip install websockets")

    async with websockets.connect(ws_url) as ws:
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
        result = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

    value = result.get("result", {}).get("result", {}).get("value", "")
    if not value:
        print("CDP 返回为空")
        return None

    session = json.loads(value)
    if session.get("error"):
        print(f"CDP 错误: {session['error']}")
        return None

    return session


def main():
    parser = argparse.ArgumentParser(description="从 WorkBuddy 提取 Token")
    parser.add_argument("--port", type=int, default=9222, help="CDP 端口 (默认 9222)")
    parser.add_argument("--save", action="store_true", help="保存到 data/token.json")
    args = parser.parse_args()

    session = asyncio.run(extract(args.port))
    if not session:
        sys.exit(1)

    # 兼容新版（嵌套 auth）和旧版（扁平）结构
    auth = session.get("auth", session)
    access_token = auth.get("accessToken", "")
    refresh_token = auth.get("refreshToken", "")

    if not access_token:
        print("未获取到 accessToken")
        sys.exit(1)

    print(f"accessToken:  extracted (len={len(access_token)})")
    print(f"refreshToken: extracted={bool(refresh_token)} (len={len(refresh_token)})")

    if args.save:
        import time
        out = Path(__file__).parent / "data" / "token.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, indent=2))
        print(f"\n已保存到 {out}")


if __name__ == "__main__":
    main()
