# 用 AI 快速部署 / 开发

本页给“把仓库交给 AI 代理”的场景使用。目标不是让 AI 自由发挥，而是给它边界、
验收标准和排障路径，避免它改错频率、占错进程或伪造数据。

配套阅读： [README](../README.md)、[AGENTS.md](../AGENTS.md)、
[ARCHITECTURE](ARCHITECTURE.md)。

## 1. 部署前先准备这些信息

把下面的信息补充完整再交给 AI，能显著减少来回确认：

- 机器：macOS（Apple Silicon / Intel）或 Linux 发行版。
- SDR：型号、数量、是否已插好、天线接在哪个频段。
- 目标频段：默认 421–423 MHz；如果不同，提供中心频率和信道表。
- 运行方式：临时 `python dashboard.py`，还是 launchd / systemd 常驻。
- 访问方式：只本机，还是需要 SSH 隧道 / 局域网 / 反向代理。
- 端口：默认 8787，是否被占用。
- 是否有历史 `rssi.db` 需要保留。

## 2. 可直接粘贴给 AI 的部署提示词

> 把 `<PROJECT_DIR>` 等占位符替换成实际值，再把整段发给 Claude Code / Codex / Cursor 等代理。

```text
你是本项目的部署助手。请在当前机器上把 RTL-SDR 信号监控服务跑起来，并给出可复现的验证结果。

项目目录：<PROJECT_DIR>
目标：浏览器能打开 http://127.0.0.1:8787；/api/state 显示 device=true 且 33 个信道；
      /api/chan-detail 能返回单信道分析数据。

开始前必须：
1. 阅读 README.md、AGENTS.md、docs/ARCHITECTURE.md。
2. 探测环境：uname -a、python3 --version、numpy 是否可用、rtl_test 是否存在、
   clang/gcc 是否存在、端口 8787 是否被占用、是否已有 dashboard.py/iqcap 进程。
3. 不要假设 Homebrew 前缀；Apple Silicon 通常是 /opt/homebrew，Intel 是 /usr/local，Linux 是 /usr。

执行步骤：
1. 创建 .venv，安装 requirements.txt。
2. 安装 librtlsdr（macOS 用 brew，Debian/Ubuntu 用 apt）。
3. 启动 `python dashboard.py --host 127.0.0.1 --port 8787`，首次会自动编译 .bin/iqcap。
   如果编译失败，按错误设置 BREW_PREFIX / CC 后重试，并把完整错误贴出来。
4. 启动后运行 `bash scripts/smoke_test.sh`，要求 6 项全部 PASS。
5. 额外检查：
   - curl -s http://127.0.0.1:8787/api/state：device 必须为 true，channels 长度必须等于 channels.csv 的行数。
   - 打开 /api/chan-detail?key=<第一个信道>&range=900：points 可以是空数组（可能没有发射），
     但 HTTP 必须是 200 且 level/text 字段存在。
   - 如果机器有浏览器/无头 Chrome，确认页面总览有 33 张卡、信道分析 Tab 能切换、无 JS 报错。
6. 如果需要常驻，按 deploy/ 下的模板配置 launchd 或 systemd，并说明如何停止/重启/看日志。

硬性约束：
- 同一支 SDR 只能有一个采集进程；重启前先停旧进程，不要在后台留两个。
- 不要修改 channels.csv、FC/FS、判定阈值；如需修改，先列出原因并等我确认。
- 不要把服务绑定到 0.0.0.0 或公网；没有鉴权。
- 不要用随机数/模拟数据冒充真实采集；设备不可用就如实报告。
- 不要删除 rssi.db 或历史数据，除非我明确要求。

最后按以下格式报告：
环境：OS/架构、Python、numpy、SDR、librtlsdr
启动命令：
访问地址：
验收结果：smoke_test 通过数、/api/state 的 device 和 channels、页面检查结果
未验证项 / 风险：
```

## 3. 人工验收清单

部署完成后，逐项确认：

```bash
# 1. 服务和端口
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8787/

# 2. 设备与信道
curl -s http://127.0.0.1:8787/api/state \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("device",d["device"],"channels",len(d["channels"]),"verdict",d["verdict"]["level"])'

# 3. 全量曲线
curl -s 'http://127.0.0.1:8787/api/chan-trend?range=300' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("chan_trend",len(d["chan_trend"]))'

# 4. 单信道分析（把 key 换成 /api/state 里的第一个 key）
curl -s 'http://127.0.0.1:8787/api/chan-detail?key=Site1-11&range=900' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["key"],d["level"],d["text"],len(d["points"]))'

# 5. 一键冒烟
bash scripts/smoke_test.sh
```

