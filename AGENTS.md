# AGENTS.md — 给 AI 编码代理的工程指南

本文件面向 Claude Code、Codex、Cursor 等编码代理。目标：拿到仓库后能**先读懂、再安全地部署/修改、最后自证**，
而不是凭猜测改代码。

## 0. 开始前

按顺序阅读：

1. `README.md` — 部署、配置、运维、故障排查。
2. `docs/ARCHITECTURE.md` — 数据流、算法、API 字段、判定逻辑。
3. `docs/AI_DEPLOY.md` — 可直接粘贴的部署提示词和验收清单（如果任务是“帮我跑起来”）。

然后执行：

```bash
pwd
ls -la
python3 --version
python3 -c "import numpy; print(numpy.__version__)" 2>/dev/null || true
command -v rtl_test || true
command -v clang || command -v gcc || true
```

不要假设端口、Python 路径、Homebrew 前缀或 SDR 数量；先探测，再执行。

## 1. 项目是什么

RTL-SDR 单接收机，2.56 MHz 带宽，一次采集同时测量 33 个 12.5 kHz 信道。
后端是单文件 `dashboard.py`（采集线程 + HTTP 服务 + SQLite），前端是 `web/` 下的原生
HTML/CSS/JS（无构建步骤、无 npm 依赖）。

**读数语义（必须保留）：**

- `delta`：信道功率相对同帧全带噪声底的中值电平（dB）。
- `level = delta - 该信道长期噪声底`：页面上的“高于噪底”。
- 两者都是**相对值，不是 dBm**。禁止在 UI/文档/接口里暗示绝对功率。
- 用差分是为了抵消 RTL-SDR 的 AGC 和温漂；不要改成绝对功率。

## 2. 环境与命令

### 安装

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

`librtlsdr` 是系统依赖：

```bash
brew install librtlsdr           # macOS
sudo apt install librtlsdr-dev   # Debian/Ubuntu
```

首次启动 dashboard 会自动编译 `.bin/iqcap`。若路径非标准：

```bash
BREW_PREFIX=/opt/homebrew python dashboard.py   # Apple Silicon 默认
BREW_PREFIX=/usr/local python dashboard.py      # Intel Mac
BREW_PREFIX=/usr CC=gcc python dashboard.py     # Linux
```

### 启动

```bash
.venv/bin/python dashboard.py --host 127.0.0.1 --port 8787
```

默认只监听本机。不要在没有告知用户的情况下改成 `0.0.0.0`；服务没有鉴权。

### 自检

```bash
python3 -m py_compile dashboard.py rtl_multi_monitor.py
node --check web/app.js          # 可选，需要 Node
bash scripts/smoke_test.sh       # 服务已启动时做端到端检查
```

### 接口抽查

```bash
curl -s http://127.0.0.1:8787/api/state | python3 -m json.tool | head -40
curl -s 'http://127.0.0.1:8787/api/chan-trend?range=60' | python3 -m json.tool | head
curl -s 'http://127.0.0.1:8787/api/chan-detail?key=Site1-11&range=900' | python3 -m json.tool | head -40
```

## 3. 文件地图

| 路径 | 作用 | 修改注意 |
| --- | --- | --- |
| `dashboard.py` | 采集、FFT、差分、状态机、SQLite、HTTP API | 主后端；改完要重启进程 |
| `web/index.html` | 页面结构、两个 Tab | 新增面板/Tab 先改这里 |
| `web/style.css` | 主题变量、布局、图表/卡片样式 | 用现有 CSS 变量，不写死深浅色 |
| `web/app.js` | 轮询、渲染、Canvas、Tab 路由 | 无构建步骤；改完 `node --check` |
| `channels.csv` | 站点/频点/控制信道表 | 频点必须落在采集带宽内 |
| `iqcap.c` | 最小 IQ 采集器 | 一般不需要动；动完删 `.bin/iqcap` 重编译 |
| `rtl_multi_monitor.py` | 早期多信道原型 | **不是主程序**，不要在上面加功能 |
| `sdr_rssi_monitor.py` | 早期单信道原型 | **不是主程序** |
| `scripts/smoke_test.sh` | 端到端冒烟 | 新接口/新页面应补检查 |
| `deploy/` | launchd/systemd 模板 | 占位符需替换 |
| `docs/ARCHITECTURE.md` | 算法/接口详细说明 | 改算法时同步更新 |

数据库 `rssi.db`、`.bin/iqcap`、`.venv/`、日志都是运行时产物，已写入 `.gitignore`。

## 4. 不可破坏的约束

1. **单实例 SDR**：同一台机器只能有一个 `dashboard.py` / `iqcap` 进程。测试前先确认，
   不要为了“重启”同时留两个进程。`pgrep -fl 'dashboard.py|iqcap'`。
