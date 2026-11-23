#!/usr/bin/env python3
"""
SDR数据对讲系统信号监控（后端）
==============================

启动:
    <bundled-python> dashboard.py --port 8787

后端每秒钟做三件事：
  1. 采集 33 个信道，算相对场强（差分，抗 AGC 漂移）；
  2. 判断"信号是否异常"，输出红/黄/绿结论 + 站点级状态 + 问题清单；
  3. 落库 SQLite；启动时回读最近 3 小时，用于建立基线。

判定逻辑（诚实版）：
  · 基线 = 各站点控制信道"有发射时"电平的滚动中值，需要 ≥60 个有效样本
  · 当前值 = 最近 5 分钟峰值
  · 偏差 < -3 dB 警告，< -6 dB 异常
  · 站点长时间无活动 → 灰色"无活动"，不算故障（可能只是没人用）
"""

import argparse
import json
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse, parse_qs
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
EXE = os.path.join(HERE, ".bin", "iqcap")

N = 8192
FS = 2_560_000.0
FC = 422_230_000.0
HALF = 6_250.0

TICK_HZ = 4          # 发布频率：4 次/秒
TICK_S = 1.0 / TICK_HZ
HIST_SEC = 1800      # 保留 30 分钟
HIST = HIST_SEC * TICK_HZ
NOW_WIN = 300 * TICK_HZ   # "当前电平"取最近 5 分钟峰值
BASE_MIN = 120       # 站点峰值基线最少样本数
CHAN_MIN = 60        # 单信道基线最少样本数（约 15 秒发射）
ON_TICKS = 4         # 连续 1 秒高于阈值才算载波
OFF_TICKS = 8        # 连续 2 秒低于阈值才算消失
CH_TREND_POINTS = 140  # 每个信道曲线最多返回的桶数
DETAIL_POINTS = 240     # 单信道分析曲线最多返回的桶数
DETAIL_ACTIVE_DB = 6.0  # 分析时把高于噪底 6 dB 的样本视为"有发射"
DETAIL_RECENT_SEC = 300 # 近期窗口（秒），不足时按范围缩短
DETAIL_MIN_BASE = 30    # 基线窗口最少有效样本
DETAIL_MIN_RECENT = 10  # 近期窗口最少有效样本
DB_EVERY = 4         # 数据库 1 秒写一次
WARN_DB, BAD_DB = 3.0, 6.0
ON_DB, OFF_DB = 8.0, 4.0
REST_DUTY = 0.6
ADJ_MHZ = 0.025      # 25 kHz 内邻道活跃且足够强 → 本读数可能受压缩影响
ADJ_STRONG = 28.0    # 实测：邻道电平 <25 dB 时泄漏约 0.2 dB，>30 dB 时约 1.7 dB


def _fmt_span(sec: float) -> str:
    if sec >= 3600:
        return "%.0f 小时" % (sec / 3600.0)
    return "%.0f 分钟" % (sec / 60.0)


def build_tool() -> None:
    src = os.path.join(HERE, "iqcap.c")
    if os.path.exists(EXE) and os.path.getmtime(EXE) > os.path.getmtime(src):
        return
    os.makedirs(os.path.dirname(EXE), exist_ok=True)
    cc = os.environ.get("CC", "clang")
    brew = os.environ.get("BREW_PREFIX", "/opt/homebrew")
    subprocess.run([cc, "-O2", "-o", EXE, src,
                    "-I%s/include" % brew, "-L%s/lib" % brew, "-lrtlsdr"], check=True)


def load_channels(path: str):
    import csv
    out = []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            mhz = float(r["downlink_mhz"])
            if abs(mhz * 1e6 - FC) > FS / 2 - 20_000:
                continue
            out.append({
                "key": "%s-%s" % (r["site"], r["repeater_id"]),
                "site": r["site"],
                "rep": r["repeater_id"],
                "mhz": mhz,
                "ctrl": r["is_control"] == "1",
            })
    out.sort(key=lambda c: c["mhz"])
    return out