预期：

- `device True`、`channels 33`。
- `/api/chan-trend` 返回 33 个 key。
- `/api/chan-detail` 返回 200；`points` 可能为空（该频点最近没发射），`level` 为
  `ok / warn / bad / idle / insufficient` 之一。
- `smoke_test.sh` 全部 PASS。

浏览器验收：

1. 总览顶部显示设备 ONLINE，站点卡片有读数。
2. “全部信道明细”有 33 张卡片，每张有当前 dB 和曲线。
3. 顶部“信道分析”Tab 能打开；左侧信道列表可搜索、可切“控制/业务”。
4. 选择任一有发射的频点，能看到大图、基线、趋势、统计和“信号稳定/偏低/明显下降”结论。
5. 切 15m / 30m / 1h / 3h 不报错；悬停曲线有提示。
6. 浏览器控制台无红色报错。

## 4. 常见部署问题

| 报错 / 现象 | 处理 |
| --- | --- |
| `No supported devices found` / 找不到 SDR | 电视棒没插好、USB 线/口问题、Linux 权限不足，或驱动未加载。先 `rtl_test -t`；Linux 检查 `lsusb` 和 plugdev/udev。 |
| `usb_claim_interface error -3` / `device busy` | 已有进程占用 SDR。`pgrep -fl 'dashboard.py|iqcap'`，停掉旧进程后再启动；同一支 SDR 只能一个实例。 |
| `Kernel driver is active`（Linux） | DVB 驱动占用。按 rtl-sdr 文档卸载/黑名单 `dvb_usb_rtl28xxu`，或先 `rtl_test -t` 验证。 |
| `ModuleNotFoundError: numpy` | 没进 venv 或没装依赖：`. .venv/bin/activate && pip install -r requirements.txt`。 |
| `clang: error: librtlsdr not found` | 没安装 `librtlsdr`，或 `BREW_PREFIX` 不对：Apple Silicon `/opt/homebrew`、Intel `/usr/local`、Linux `/usr`。 |
| `Address already in use` | 端口被占用：`lsof -i :8787`，换 `--port` 或停旧服务。 |
| 页面显示“数据停止更新” | 进程还在但 SDR 掉线/卡死：看日志、重新插拔、确认没有第二个采集进程。 |
| `CLIP` > 2% | 输入过载，读数不可信；加衰减器。不要关闭 AGC。 |
| 曲线一直“样本不足” | 该频点最近确实没有发射；不是部署失败。 |
| 编译产物缺失 | 删除 `.bin/iqcap` 后重启 dashboard 会自动重编译，或手动执行 `clang` 命令。 |

## 5. 让 AI 继续开发

部署成功后，如果要改功能，用第二个提示词：

```text
请先阅读 AGENTS.md、README.md 和 docs/ARCHITECTURE.md，再完成下面的需求：

<需求描述>

要求：
1. 先给出改动计划（涉及文件、风险、验证方式），再动手。
2. 不要改与需求无关的频率、阈值、采集参数、channels.csv。
3. 保持差分 dB 语义：不要引入 dBm，不要把“无发射”判成故障。
4. 前端改动不能引入构建步骤或 npm 依赖；复用现有主题变量和轮询节奏。
5. 完成后必须执行：
   - python3 -m py_compile dashboard.py
   - node --check web/app.js（有 Node 时）
   - bash scripts/smoke_test.sh
   - 1400/1000/760px 页面检查；两个 Tab 均无 JS 报错。
6. 更新受影响的文档（README / docs/ARCHITECTURE.md）。
7. 按 AGENTS.md 第 7 节的格式报告，附上实际命令输出。
```

## 6. 安全与边界

- 服务没有认证、没有 HTTPS、没有 CSRF 防护，定位是**本机/内网工具**。
- 远程访问优先 SSH 隧道：
  `ssh -L 8787:127.0.0.1:8787 user@host`。
- 如果需要多人访问，放在带认证的反向代理后面，并限制来源 IP。
- 不要把 `channels.csv`、截图、日志或数据库上传到公开仓库，除非确认频点信息可以公开。
- 修改采集参数前确认硬件能力；错误增益/采样率可能导致读数失真或设备不稳定。
