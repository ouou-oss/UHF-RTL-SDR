# 架构与算法说明

本文给开发者和 AI 代理阅读：说明数据从天线到页面的完整链路、每个 dB 数字的来源、
判定阈值、接口字段和已知限制。部署步骤见 [README](../README.md)，
AI 代理约定见 [AGENTS.md](../AGENTS.md)。

## 1. 进程拓扑

```text
RTL-SDR (USB)
   │  raw IQ, 8-bit
   ▼
.bin/iqcap              最小 C 采集器：设置中心频率/采样率/增益/AGC，输出原始 IQ
   │  stdout (binary)
   ▼
dashboard.py Engine     单线程：FFT → 信道功率 → 差分电平 → 状态机 → SQLite
   │  memory state                              │
   ├──► /api/state       250 ms 前端轮询          └──► rssi.db
   ├──► /api/chan-trend  2 s 前端轮询（内存 30 分钟）
   └──► /api/chan-detail 5 s 前端轮询（SQLite 历史）
   │
   ▼
web/index.html + style.css + app.js   浏览器渲染（Canvas，无前端框架）
```

同一支 SDR 只能由一个 `dashboard.py` / `iqcap` 进程占用。两个实例会互相抢 USB，
表现为 `usb_claim_interface error -3` 或设备离线。

## 2. 采集与 FFT

`iqcap.c` 直接调用 librtlsdr，避免 `rtl_power` 无法控制 RTL2832U 内部 AGC 的问题。

```text
N   = 8192
FS  = 2.56 MHz
FC  = 422.23 MHz
bin = FS / N ≈ 312.5 Hz
HALF= 6.25 kHz（12.5 kHz 信道的一半）
```

每次处理：

1. IQ 按 8192 点分帧，加 Hann 窗，做 FFT。
2. 取每帧功率谱，按时间累加到一次 tick。
3. 信道功率 `pch` = 该信道 ±6.25 kHz 内所有 bin 的功率和。
4. 全带噪声底 `floor` = 本帧功率谱的中位数。
5. `delta = 10·log10(pch / floor)`，即该信道相对同帧全带噪声底的电平（dB）。
6. 长期噪声底 `floor_db[k]` = 该信道 `delta` 在最近 1 小时窗口的 **10 分位**，每 60 tick 更新。
7. `level = delta - floor_db[k]`，即页面上的“高于噪底”读数。

为什么用差分：

- 电视棒没有 TCXO，绝对电平随温度/时间漂移，且 FC0012 的调谐器增益档基本无效。
- 信道功率和全带噪声底共享同样的 AGC/温漂，相减可以抵消大部分共模变化。
- 实测短时 sigma 约 0.1 dB 量级，足以观察 3 dB 以上的长期变化。

**所有 `delta` / `level` 都是相对值，不是 dBm，也没有做绝对功率校准。**

## 3. 载波状态机

每个信道独立维护 `on_cnt` / `off_cnt`：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `ON_DB` | 8 dB | `level` 高于此值视为“有载波”候选 |
| `OFF_DB` | 4 dB | 低于此值视为“载波消失”候选 |
| `ON_TICKS` | 4 | 连续命中后确认 active |
| `OFF_TICKS` | 8 | 连续命中后确认 idle |

按标称 4 Hz tick，确认时间约 1 秒 / 2 秒；实际 tick 速率受 CPU 和 SDR 读取影响，
可能只有约 2 Hz，对应时间会变长。

`duty` = 最近 120 秒 active 的比例，仅用于展示，不参与告警。

### 邻道判别

若同一次 tick 中，存在与本信道相差 ≤25 kHz、且 `level ≥ 28 dB` 的其它 active 信道，
则本信道标记 `unreliable: true`。前端显示“邻道”，表示读数可能被强邻道压缩/泄漏带偏。
这是提示，不会自动改数。

## 4. 站点级判定

每个站点只看**控制信道**（`is_control=1`）：

1. 只有 `level > ON_DB` 的样本会进入该信道的基线队列 `chan_on`（约 30 分钟窗口）。
2. 基线 `base` = 队列中值，样本数需 ≥ `CHAN_MIN`（60）。
3. 当前值 `current` = 最近 60 秒内该信道 `level` 的最大值；要求 `> ON_DB` 才视为有发射。
4. 在所有“有发射”的控制信道里，选 `current` 最大的作为主导信道。
5. `dev = current - base`：
   - `dev ≤ -6` → 目标状态 bad；
   - `dev ≤ -3` → 目标状态 warn；
   - 否则 ok。