2. **AGC 必须开启**：`iqcap` 参数为 `192 1 0 <center> <rate>`；关闭 AGC 会严重削顶。
3. **差分测量**：不要用绝对 FFT 功率替换 `delta`；不要用 dBm 标注。
4. **空闲 ≠ 故障**：无发射时只能显示“无活动 / 无有效发射 / 样本不足”，不能自动判故障。
5. **阈值**：站点判定 −3 dB 提醒、−6 dB 告警；单信道分析同样。不要随意改。
6. **前端中文**：现有 UI 是中文工业看板风格；新增文案保持简洁、可操作、别夸大结论。
7. **无鉴权**：默认只绑定 `127.0.0.1`；不要默认开放公网。
8. **数据量**：`samples` 无自动清理，满负荷约 100 MB/天；不要写无限增长的日志。
9. **不伪造数据**：测试时如果 SDR 不可用，明确报告“设备离线/无数据”，不要用随机数冒充真实采集。

## 5. 常见改动怎么做

### 改前端布局/文案

1. 只改 `web/index.html`、`web/style.css`、`web/app.js`。
2. 复用主题变量（`--panel`、`--fg`、`--warn`、`--grid` 等）。
3. 保留 250 ms / 2 s / 5 s 的轮询节奏，除非有明确理由。
4. 检查 1400 / 1000 / 760 px 宽度无横向滚动。
5. `node --check web/app.js`，刷新页面确认两个 Tab 都正常。

### 加一个后端接口

1. 在 `Engine` 中实现取数/计算方法，注意 `lock` / `hlock` / `db_lock` 的职责。
2. 在 `Handler.do_GET` 的 `path == "/api/..."` 分支解析参数、校验、返回 JSON。
3. 需要长历史时查 SQLite，注意 `(key, ts)` 索引；不要在主循环里阻塞。
4. 前端 `fetch` 加 `{ cache: 'no-store' }`，结果做字段防御。
5. 更新 `docs/ARCHITECTURE.md` 和 `scripts/smoke_test.sh`。

### 改判定逻辑

1. 先读 `docs/ARCHITECTURE.md` 第 3–6 节和自己要改的函数。
2. 保留“滞后确认”和“空闲不算故障”的原则。
3. 用历史数据回放/接口抽查，确认不会因重启或噪声抖动误报。
4. 同步更新页面说明文字（`web/index.html`）和文档，避免 UI 与算法不一致。

### 换频段

1. 修改 `dashboard.py` 顶部 `FC` / `FS`（以及必要时的 `N` / `HALF`）。
2. 确认 `channels.csv` 所有 `downlink_mhz` 在 `FC ± FS/2` 内，否则会被过滤。
3. 确认天线和 SDR 调谐范围支持；重新启动并看 `/api/state` 的 `device`、`channels`。

### 部署成服务

- macOS：`deploy/com.radio.dashboard.plist.example`，替换占位符后放 `~/Library/LaunchAgents/`。
- Linux：`deploy/radio-dashboard.service`，替换路径/用户后放 `/etc/systemd/system/`。
- 远程访问优先 SSH 隧道，不要直接 `--host 0.0.0.0` 暴露。

## 6. 验证与完成标准（Definition of Done）

任何改动至少完成：

- [ ] `python3 -m py_compile dashboard.py` 通过。
- [ ] `node --check web/app.js` 通过（有 Node 时）。
- [ ] 服务能启动，`/api/state` 返回 200 且 `channels` 数量与 `channels.csv` 一致。
- [ ] `bash scripts/smoke_test.sh` 全部 PASS（SDR 不可用时明确说明哪一步无法验证）。
- [ ] 页面两个 Tab 正常：总览 33 张卡、分析页曲线/筛选/时间范围/悬停无 JS 报错。
- [ ] 1400 / 1000 / 760 px 宽度无横向溢出。
- [ ] 改动涉及的文档（README / ARCHITECTURE / AGENTS）同步更新。
- [ ] 如果重启过后端，确认旧进程/旧 `iqcap` 没有残留。

## 7. 报告给用户的格式

部署任务报告：

```text
环境：OS/架构、Python、numpy、SDR、librtlsdr
启动命令：...
访问地址：http://127.0.0.1:8787
验收：smoke_test 6/6 PASS；/api/state device=true channels=33
未验证/风险：...
```

开发任务报告：

```text
改了什么：...
为什么：...
验证：py_compile / node --check / smoke_test / 页面检查
影响范围：...
已知限制：...
```

不要只说“已完成”；给出可复现的命令和实际结果。

## 8. 相关文档

- [README.md](README.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/AI_DEPLOY.md](docs/AI_DEPLOY.md)
- [deploy/](deploy/)