class Engine(threading.Thread):
    def __init__(self, channels, db_path=""):
        super().__init__(daemon=True)
        self.channels = channels
        self.sites = sorted({c["site"] for c in channels})
        self.db_path = db_path
        self.lock = threading.Lock()
        self.hlock = threading.Lock()   # 保护 self.hist：单一写线程 + 多个 HTTP 读线程
        self.state = {}
        self.floor_d = {c["key"]: deque(maxlen=4 * 3600) for c in channels}  # 1 小时 delta
        self.floor = {c["key"]: None for c in channels}
        self.hist = {c["key"]: deque(maxlen=HIST) for c in channels}
        self.inst_hist = {s: deque(maxlen=6) for s in self.sites}
        self.act_hist = {c["key"]: deque(maxlen=HIST) for c in channels}
        self.on_cnt = {c["key"]: 0 for c in channels}
        self.off_cnt = {c["key"]: 0 for c in channels}
        self.chan_on = {c["key"]: deque(maxlen=4 * 1800) for c in channels if c["ctrl"]}  # 30 分钟活跃样本
        self.dev_state = {x: 0 for x in self.sites}
        self.dev_up = {x: 0 for x in self.sites}
        self.dev_dn = {x: 0 for x in self.sites}
        self.events = deque(maxlen=200)
        self.tick_no = 0
        self.t0 = time.time()
        self.device = False
        self.db = None
        self.db_lock = threading.Lock()
        if db_path:
            self.db = sqlite3.connect(db_path, check_same_thread=False)
            self.db.execute("CREATE TABLE IF NOT EXISTS samples("
                            "ts REAL, key TEXT, mhz REAL, delta REAL, level REAL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_samples_key_ts "
                            "ON samples(key, ts)")
            self.db.commit()
            self._seed()

    # ---- 启动时回读历史，让基线和趋势不因重启清零 ----
    def _seed(self):
        cutoff = time.time() - 3 * 3600
        rows = self.db.execute(
            "SELECT ts,key,delta FROM samples WHERE ts>? ORDER BY ts", (cutoff,)).fetchall()
        if not rows:
            return

        # 1) 逐信道噪声底：历史 delta 的 10 分位
        hist = {}
        for _, k, d in rows:
            if k in self.floor_d:
                hist.setdefault(k, []).append(d)
        for k, vals in hist.items():
            self.floor_d[k].extend(vals)
            self.floor[k] = float(np.percentile(np.asarray(vals, dtype=float), 10))

        # 2) 逐信道曲线：回读最近 30 分钟，level = delta - 该信道噪声底
        cutoff30 = time.time() - HIST_SEC
        seeded = 0
        for ts, k, d in rows:
            if ts >= cutoff30 and k in self.hist and self.floor.get(k) is not None:
                with self.hlock:
                    self.hist[k].append((ts, d - self.floor[k]))
                seeded += 1

        self.event("已回读 %d 条历史采样，%d 个信道噪声底已建立，%d 个曲线点已恢复"
                   % (len(rows), len(hist), seeded), "info")

    def event(self, msg, kind="info", site=""):
        self.events.appendleft({"t": time.time(), "msg": msg, "kind": kind, "site": site})

    def run(self):
        win = np.hanning(N).astype(np.float32)
        freqs = FC + np.fft.fftshift(np.fft.fftfreq(N, 1.0 / FS))
        masks = {c["key"]: np.abs(freqs - c["mhz"] * 1e6) < HALF for c in self.channels}
        prev = {c["key"]: False for c in self.channels}
        while True:
            proc = subprocess.Popen([EXE, "192", "1", "0", str(int(FC)), str(int(FS))],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.device = True
            self.event("设备已连接", "ok")
            buf, acc, nacc, clip, tot = bytearray(), None, 0, 0, 0
            last = time.time()
            try:
                while True:
                    chunk = os.read(proc.stdout.fileno(), 1 << 18)
                    if not chunk:
                        raise IOError("device closed")
                    buf += chunk
                    nf = len(buf) // (2 * N)
                    if nf == 0:
                        continue
                    use = nf * 2 * N
                    a = np.frombuffer(bytes(buf[:use]), np.uint8).astype(np.float32) - 127.5
                    del buf[:use]
                    z = a.reshape(nf, N, 2)
                    iq = z[:, :, 0] + 1j * z[:, :, 1]
                    clip += int(np.sum((np.abs(iq.real) > 126) | (np.abs(iq.imag) > 126)))
                    tot += iq.size
                    spec = np.fft.fftshift(np.fft.fft(iq * win, axis=1), axes=1)
                    p = spec.real ** 2 + spec.imag ** 2
                    s = p.sum(axis=0)
                    acc = s if acc is None else acc + s
                    nacc += nf
                    if time.time() - last < TICK_S:
                        continue
                    last = time.time()
                    self._tick(acc / nacc, masks, clip, tot, prev)
                    acc, nacc, clip, tot = None, 0, 0, 0
            except Exception as e:
                self.device = False
                self.event("设备中断: %s" % e, "err")
                # 必须发布“设备离线”状态，否则 /api/state 会一直返回掉线前的旧快照，
                # 看板把 17 小时前的数据当成实时数据显示。
                with self.lock:
                    st = dict(self.state) if self.state else {}
                    st.update({
                        "ts": time.time(), "device": False,
                        "clip": 0.0, "rate": FS,
                        "uptime": time.time() - self.t0,
                    })
                    v = st.get("verdict")
                    if not isinstance(v, dict):
                        v = {}
                    v.update({
                        "level": "bad", "title": "设备离线",
                        "sub": "未检测到 SDR，请检查 USB 连接",
                        "sites": [], "issues": [], "active": 0,
                        "total": len(self.channels),
                    })
                    st["verdict"] = v
                    self.state = st
                time.sleep(5)

    def _tick(self, psd, masks, clip, tot, prev):
        floor = max(float(np.median(psd)), 1e-12)
        ts = time.time()
        clip_pct = 100.0 * clip / max(tot, 1)
        out = []

        for c in self.channels:
            k = c["key"]
            pch = float(np.sum(psd[masks[k]]))
            delta = 10 * math.log10(max(pch, 1e-12) / floor)
            # 长期噪声底：只用 1 小时窗口的 10 分位，每 15 秒更新一次。
            # 不用短窗滚动分位——信道繁忙时分位点会漂到活跃电平上，导致 level 假性塌陷。
            self.floor_d[k].append(delta)
            if self.floor[k] is None:
                self.floor[k] = delta
            with self.hlock:
                self.hist[k].append((ts, delta - self.floor[k]))
            level = delta - self.floor[k]

            if level > ON_DB:
                self.on_cnt[k] += 1
                self.off_cnt[k] = 0
            elif level < OFF_DB:
                self.off_cnt[k] += 1
                self.on_cnt[k] = 0

            active = prev[k]
            if not active and self.on_cnt[k] >= ON_TICKS:
                active = True
                self.event("%s 载波出现 (%.1f dB)" % (k, level), "ok", c["site"])
            elif active and self.off_cnt[k] >= OFF_TICKS:
                active = False
                self.event("%s 载波消失" % k, "idle", c["site"])
            prev[k] = active
            self.act_hist[k].append(1 if active else 0)
            if c["ctrl"] and level > ON_DB:
                self.chan_on[k].append(level)
            a2 = np.fromiter(self.act_hist[k], float, len(self.act_hist[k]))[-120 * 4:]
            out.append({**c, "delta": delta, "level": level, "active": active,
                        "duty": float(a2.mean()) if len(a2) else 0.0, "unreliable": False})

        # 邻道判别：只有"邻道不仅活跃、而且很强"时本读数才可能被压缩带偏。
        # 实测泄漏量取决于源强度而非间距：<25 dB 约 0.2 dB，>30 dB 约 1.7 dB。
        act = [x for x in out if x["active"]]
        for x in out:
            if x["active"]:
                x["unreliable"] = any(
                    y is not x and abs(y["mhz"] - x["mhz"]) <= ADJ_MHZ
                    and y["level"] >= ADJ_STRONG for y in act)

        self.tick_no += 1
        if self.tick_no % 60 == 0:
            for k, dq in self.floor_d.items():
                if len(dq) >= 120:
                    self.floor[k] = float(np.percentile(np.fromiter(dq, float, len(dq)), 10))

        # 站点级：控制信道峰值
        with self.lock:
            self.inst = {}
            for s in self.sites:
                pk = max([x["level"] for x in out if x["site"] == s and x["ctrl"]] or [0.0])
                pk = max(pk, 0.0)
                self.inst_hist[s].append(pk)
                self.inst[s] = (round(pk, 1), round(max(self.inst_hist[s]), 1))

            verdict = self._verdict(out, clip_pct, ts)
            self.state = {
                "ts": ts, "device": self.device, "clip": clip_pct, "rate": FS,
                "uptime": ts - self.t0, "channels": out, "verdict": verdict,
            }

        if self.db is not None and self.tick_no % DB_EVERY == 0:
            with self.db_lock:
                self.db.executemany("INSERT INTO samples VALUES (?,?,?,?,?)",
                                    [(ts, c["key"], c["mhz"], c["delta"], c["level"]) for c in out])
                self.db.commit()

    def build_chan_trend(self, f, t):
        """在 [f, t] 区间内按自适应桶宽重建每个信道的电平曲线（桶内取峰值）。"""
        span = max(t - f, 1.0)
        bucket = max(0.5, min(120.0, span / CH_TREND_POINTS))
        nb = max(1, int(math.ceil(span / bucket)))
        out = {}
        with self.hlock:
            snaps = {c["key"]: list(self.hist[c["key"]]) for c in self.channels}
        for c in self.channels:
            k = c["key"]
            peaks = [None] * nb
            for ts_, v in reversed(snaps[k]):
                if ts_ > t:
                    continue
                if ts_ < f:
                    break
                i = min(nb - 1, int((ts_ - f) / bucket))
                if peaks[i] is None or v > peaks[i]:
                    peaks[i] = v
            out[k] = [[round(f + i * bucket, 1), round(v, 1)]
                      for i, v in enumerate(peaks) if v is not None]
        return out

    def _load_samples(self, key, f, t):
        """读取单信道 [f, t] 的 level 样本；优先用 SQLite，失败时退回内存。"""
        if self.db is not None:
            try:
                with self.db_lock:
                    rows = self.db.execute(
                        "SELECT ts,level FROM samples WHERE key=? AND ts>=? AND ts<=? "
                        "ORDER BY ts", (key, f, t)).fetchall()
                return [(float(a), float(b)) for a, b in rows if b is not None]
            except sqlite3.Error:
                pass
        with self.lock:
            return [(ts_, v) for ts_, v in self.hist.get(key, ())
                    if f <= ts_ <= t]

    def channel_detail(self, key, f, t):
        """单信道发射强度分析：只统计有发射的样本，给出基线/近期/变化。"""
        ch = next((c for c in self.channels if c["key"] == key), None)
        if ch is None:
            return None

        span = max(t - f, 1.0)
        rows = self._load_samples(key, f, t)
        active = [(ts_, v) for ts_, v in rows if v >= DETAIL_ACTIVE_DB]

        recent_sec = min(DETAIL_RECENT_SEC, max(120.0, span * 0.2))
        recent_start = t - recent_sec
        base_vals = [v for ts_, v in active if ts_ < recent_start]
        recent_vals = [v for ts_, v in active if ts_ >= recent_start]

        baseline = recent = delta = None
        if len(base_vals) >= DETAIL_MIN_BASE:
            baseline = float(np.median(np.asarray(base_vals, dtype=float)))
        if len(recent_vals) >= DETAIL_MIN_RECENT:
            recent = float(np.median(np.asarray(recent_vals, dtype=float)))
        if baseline is not None and recent is not None:
            delta = recent - baseline

        level = "insufficient"
        if baseline is None:
            text = "较早时段有效发射样本不足，继续积累后再判断"
        elif recent is None:
            level = "idle"
            text = ("最近 %s无有效发射，无法判断（可能只是没人使用）"
                    % _fmt_span(recent_sec))
        elif delta <= -BAD_DB:
            level = "bad"
            text = ("最近 %s比早先下降 %.1f dB，重点检查该频点功率"
                    % (_fmt_span(recent_sec), abs(delta)))
        elif delta <= -WARN_DB:
            level = "warn"
            text = ("最近 %s比早先下降 %.1f dB，建议确认该频点功率"
                    % (_fmt_span(recent_sec), abs(delta)))
        else:
            level = "ok"
            text = ("最近 %s与早先基本持平（变化 %+.1f dB）"
                    % (_fmt_span(recent_sec), delta or 0.0))

        bucket = max(1.0, min(120.0, span / DETAIL_POINTS))
        nb = max(1, int(math.ceil(span / bucket)))
        peaks = [None] * nb
        counts = [0] * nb
        for ts_, v in active:
            i = min(nb - 1, int((ts_ - f) / bucket))
            if peaks[i] is None or v > peaks[i]:
                peaks[i] = v
            counts[i] += 1
        points = [[round(f + i * bucket, 1), round(v, 1), counts[i]]
                  for i, v in enumerate(peaks) if v is not None]
        last = {"t": round(active[-1][0], 1), "v": round(active[-1][1], 1)} if active else None

        return {
            "ts": time.time(), "from": f, "to": t, "key": key,
            "site": ch["site"], "rep": ch["rep"], "mhz": ch["mhz"], "ctrl": ch["ctrl"],
            "points": points, "level": level, "text": text,
            "baseline": None if baseline is None else round(baseline, 1),
            "baseline_n": len(base_vals),
            "recent": None if recent is None else round(recent, 1),
            "recent_n": len(recent_vals),
            "recent_peak": round(max(recent_vals), 1) if recent_vals else None,
            "recent_sec": round(recent_sec),
            "delta": None if delta is None else round(delta, 1),
            "duty": round(100.0 * len(active) / len(rows), 1) if rows else 0.0,
            "samples": len(rows), "last": last,
        }

    def _verdict(self, out, clip_pct, ts):
        sites = []
        rank = 0
        for s in self.sites:
            chans = [x for x in out if x["site"] == s]
            ctrls = [x for x in chans if x["ctrl"]]
            active = sum(1 for x in chans if x["active"])
            clean = sum(1 for x in chans if x["active"] and not x["unreliable"])
            peak = max([x["level"] for x in chans if x["ctrl"]] or [0.0])

            # 逐信道：基线取"活跃样本的滚动中位数"（对多设备轮流发射稳健），
            # 当前值取最近 60 秒峰值；站点读数跟随"当前占主导的那台设备"。
            cands = []
            warmed = 0
            for c in ctrls:
                k = c["key"]
                arr = self.chan_on[k]
                if len(arr) >= CHAN_MIN:
                    warmed += 1
                    base = float(np.median(np.fromiter(arr, float, len(arr))))
                    with self.hlock:
                        snap = list(self.hist[k])[-600:]
                    recent = [lv for t, lv in snap if t >= ts - 60]
                    if recent and max(recent) > ON_DB:
                        cands.append((max(recent), max(recent) - base, k, base, c["mhz"]))

            dom = None
            if not ctrls or warmed == 0:
                state, text, dev, base = "init", "基线建立中", None, None
            elif not cands:
                state, text, dev, base = "idle", "无活动", None, None
            else:
                dom = max(cands)                      # 取最近峰值最高的信道
                dev = round(dom[1], 1)
                base = round(dom[3], 1)
                target = 2 if dev <= -BAD_DB else (1 if dev <= -WARN_DB else 0)
                cur = self.dev_state[s]
                if target > cur:
                    self.dev_up[s] += 1
                    self.dev_dn[s] = 0
                    if self.dev_up[s] >= 4 * 15:      # 连续 15 秒
                        cur = target
                        self.dev_up[s] = 0
                        self.event("%s 信号下降 %.1f dB (%s)" % (s, dev, dom[2]),
                                   "err" if target == 2 else "warn", s)
                elif target < cur:
                    self.dev_dn[s] += 1
                    self.dev_up[s] = 0
                    if self.dev_dn[s] >= 4 * 30:      # 连续 30 秒
                        cur = target
                        self.dev_dn[s] = 0
                        self.event("%s 信号恢复" % s, "ok", s)
                else:
                    self.dev_up[s] = 0
                    self.dev_dn[s] = 0
                self.dev_state[s] = cur
                state, text = {0: ("ok", "正常"),
                               1: ("warn", "信号偏低"),
                               2: ("bad", "信号下降")}[cur]

            rank = max(rank, 2 if state == "bad" else (1 if state == "warn" else 0))
            inst, inst1 = self.inst.get(s, (0.0, 0.0))
            sites.append({
                "site": s, "state": state, "text": text,
                "inst": inst, "inst1": inst1,
                "active": active, "clean": clean, "total": len(chans),
                "peak": round(peak, 1), "base": base, "dev": dev,
                "ctrlKey": dom[2] if dom else "",
                "ctrlMhz": dom[4] if dom else 0,
            })

        if not self.device:
            level, title = "bad", "设备离线"
            rank = 2
        elif clip_pct > 2:
            level, title = "warn", "数据质量下降"
            rank = max(rank, 1)
        elif rank >= 2:
            level, title = "bad", "发现异常"
        elif rank >= 1:
            level, title = "warn", "需要关注"
        elif any(x["state"] == "init" for x in sites):
            level, title = "init", "基线建立中"
        elif all(x["state"] == "idle" for x in sites):
            level, title = "idle", "全部站点无活动"
        else:
            level, title = "ok", "运行正常"

        issues = []
        for e in self.events:
            if e["kind"] in ("err", "warn") and ts - e["t"] < 1800:
                issues.append({"t": e["t"], "site": e["site"], "kind": e["kind"],
                               "text": e["msg"]})
            if len(issues) >= 8:
                break
        if clip_pct > 2:
            issues.insert(0, {"t": ts, "site": "", "kind": "warn",
                              "text": "采样削顶 %.1f%%，当前数据不可信" % clip_pct})

        issue_sites = {i["site"] for i in issues if i.get("site")}
        for x in sites:
            if x["site"] in issue_sites and x["state"] == "ok":
                x["state"], x["text"] = "warn", "需确认"
        if issues and level in ("ok", "idle"):
            level, title = "warn", "需要关注"

        active_n = sum(1 for x in out if x["active"])
        n_init = sum(1 for x in sites if x["state"] == "init")
        n_bad = sum(1 for x in sites if x["state"] == "bad")
        n_warn = sum(1 for x in sites if x["state"] == "warn")
        if not self.device:
            sub = "采集设备已断开，正在尝试重连"
        elif clip_pct > 2:
            sub = "采样削顶 %.1f%%，当前数据不可信，请降低增益或加衰减" % clip_pct
        elif n_bad:
            sub = "%d 个站点信号明显下降，基线取自该信道自身 30 分钟中值" % n_bad
        elif n_warn:
            sub = "%d 个站点信号偏低，基线取自该信道自身 30 分钟中值" % n_warn
        elif issues:
            sub = "最近 30 分钟有 %d 条需要确认的记录" % len(issues)
        elif n_init:
            sub = "%d 个站点基线建立中，需累计该信道约 15 秒发射样本" % n_init
        elif active_n == 0:
            sub = "全部站点无活动，可能只是没人使用，不代表故障"
        else:
            sub = "最近 30 分钟未发现异常 · %d 个站点 · %d 个信道 · 活跃 %d" % (
                len(sites), len(out), active_n)
        return {"level": level, "title": title, "sub": sub, "sites": sites,
                "issues": issues, "active": active_n, "total": len(out)}


class Handler(BaseHTTPRequestHandler):
    engine = None

    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            with self.engine.lock:
                st = self.engine.state
            return self._send(json.dumps(st, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if path == "/api/chan-trend":
            q = parse_qs(urlparse(self.path).query)
            now = time.time()
            try:
                to = float(q.get("to", [now])[0])
            except ValueError:
                to = now
            if "from" in q:
                try:
                    f = float(q["from"][0])
                except ValueError:
                    f = to - 1800.0
            else:
                try:
                    f = to - float(q.get("range", [300])[0])
                except ValueError:
                    f = to - 300.0
            if to > now:
                to = now
            with self.engine.lock:
                oldest = now
                for dq in self.engine.hist.values():
                    if dq:
                        oldest = min(oldest, dq[0][0])
            f = max(f, oldest)
            if to - f < 5:
                f = max(oldest, to - 5)
            body = {"ts": now, "from": f, "to": to,
                    "chan_trend": self.engine.build_chan_trend(f, to)}
            return self._send(json.dumps(body, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if path == "/api/chan-detail":
            q = parse_qs(urlparse(self.path).query)
            key = (q.get("key", [""])[0] or "").strip()
            now = time.time()
            try:
                to = float(q.get("to", [now])[0])
            except ValueError:
                to = now
            if "from" in q:
                try:
                    f = float(q["from"][0])
                except ValueError:
                    f = to - 3600.0
            else:
                try:
                    f = to - float(q.get("range", [3600])[0])
                except ValueError:
                    f = to - 3600.0
            if to > now:
                to = now
            f = max(f, now - 6 * 3600, to - 6 * 3600)
            if to - f < 30:
                f = max(now - 6 * 3600, to - 30)
            body = self.engine.channel_detail(key, f, to)
            if body is None:
                self.send_error(404)
                return
            return self._send(json.dumps(body, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if path == "/":
            path = "/index.html"
        fp = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not fp.startswith(WEB) or not os.path.isfile(fp):
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8"}.get(
            os.path.splitext(fp)[1], "application/octet-stream")
        with open(fp, "rb") as fh:
            self._send(fh.read(), ctype)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址；默认仅本机。远程访问请用 SSH 隧道/反向代理，本服务无鉴权")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--db", default=os.path.join(HERE, "rssi.db"))
    ap.add_argument("--channels", default=os.path.join(HERE, "channels.csv"))
    args = ap.parse_args()
    build_tool()
    chans = load_channels(args.channels)
    eng = Engine(chans, args.db)
    Handler.engine = eng
    eng.start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print("仪表台: http://%s:%d  (信道 %d, 中心 %.3f MHz)"
          % (shown, args.port, len(chans), FC / 1e6), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