6. 状态升级需连续 15 秒确认，恢复需连续 30 秒确认（按标称 4 Hz 计数）。
7. 站点整体状态取所有站点、设备状态、削顶和问题列表的最高级别。

特殊情况：

- 没有控制信道，或所有控制信道基线样本不足 → `init`（基线建立中）。
- 有基线但没有 active 控制信道 → `idle`（无活动，不代表故障）。
- `clip > 2%` → 数据质量下降，因为削顶会让读数失真。
- 设备断开 → `bad`，旧快照被替换为“设备离线”，前端另有 8 秒数据过期保护。

## 5. 前端

### 5.1 文件职责

| 文件 | 职责 |
| --- | --- |
| `web/index.html` | 页面骨架、两个 Tab、说明文字 |
| `web/style.css` | 主题变量、响应式布局、卡片/图表/分析页样式 |
| `web/app.js` | 轮询、状态渲染、总览 sparkline、分析页 Canvas、Tab 路由 |

没有打包器、没有 npm 依赖；修改后刷新浏览器即可。浏览器通过 `/`、`/style.css`、
`/app.js` 访问静态文件。

### 5.2 Tab 与状态

- `#overview`：总览。
- `#channel`：信道分析；选中的频点存在 `localStorage["radio-detail-key"]`。
- 分析时间范围存在 `localStorage["radio-detail-range"]`，可选 900 / 1800 / 3600 / 10800 秒。
- 切换 Tab 使用 `history.replaceState`，刷新后仍停留在当前 Tab。
- 从总览点击任一信道卡片会写入选中频点并跳到 `#channel`。

### 5.3 轮询节奏

| 数据 | 频率 | 用途 |
| --- | --- | --- |
| `/api/state` | 250 ms | 状态卡、站点卡、总览信道读数 |
| `/api/chan-trend` | 2 s | 总览小卡片曲线 |
| `/api/chan-detail` | 5 s（仅分析 Tab） | 单信道大图与判定 |

视图隐藏时会跳过总览 Canvas 重绘；切回总览会立即补画。

### 5.4 Canvas 绘制

- 所有图表按 `devicePixelRatio` 缩放，CSS 尺寸与画布像素分离。
- 总览 sparkline：每 2 秒一个桶内峰值，纵轴按各卡片窗口自适应，右上角标注峰值。
- 分析图：
  - 只画 `level ≥ 6 dB` 的发射样本；
  - 细点是分桶发射峰值（桶数上限 240）；
  - 粗蓝线是这些峰值的滑动中值（窗口为时间范围的 6%，至少 120 秒）；
  - 黄色虚线是“早先基线”，右侧阴影是最近观察窗；
  - 悬停显示时间、峰值、趋势和桶内样本数。

## 6. 单信道分析

`GET /api/chan-detail?key=<key>&range=<seconds>`：

1. 从 SQLite 读取该信道 `[to-range, to]` 的 `(ts, level)`；DB 不可用时退回内存 30 分钟窗口。
2. 有效发射样本：`level ≥ 6 dB`（比总览 active 的 8 dB 更宽松，尽量捕捉弱信号）。
3. 近期窗口：`recent_sec = min(300, max(120, range × 0.2))`。
   - 15m → 3 分钟
   - 30m / 1h / 3h → 5 分钟
4. 基线窗口：`[from, to - recent_sec)`；分别计算两个窗口内有效样本的中值。
5. `delta = recent - baseline`：
   - 基线样本 <30 或近期样本 <10 → `insufficient`；
   - 基线有、近期无有效样本 → `idle`；
   - `delta ≤ -6` → `bad`；
   - `delta ≤ -3` → `warn`；
   - 其它 → `ok`。
6. 曲线点：自适应桶宽（最多 240 桶），每桶取 `level` 最大值，记录桶内有效样本数。
7. 返回 `duty`、`samples`、`last`（最近一次有效发射）辅助判断。

判定只描述“最近观察窗相对更早窗口的变化”。同一频点可能多台设备轮流使用，
中值下降不等于基站功率下降，页面会明确提示结合现场确认。

## 7. SQLite

```sql
CREATE TABLE samples(
  ts REAL,       -- Unix 时间戳
  key TEXT,      -- "Site1-11"
  mhz REAL,      -- 下行频率
  delta REAL,    -- 相对全带噪声底 dB
  level REAL     -- 相对该信道长期噪声底 dB
);
CREATE INDEX idx_samples_key_ts ON samples(key, ts);
```

