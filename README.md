# WorkBuddy Proxy

将 WorkBuddy 内置的 AI 模型通过 **OpenAI 兼容 API** 暴露，可直接接入 Cherry Studio、ChatGPT-Next-Web、Cursor 等第三方工具。

> **无需公司 VPN 或特殊网络环境**，`copilot.tencent.com` 是公网 SaaS 服务，普通网络直连即可。

---

## 可用模型

共 **30** 个模型，全部经过可用性验证（2026-04-06）。

| 类别 | 模型 ID | 备注 |
|------|---------|------|
| **DeepSeek** (9) | `deepseek-r1` · `deepseek-v3` · `deepseek-v3.2` · `deepseek-v3-1` · `deepseek-v3-0324` · `deepseek-v3-1-volc` · `deepseek-v3-0324-lkeap` · `deepseek-r1-0528-lkeap` · `deepseek-v3-2-volc-ioa` | 全部稳定 |
| **Claude** (7) | `claude-opus-4.6` · `claude-opus-4.6-1m` · `claude-sonnet-4.6` · `claude-sonnet-4.6-1m` · `claude-opus-4.5` · `claude-4.5` · `claude-haiku-4.5` | 推荐首选；`-1m` 后缀为 1M 上下文版本 |
| **Gemini** (2) | `gemini-3.0-pro` · `gemini-3.1-flash-lite` | `gemini-3.0-pro` 偶有间歇性空响应 |
| **GLM** (7) | `glm-4.6` · `glm-4.7` · `glm-4.7-ioa` · `glm-5.0-ioa` · `glm-5.0-turbo-ioa` · `glm-5v-turbo` · `glm-5v-turbo-ioa` | `5v` 系列支持视觉 |
| **混元** (3) | `hunyuan-2.0-instruct` · `hunyuan-2.0-instruct-ioa` · `hunyuan-2.0-thinking-ioa` | `thinking` 为推理模型 |
| **Kimi** (1) | `kimi-k2.5-ioa` | |
| **默认** (1) | `codewise-default-model-v2` | WorkBuddy 默认路由模型 |

> 完整的模型测试报告参见 [MODEL_ANALYSIS.md](./MODEL_ANALYSIS.md)。

---

## 前置准备

