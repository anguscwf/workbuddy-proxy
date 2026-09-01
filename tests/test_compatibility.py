#!/usr/bin/env python3
"""
WorkBuddy Proxy - 兼容性测试脚本

WorkBuddy 更新后运行此脚本，快速验证系统是否仍能正常工作。

用法:
    # 安装测试依赖
    pip install -r requirements-test.txt

    # 运行完整测试
    python tests/test_compatibility.py

    # 快速检查（跳过 API 调用）
    python tests/test_compatibility.py --skip-api

    # 检查 CDP 连接
    python tests/test_compatibility.py --test-cdp

    # 检查特定模型
    python tests/test_compatibility.py --model deepseek-v3
"""

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

try:
    import httpx
except ImportError:
    sys.exit("缺少依赖: pip install httpx")

try:
    import jwt
except ImportError:
    sys.exit("缺少依赖: pip install pyjwt")


PROXY_URL = "http://127.0.0.1:19090"
PROXY_API_KEY = "wb-proxy-key"
CDP_URL = "http://127.0.0.1:9222"
WB_API_BASE = "https://copilot.tencent.com"

MODELS_TO_TEST = [
    "deepseek-v3",
    "claude-opus-4.6",
    "deepseek-r1",
]


class Status(Enum):
    PASS = "✅ PASS"
    FAIL = "❌ FAIL"
    SKIP = "⏭️  SKIP"
    WARN = "⚠️  WARN"


@dataclass
class TestResult:
    name: str
    status: Status
    message: str = ""
    details: dict = field(default_factory=dict)
    elapsed_ms: float = 0