- 写入：每 `DB_EVERY=4` 个 tick 一次，33 行/次；有 `db_lock` 保护。
- 读取：`/api/chan-detail` 按 `key + ts` 查询，有索引；最长 6 小时。
- 启动回读：最近 3 小时用于噪声底，最近 30 分钟用于 sparkline 曲线。
- 无自动清理；容量估算和清理命令见 [README](../README.md#数据与维护)。

## 8. API 契约

### `GET /api/state`

```json
{
  "ts": 1795402705.1,
  "device": true,
  "clip": 0.0,
  "rate": 2560000.0,
  "uptime": 171195.0,
  "channels": [
    {
      "key": "Site1-1", "site": "Site1", "rep": "1",
      "mhz": 421.1125, "ctrl": true,
      "delta": 29.9, "level": 0.35,
      "active": false, "duty": 0.0, "unreliable": false
    }
  ],
  "verdict": {
    "level": "ok",
    "title": "运行正常",
    "sub": "……",
    "active": 12,
    "total": 33,
    "sites": [
      {
        "site": "Site1", "state": "ok", "text": "正常",
        "inst": 12.0, "inst1": 15.0,
        "active": 5, "clean": 5, "total": 12,
        "peak": 15.0, "base": 12.0, "dev": 0.4,
        "ctrlKey": "Site1-1", "ctrlMhz": 421.1125
      }
    ],
    "issues": [
      {"t": 1795400000.0, "site": "Site1", "kind": "warn", "text": "……"}
    ]
  }
}
```

前端在 `ts` 超过 8 秒未更新时，会强制显示“数据停止更新”。

### `GET /api/chan-trend?range=300`

`range` 默认 300 秒，服务端会限制在内存窗口（约 30 分钟）内。

```json
{
  "ts": 1795402705.1,
  "from": 1795402405.1,
  "to": 1795402705.1,
  "chan_trend": {
    "Site1-1": [[1795402405.1, 2.0], [1795402407.2, 1.4]]
  }
}
```

数组元素为 `[时间戳, level]`，每桶取峰值，桶宽自适应（最多约 140 点）。

### `GET /api/chan-detail?key=Site1-11&range=3600`

```json
{
  "ts": 1795402705.1,
  "from": 1795399105.1,
  "to": 1795402705.1,
  "key": "Site1-11",
  "site": "Site1",
  "rep": "11",
  "mhz": 422.8625,
  "ctrl": false,
  "points": [[1795401887.6, 25.6, 2]],
  "level": "ok",
  "text": "最近 5 分钟与早先基本持平（变化 -1.2 dB）",
  "baseline": 26.8,
  "baseline_n": 88,
  "recent": 25.6,
  "recent_n": 16,
  "recent_peak": 27.4,
  "recent_sec": 300,
  "delta": -1.2,
  "duty": 12.9,
  "samples": 809,
  "last": {"t": 1795402637.0, "v": 23.4}
}
```

- `points`：`[时间戳, 桶内 level 峰值, 桶内有效样本数]`。
- `baseline` / `recent`：中值；为 `null` 表示样本不足或无发射。
- 未知 `key` 返回 404。

## 9. 性能与限制

- 33 信道 FFT 处理在普通 Apple Silicon 上可跑约 2 Hz tick（受 CPU/SDR 影响）。
- 内存曲线窗口 30 分钟；更长历史只在 SQLite 中，分析接口按需聚合。
- 单接收机只能看到“本地天线处的信号”，不能区分基站功率下降、天线劣化、遮挡和不同设备。
- 没有绝对功率校准和方向信息。
- 空闲信道没有可测信号；页面显示“无有效发射”是信息不足，不是自动告警。
- 没有鉴权和多用户；默认只监听本机。

## 10. 扩展点

- **换频段**：修改 `FC` / `FS`，确认 `channels.csv` 频点落在采集带宽内；必要时调整 `HALF`。
- **加接口**：在 `Engine` 里实现数据方法，在 `Handler.do_GET` 中注册路由，再在 `web/app.js` 轮询。
- **加页面**：新增 Tab 按钮和视图容器，在 `app.js` 的 `setTab` 中切换；静态文件无构建步骤。
- **改判定阈值**：`WARN_DB` / `BAD_DB`、`ON_DB` / `OFF_DB`、`DETAIL_ACTIVE_DB`；改动前先确认现场噪声分布。
- **数据保留**：建议在部署层用 cron/systemd timer 定期清理 `samples`，而不是在采集线程里做阻塞操作。