| 项目 | Windows | macOS |
|------|---------|-------|
| **Python 3.10+** | [python.org](https://www.python.org/downloads/)，**安装时勾选 "Add Python to PATH"** | `brew install python` |
| **Git** | [git-scm.com](https://git-scm.com/download/win) | macOS 自带 |
| **WorkBuddy** | 已安装并**至少登录过一次** | 同左 |
| **Docker**（可选） | [Docker Desktop](https://www.docker.com/products/docker-desktop/) | 同左 |

---

## 快速开始

### 第一步：克隆仓库

```bash
git clone https://github.com/rocky99261/workbuddy-proxy.git
cd workbuddy-proxy
```

---

### 第二步：环境配置 & Token 提取

Token 的真实有效期以 JWT 的 `exp` 为准；当前环境观测约 **30 天**。代理会在剩余不足 3 天时预警。

<details>
<summary><b>Windows — 一键配置（推荐）</b></summary>

双击 `setup.bat`，按提示操作即可自动完成依赖安装和 Token 提取。

如果需要手动操作：

```powershell
# 1. 若 WorkBuddy 已运行，先保存工作并从系统托盘手动退出；再用仓库脚本启动
.\start-wb-debug.bat

# 常见安装路径：
#   %LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe
#   C:\Program Files\WorkBuddy\WorkBuddy.exe

# 2. 新开一个窗口，安装依赖并提取 Token
python -m pip install -r requirements.txt
python extract_token.py --save

# 3. 看到 "已保存到 data\token.json" 后，运行中的代理会在 10 秒内热加载
```

</details>

<details>
<summary><b>macOS — 手动配置</b></summary>

```bash
# 1. 以仅监听本机回环地址的调试模式启动 WorkBuddy
/Applications/WorkBuddy.app/Contents/MacOS/Electron --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222

# 2. 新开一个终端，安装依赖并提取 Token
pip3 install -r requirements.txt
python3 extract_token.py --save

# 3. 看到 "已保存到 data/token.json" 后，可以关闭 WorkBuddy
```

</details>

> **提取失败？** 检查以下几点：
> - WorkBuddy 是否已登录（打开 WorkBuddy 能正常使用 AI 功能）
> - 是否用 `--remote-debugging-port=9222` 参数启动的
> - 端口 9222 是否被占用（可以换成 9223，对应 `python extract_token.py --port 9223 --save`）

---

### 第三步：启动服务

#### 方式一：直接运行（推荐本地使用）

| 系统 | 命令 | 说明 |
|------|------|------|
| **Windows** | 双击 `start.bat` | 最简单，无需任何配置 |
| **Windows** | `.\start.ps1` 或 `.\start.ps1 -Port 8080` | PowerShell 用户 |
| **macOS** | `./start.sh` 或 `./start.sh --port 8080` | |

脚本会自动检查 Python、安装依赖、提取 Token（如需要），然后启动服务。

#### 方式二：Docker

适用于 Windows 和 macOS，安装好 Docker Desktop 后：

```bash
docker compose up -d
```

#### 验证服务状态

```bash
# macOS / Linux
curl http://127.0.0.1:19090/health

# Windows PowerShell
Invoke-RestMethod http://127.0.0.1:19090/health

# Windows CMD
curl http://127.0.0.1:19090/health
```

返回 `{"status":"ok","has_token":true,"expired":false,"state":"ready",...}` 即表示 Token 层正常；真实调用仍需用一次最小聊天请求确认上游可用。

---

### 第四步：配置第三方工具

在 Cherry Studio / ChatGPT-Next-Web / Cursor / OpenClaw 等工具中：

| 配置项 | 值 |
|--------|------|
| **Base URL** | `http://127.0.0.1:19090/v1` |
| **API Key** | `wb-proxy-key` |

选择模型时填写上方表格中的**模型 ID**（如 `deepseek-v3`、`claude-opus-4.6`）。

---

## Windows 持久化运行

在 Windows 主机上让服务**开机自启、后台常驻**，有以下几种方案：

### 方案一：Docker Desktop 自动启动（最简单）

Docker Desktop 默认会随 Windows 启动，容器配置了 `restart: unless-stopped`，所以 **开机后服务自动恢复**，无需额外配置。

确认设置：
1. Docker Desktop → Settings → General → 勾选 **"Start Docker Desktop when you sign in"**
2. 确认容器已用 `docker compose up -d` 启动过

之后每次开机，容器自动启动。

### 方案二：Windows 任务计划程序（不用 Docker）

适合不想装 Docker、直接用 Python 运行的场景。

**1. 使用守护脚本**

项目已包含 `watchdog.ps1`：登录后启动 proxy，proxy 崩溃时自动拉起；它不会结束、启动或重启 WorkBuddy。先人工安装依赖并至少成功启动一次 proxy。

**2. 配置任务计划**

打开 PowerShell（管理员），执行以下命令创建开机自启任务：

```powershell
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"C:\path\to\workbuddy-proxy\watchdog.ps1`" -Port 19090" `
    -WorkingDirectory "C:\path\to\workbuddy-proxy"

$trigger = New-ScheduledTaskTrigger -AtLogon

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName "WorkBuddyProxy" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "WorkBuddy OpenAI-compatible reverse proxy"
```

> **注意**：将 `C:\path\to\workbuddy-proxy` 替换为你的实际项目路径；若 `.env` 的 `PROXY_PORT` 不是 19090，任务参数 `-Port` 也要保持一致。
> 仓库只交付脚本，不会替你注册、启动或修改计划任务；以上命令必须由用户人工确认后执行。

**管理任务：**

```powershell
# 查看任务状态
Get-ScheduledTask -TaskName "WorkBuddyProxy"

# 手动启动
Start-ScheduledTask -TaskName "WorkBuddyProxy"

# 停止任务
Stop-ScheduledTask -TaskName "WorkBuddyProxy"

# 删除任务
Unregister-ScheduledTask -TaskName "WorkBuddyProxy" -Confirm:$false
```

### 方案三：NSSM 注册为 Windows 服务（最稳定）

[NSSM](https://nssm.cc/) 可以将任意程序注册为 Windows 服务，支持自动重启、日志记录等。

```powershell
# 1. 下载 NSSM（https://nssm.cc/download）
# 2. 解压后将 nssm.exe 放到 PATH 目录（如 C:\Windows）

# 3. 安装服务（交互式配置）
nssm install WorkBuddyProxy

# 在弹出的界面中配置：
#   Path:              C:\Users\<用户名>\AppData\Local\Programs\Python\Python312\python.exe
#   Startup directory: C:\path\to\workbuddy-proxy
#   Arguments:         server.py
#   在 "Exit actions" 标签页，设置 Restart delay: 5000 (毫秒)

# 4. 启动服务
nssm start WorkBuddyProxy
```

**管理服务：**

```powershell
nssm status WorkBuddyProxy    # 查看状态
nssm restart WorkBuddyProxy   # 重启
nssm stop WorkBuddyProxy      # 停止
nssm remove WorkBuddyProxy    # 删除服务
```

### 方案四：PM2 守护（适合已用 Node 生态、要崩溃自拉起）

[PM2](https://pm2.keymetrics.io/) 可守护任意脚本，本仓库提供 `ecosystem.config.cjs`。

**1. 安装 PM2（仅需一次）**

```powershell
npm install -g pm2
```

**2. 在项目根目录启动（后台 + 自动重启）**

```powershell
cd C:\path\to\workbuddy-proxy
pm2 start ecosystem.config.cjs
```

**Windows 说明**：PM2 直接以 `python` 作解释器时，若 PATH 里是 **pyenv-win 的 `python.bat`**，子进程可能起不来。仓库已用 **`cmd.exe /c run-proxy.cmd`** 启动（与双击 `start.bat` 等价）。若仍异常，可编辑 `run-proxy.cmd`，把其中的 `python` 改成你的 **`python.exe` 绝对路径**（例如 `pyenv which python` 的输出）。

**macOS / Linux**：使用 `run-proxy.sh`（`bash` 执行）；若 `python3` 不在 PATH，请编辑该脚本。

端口、API Key 等仍由根目录 **`.env`**（或环境变量）提供，与直接运行 `python server.py` 一致。

**3. 常用命令**

| 操作 | 命令 |
|------|------|
| 查看进程 | `pm2 ls` |
| 查看日志 | `pm2 logs workbuddy-proxy` |
| 重启 | `pm2 restart workbuddy-proxy` |
| 停止 | `pm2 stop workbuddy-proxy` |
| 取消守护 | `pm2 delete workbuddy-proxy` |

**4. 开机自启**

```powershell
pm2 save
pm2 startup
```

按 PM2 输出的提示执行一条命令（Windows 下通常需要**以管理员身份**打开终端）。完成后，登录 Windows 时会自动拉起已 `save` 的进程列表。

> **注意**：PM2 不会替你安装 Python 依赖；首次部署请先 `pip install -r requirements.txt`，并准备好 `data\token.json`（或调试模式 + `extract_token.py --save`）。

---

## 日常使用

### Docker 方式

服务配置了 `restart: unless-stopped`，**开机后 Docker Desktop 自动启动容器**，无需手动操作。

| 操作 | 命令 |
|------|------|
| 查看状态 | `docker ps` |
| 查看日志 | `docker compose logs -f` |
| 重启 | `docker compose restart` |
| 停止 | `docker compose down` |
| 启动 | `docker compose up -d` |

### Python 直接运行方式

| 操作 | Windows | macOS |
|------|---------|-------|
| 启动 | 双击 `start.bat` / `.\start.ps1` / `python server.py` | `./start.sh` / `python3 server.py` |
| 停止 | `Ctrl+C` | `Ctrl+C` |

### PM2 方式

| 操作 | 命令 |
|------|------|
| 启动 | `pm2 start ecosystem.config.cjs` |
| 状态 / 日志 | `pm2 ls` / `pm2 logs workbuddy-proxy` |
| 重启 / 停止 | `pm2 restart workbuddy-proxy` / `pm2 stop workbuddy-proxy` |

---

## Token 管理

### Token 有效期

| Token | 有效期 | 说明 |
|-------|--------|------|
| Access Token | 以 JWT `exp` 为准；当前观测约 30 天 | 主认证令牌；`/health` 返回 `token_exp` 和 `days_remaining` |
| Refresh Token | 可能为空 | 有值时优先用于续期；为空时只能通过本机 WorkBuddy CDP 重新提取 |

### 自动续期

代理每 10 秒检查一次恢复条件：重新读取 `data/token.json`，必要时使用 Refresh Token 或本机 CDP 获取新 Token。外部执行 `extract_token.py --save` 后不必重启 proxy，最多 10 秒恢复 `ready`。

Token 剩余不足 3 天时，`/health.status` 变为 `warning`，并且日志每小时最多告警一次。连续 3 次恢复失败会原子写入 `data/ALERT`；恢复后自动清除该活动告警文件。

### token 断链自救 SOP

Windows 上按以下顺序执行，脚本不会结束或重启 WorkBuddy：

1. **保存工作，从系统托盘退出 WorkBuddy。** 不要使用强制结束进程；只有用户确认退出后，调试启动参数才能生效。
2. **用调试端口重新启动 WorkBuddy。** 在仓库根目录双击 `start-wb-debug.bat`，或运行：

   ```powershell
   .\start-wb-debug.bat
   ```

   若 9222 被其他程序占用，可改用 `start-wb-debug.bat 9223`；同时把 `.env` 的 `CDP_URL` 改为 `http://127.0.0.1:9223`，下一步也加 `--port 9223`。`token.json` 可以热加载，但 `.env` 只在 proxy 启动时读取，因此修改 `CDP_URL` 后需要重启 **proxy/watchdog（不是 WorkBuddy）** 一次。

3. **重新提取并保存。** 新开 PowerShell，在仓库根目录运行：

   ```powershell
   python .\extract_token.py --save
   ```

   等待最多 10 秒后检查：

   ```powershell
   Invoke-RestMethod http://127.0.0.1:19090/health
   ```

   目标为 `status=ok`、`has_token=true`、`expired=false`、`state=ready`、`retrying=false`。随后发送一次最小聊天请求确认上游认证；`/v1/models` 只返回静态列表，不能作为认证验收。

### 故障码与处理

| `last_error` | 含义 | 操作 |
|---|---|---|
| `wb_offline` | WorkBuddy 未运行 | 手动启动；需要续期时执行上方 SOP |
| `wb_no_debug_port` | WorkBuddy 在运行，但配置的 CDP 端口不可用或不是 WorkBuddy workbench | 保存工作、托盘退出，再运行 `start-wb-debug.bat` |
| `token_expired` | Token 已过期、损坏，或 CDP 没有返回新 Token | 执行上方 SOP |
| `upstream_401` | 上游在强制刷新并重试后仍拒绝 Token | 执行上方 SOP |
| `upstream_quota` | 上游额度耗尽或限流 | 检查账户配额或稍后再试；不要重复提取 Token |

> `wb_offline` / `wb_no_debug_port` 的进程区分仅在 proxy 与 WorkBuddy 同机原生运行时可靠。Docker 通过 `host.docker.internal` 访问宿主 CDP，无法探测宿主进程，因此端口不可达时按 `wb_no_debug_port` 报告并给出同一套可操作 SOP。

### 手动更新 Token（当自动续期也失效时）

上方「token 断链自救 SOP」是 Windows 的标准手动恢复流程。macOS 可按对应方式重新开启 CDP 并提取：

```bash
# 1. 以仅监听本机回环地址的调试模式启动 WorkBuddy
/Applications/WorkBuddy.app/Contents/MacOS/Electron --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222

# 2. 重新提取 Token
python3 extract_token.py --save

# 3. 等待 10 秒并验证
curl http://127.0.0.1:19090/health
```

### 如何判断 Token 是否有效

```bash
# 健康检查（Windows 用 Invoke-RestMethod，macOS 用 curl）
curl http://127.0.0.1:19090/health
```

| 返回 | 含义 |
|------|------|
| `{"status":"ok", ...}` | 正常，Token 有效 |
| `{"status":"warning", "days_remaining":2.5, ...}` | Token 剩余不足 3 天，应安排执行 SOP |
| `{"status":"degraded", "has_token":false, ...}` | 无 Token，需要提取 |
| `{"status":"degraded", "expired":true, ...}` | Token 过期；代理每 10 秒自动重试，也可立即执行 SOP |

---

## 配置项

复制 `.env.example` 为 `.env` 按需修改：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_PORT` | `19090` | 代理监听端口 |
| `PROXY_API_KEY` | `wb-proxy-key` | 客户端使用的 API Key |
| `CDP_URL` | `http://127.0.0.1:9222` | CDP 调试地址 |
| `TOKEN_WARNING_DAYS` | `3` | 少于该天数时进入 `warning` |
| `TOKEN_WARNING_LOG_INTERVAL_SECONDS` | `3600` | Token 临期日志告警最短间隔 |
| `TOKEN_RETRY_INTERVAL_SECONDS` | `10` | Token 断链后的自动恢复间隔 |
| `ALERT_FAILURE_THRESHOLD` | `3` | 连续恢复失败多少次后写 `data/ALERT` |

> User ID、Enterprise ID、Domain 会从 JWT Token **自动解析**，无需手动配置。

---

## 常见问题

**Q: 公司网络能检测到吗？**
A: 代理只是本地转发请求到 `copilot.tencent.com`，和你正常用 WorkBuddy 的网络流量完全一样。

**Q: 可以在家使用吗？**
A: 可以。`copilot.tencent.com` 是公网服务，任何网络环境都能用。

**Q: 多人能共用一个 Token 吗？**
A: 技术上可以，但不建议。每个人应该用自己的 WorkBuddy 账号提取 Token。

**Q: Docker 构建失败（TLS 证书错误）？**
A: 网络代理（如 Clash）可能干扰 Docker 拉取镜像。关闭代理后重试 `docker compose up -d --build`。

**Q: 端口 19090 被占用？**
A: 修改 `docker-compose.yml` 中的端口映射（如改为 `29090:19090`），Base URL 对应改为 `http://127.0.0.1:29090/v1`。

**Q: Windows 上 PowerShell 提示"无法加载脚本"？**
A: 执行策略限制。以管理员身份打开 PowerShell，执行：
```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**Q: Windows 任务计划程序启动后窗口一闪而过？**
A: 这是正常现象，`-WindowStyle Hidden` 参数会隐藏窗口。服务在后台运行，通过 `Invoke-RestMethod http://127.0.0.1:19090/health` 验证。