class CompatibilityTester:
    def __init__(self, skip_api: bool = False, test_model: str = None):
        self.results: list[TestResult] = []
        self.skip_api = skip_api
        self.test_model = test_model
        self.access_token = ""
        self.wb_version = ""
        self.http_client: httpx.AsyncClient | None = None

    async def run_all(self):
        print("\n" + "=" * 60)
        print("WorkBuddy Proxy - 兼容性测试")
        print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60 + "\n")

        self.http_client = httpx.AsyncClient(timeout=30)

        try:
            await self.test_wb_version_detection()
            await self.test_cdp_connection()
            if not self.skip_api:
                await self.test_proxy_health()
                await self.test_token_loading()
                await self.test_header_validation()
                await self.test_model_list()
                await self.test_api_call()
            else:
                self.results.append(TestResult(
                    "API 调用测试", Status.SKIP, "跳过（--skip-api 参数）"
                ))
        finally:
            await self.http_client.aclose()

        self.print_summary()

    async def test_wb_version_detection(self):
        name = "WorkBuddy 版本检测"
        start = time.monotonic()

        candidates = [
            Path("/Applications/WorkBuddy.app/Contents/Resources/app/product.json"),
            Path.home() / "Library/Application Support/WorkBuddy/app/product.json",
        ]

        detected = ""
        for p in candidates:
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    detected = data.get("genieVersion", "")
                    if detected:
                        break
                except Exception:
                    continue

        elapsed = (time.monotonic() - start) * 1000

        if detected:
            self.wb_version = detected
            self.results.append(TestResult(
                name, Status.PASS,
                f"检测到版本: {detected}",
                {"version": detected, "path": str(p)}
            ))
        else:
            self.results.append(TestResult(
                name, Status.WARN,
                "未检测到 WorkBuddy 安装路径，使用默认值",
                {"expected_path": str(candidates[0])}
            ))
            self.wb_version = "4.8.1"

    async def test_cdp_connection(self):
        name = "CDP 连接测试"
        start = time.monotonic()

        try:
            resp = await self.http_client.get(f"{CDP_URL}/json", timeout=5)
            targets = resp.json()
            elapsed = (time.monotonic() - start) * 1000

            workbench_found = any(
                "workbench" in t.get("url", "") for t in targets if t.get("type") == "page"
            )

            if workbench_found:
                self.results.append(TestResult(
                    name, Status.PASS,
                    f"找到 {len(targets)} 个 CDP 目标（含 Workbench）",
                    {"target_count": len(targets), "workbench_found": workbench_found}
                ))
            else:
                self.results.append(TestResult(
                    name, Status.FAIL,
                    "CDP 端口可达，但没有 WorkBuddy workbench（可能被其他程序占用）",
                    {"target_count": len(targets), "workbench_found": False}
                ))
        except httpx.ConnectError as e:
            elapsed = (time.monotonic() - start) * 1000
            self.results.append(TestResult(
                name, Status.FAIL,
                f"无法连接 CDP: {e}",
                {"url": CDP_URL}
            ))
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            self.results.append(TestResult(
                name, Status.FAIL,
                f"CDP 测试异常: {e}"
            ))

    async def test_proxy_health(self):
        name = "代理健康检查"
        start = time.monotonic()

        try:
            resp = await self.http_client.get(
                f"{PROXY_URL}/health",
                headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
                timeout=10
            )
            data = resp.json()
            elapsed = (time.monotonic() - start) * 1000

            if data.get("status") == "ok":
                self.results.append(TestResult(
                    name, Status.PASS,
                    f"代理运行正常 (Token: {'有效' if data.get('has_token') else '无效'})",
                    data
                ))
            else:
                self.results.append(TestResult(
                    name, Status.WARN,
                    f"代理状态降级: {data}",
                    data
                ))
        except httpx.ConnectError:
            elapsed = (time.monotonic() - start) * 1000
            self.results.append(TestResult(
                name, Status.FAIL,
                f"无法连接到代理 (请确认服务已启动: {PROXY_URL})"
            ))
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            self.results.append(TestResult(
                name, Status.FAIL,
                f"健康检查异常: {e}"
            ))

    async def test_token_loading(self):
        name = "Token 加载测试"
        start = time.monotonic()

        token_file = Path(__file__).parent.parent / "data" / "token.json"
        elapsed = (time.monotonic() - start) * 1000

        if not token_file.exists():
            self.results.append(TestResult(
                name, Status.FAIL,
                f"Token 文件不存在: {token_file}",
                {"expected_path": str(token_file)}
            ))
            return

        try:
            data = json.loads(token_file.read_text(encoding="utf-8"))
            access_token = data.get("access_token", "")
            refresh_token = data.get("refresh_token", "")

            if not access_token:
                self.results.append(TestResult(
                    name, Status.FAIL,
                    "Token 文件为空或格式错误"
                ))
                return

            try:
                payload = jwt.decode(access_token, options={"verify_signature": False})
                exp = payload.get("exp", 0)
                exp_date = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M:%S")
                is_expired = time.time() >= exp
                needs_refresh = time.time() >= (exp - 300)

                self.access_token = access_token

                self.results.append(TestResult(
                    name, Status.PASS if not is_expired else Status.WARN,
                    f"Token {'有效' if not is_expired else '已过期'}，过期时间: {exp_date}",
                    {
                        "expires": exp_date,
                        "has_refresh": bool(refresh_token),
                        "needs_refresh": needs_refresh,
                    }
                ))
            except Exception as e:
                self.results.append(TestResult(
                    name, Status.FAIL,
                    f"Token 解析失败: {e}"
                ))

        except Exception as e:
            self.results.append(TestResult(
                name, Status.FAIL,
                f"Token 文件读取失败: {e}"
            ))

    async def test_header_validation(self):
        name = "Header 完整性验证"
        start = time.monotonic()

        required_headers = {
            "X-IDE-Type": "CodeBuddyIDE",
            "X-IDE-Name": "CodeBuddyIDE",
            "X-IDE-Version": self.wb_version,
            "X-Product-Version": self.wb_version,
            "X-Product": "SaaS",
            "X-Env-ID": "production",
        }

        headers = {k: v for k, v in required_headers.items()}
        elapsed = (time.monotonic() - start) * 1000

        if self.access_token:
            try:
                payload = jwt.decode(self.access_token, options={"verify_signature": False})
                user_id = payload.get("sub", "")
                iss = payload.get("iss", "")

                m = re.search(r"/sso-([^/]+)$", iss)
                enterprise_id = m.group(1) if m else ""

                headers.update({
                    "X-User-Id": user_id,
                    "X-Enterprise-Id": enterprise_id,
                })
            except Exception:
                pass

        missing = [k for k, v in required_headers.items() if not v]

        if not missing:
            self.results.append(TestResult(
                name, Status.PASS,
                f"所有必需 Header 正常 (版本: {self.wb_version})",
                {"headers": headers}
            ))
        else:
            self.results.append(TestResult(
                name, Status.FAIL,
                f"缺少必需 Header: {missing}"
            ))

    async def test_model_list(self):
        name = "模型列表获取"
        start = time.monotonic()

        try:
            resp = await self.http_client.get(
                f"{PROXY_URL}/v1/models",
                headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
                timeout=10
            )
            data = resp.json()
            elapsed = (time.monotonic() - start) * 1000

            models = data.get("data", [])
            model_ids = [m["id"] for m in models]

            critical_models = ["deepseek-v3", "claude-opus-4.6"]
            missing = [m for m in critical_models if m not in model_ids]

            if not missing:
                self.results.append(TestResult(
                    name, Status.PASS,
                    f"获取到 {len(models)} 个模型，核心模型全部可用",
                    {"model_count": len(models), "models": model_ids[:5]}
                ))
            else:
                self.results.append(TestResult(
                    name, Status.WARN,
                    f"核心模型缺失: {missing}",
                    {"available": model_ids}
                ))
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            self.results.append(TestResult(
                name, Status.FAIL,
                f"模型列表获取失败: {e}"
            ))

    async def test_api_call(self):
        name = "API 调用测试"
        start = time.monotonic()

        test_model = self.test_model or "deepseek-v3"
        models_to_try = [test_model] if test_model else MODELS_TO_TEST

        success = False
        error_msg = ""

        for model in models_to_try:
            try:
                resp = await self.http_client.post(
                    f"{PROXY_URL}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {PROXY_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "说 hi"}],
                        "max_tokens": 20,
                    },
                    timeout=60,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    elapsed = (time.monotonic() - start) * 1000

                    self.results.append(TestResult(
                        f"API 调用 ({model})", Status.PASS,
                        f"调用成功，响应: {content[:50]}...",
                        {"model": model, "response": content[:100]}
                    ))
                    success = True
                    break
                else:
                    error_msg = (
                        f"HTTP {resp.status_code}（响应正文已隐藏）"
                    )
            except httpx.TimeoutException:
                error_msg = f"模型 {model} 调用超时"
            except Exception as e:
                error_msg = str(e)

        if not success:
            elapsed = (time.monotonic() - start) * 1000
            self.results.append(TestResult(
                name, Status.FAIL,
                f"所有测试模型均失败: {error_msg}",
                {"tried_models": models_to_try}
            ))

    def print_summary(self):
        print("\n" + "-" * 60)
        print("测试结果摘要")
        print("-" * 60)

        passed = sum(1 for r in self.results if r.status == Status.PASS)
        failed = sum(1 for r in self.results if r.status == Status.FAIL)
        warned = sum(1 for r in self.results if r.status == Status.WARN)
        skipped = sum(1 for r in self.results if r.status == Status.SKIP)

        for r in self.results:
            icon = r.status.value
            print(f"{icon} [{r.name}] {r.message}")

        print("-" * 60)
        print(f"总计: {len(self.results)} 项 | "
              f"✅ 通过: {passed} | "
              f"❌ 失败: {failed} | "
              f"⚠️  警告: {warned} | "
              f"⏭️  跳过: {skipped}")
        print("-" * 60)

        if failed > 0:
            print("\n⚠️  检测到兼容性问题，请检查上述失败的测试项")
            print("常见问题排查:")
            print("  1. WorkBuddy 版本是否已更新？检查: https://workbuddy.com/update")
            print("  2. Token 是否过期？删除 data/token.json 后重新提取")
            print("  3. CDP 端口是否被占用？尝试: python extract_token.py --port 9223")
            sys.exit(1)
        elif warned > 0:
            print("\n⚠️  存在警告项，建议关注")
            sys.exit(0)
        else:
            print("\n🎉 所有测试通过！系统兼容正常")
            sys.exit(0)


async def main():
    parser = argparse.ArgumentParser(description="WorkBuddy Proxy 兼容性测试")
    parser.add_argument("--skip-api", action="store_true",
                        help="跳过 API 调用测试（代理未运行时使用）")
    parser.add_argument("--test-cdp", action="store_true",
                        help="仅测试 CDP 连接")
    parser.add_argument("--model", type=str, default=None,
                        help="指定测试模型 (默认: deepseek-v3)")
    args = parser.parse_args()

    tester = CompatibilityTester(
        skip_api=args.skip_api or args.test_cdp,
        test_model=args.model
    )

    if args.test_cdp:
        tester.http_client = httpx.AsyncClient(timeout=30)
        await tester.test_wb_version_detection()
        await tester.test_cdp_connection()
        await tester.http_client.aclose()
        for r in tester.results:
            print(f"{r.status.value} [{r.name}] {r.message}")
    else:
        await tester.run_all()


if __name__ == "__main__":
    asyncio.run(main())
