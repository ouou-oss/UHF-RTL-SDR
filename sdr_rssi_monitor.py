#!/usr/bin/env python3
"""
UHF 集群基站控制信道 —— 差分 RSSI 监控原型
================================================

为什么用"差分"：
    这根 FC0012 电视棒没有 TCXO，增益档也基本失效，绝对电平会随温度/时间漂移。
    但把"信号信道"减去"邻近空闲信道的噪声底"，两者共模漂移会被抵消，
    剩下的差值在 2~3 dB 内是可重复的，足够判断天馈是否劣化（劣化通常表现为 3 dB 以上）。

为什么用 rtl_power 而不是 pyrtlsdr：
    本机没有 numpy/pyrtlsdr，rtl_power 自带 C 版 FFT，直接出已校准的 dB 谱，零依赖。

用法：
    # 冒烟测试（FM 广播，连续载波，验证链路）
    python3 sdr_rssi_monitor.py --sig 100.0M --ref 99.9M --seconds 8

    # 实际监控（示例：452.500 为控制信道，452.400 为参考空闲信道）
    python3 sdr_rssi_monitor.py --sig 452.500M --ref 452.400M --loop --db rssi.db
"""

import argparse
import os
import re
import sqlite3
import statistics as st
import subprocess
import sys
import tempfile
import time

BIN_HZ = 12_500.0  # DMR 一个信道宽度


def parse_freq(text: str) -> float:
    """'452.5M' / '452500k' / '452500000' -> Hz"""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([kKmMgG]?)\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"无法解析频率: {text}")
    mult = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]
    return float(m.group(1)) * mult


def run_sweep(lo_hz: float, hi_hz: float, seconds: int, gain: float, out_path: str) -> None:
    """调用 rtl_power 采集一段时间的谱，写入 out_path（CSV 累积）。"""
    cmd = [
        "rtl_power",
        "-f", f"{lo_hz:.0f}:{hi_hz:.0f}:{BIN_HZ:.0f}",
        "-g", str(gain),
        "-i", "1",
        "-e", str(seconds),
        out_path,
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def read_rows(path: str, lo_hz: float, step_hz: float, sig_hz: float, ref_hz: float):
    """解析 rtl_power CSV，返回 [(ts, sig_db, ref_db)]。"""
    out = []
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                continue
            try:
                lo = float(parts[2])
                step = float(parts[4])
                vals = [float(x) for x in parts[6:] if x not in ("", "nan")]
            except ValueError:
                continue
            if not vals:
                continue
            ts = time.time()
            si = int((sig_hz - lo) / step)
            ri = int((ref_hz - lo) / step)
            if 0 <= si < len(vals) and 0 <= ri < len(vals):
                out.append((ts, vals[si], vals[ri]))
    return out


def stats_line(tag: str, xs) -> str:
    if not xs:
        return f"{tag}: 无数据"
    return (f"{tag}: n={len(xs):4d}  mean={st.mean(xs):7.2f} dB  "
            f"sd={st.pstdev(xs):5.2f}  min={min(xs):7.2f}  max={max(xs):7.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="UHF 控制信道差分 RSSI 监控")
    ap.add_argument("--sig", type=parse_freq, required=True, help="控制信道频率")
    ap.add_argument("--ref", type=parse_freq, required=True, help="邻近空闲参考信道频率")
    ap.add_argument("--seconds", type=int, default=10, help="每批采集时长")
    ap.add_argument("--gain", type=float, default=19.2, help="rtl_power 增益档（FC0012 仅 5 档）")
    ap.add_argument("--loop", action="store_true", help="持续监控")
    ap.add_argument("--db", default="", help="SQLite 输出路径（持续监控用）")
    ap.add_argument("--alert-drop", type=float, default=3.0, help="差值跌破多少 dB 告警")
    args = ap.parse_args()

    lo = min(args.sig, args.ref) - BIN_HZ
    hi = max(args.sig, args.ref) + BIN_HZ
    print(f"信号 {args.sig/1e6:.4f} MHz | 参考 {args.ref/1e6:.4f} MHz | "
          f"扫描 {lo/1e6:.4f}~{hi/1e6:.4f} MHz | 增益 {args.gain} dB", flush=True)

    db = None
    if args.db:
        db = sqlite3.connect(args.db)
        db.execute("CREATE TABLE IF NOT EXISTS samples("
                   "ts REAL, sig_db REAL, ref_db REAL, delta REAL)")
        db.commit()

    baseline = None
    try:
        round_no = 0
        while True:
            round_no += 1
            fd, path = tempfile.mkstemp(suffix=".csv", prefix="rtlpow_")
            os.close(fd)
            try:
                run_sweep(lo, hi, args.seconds, args.gain, path)
                rows = read_rows(path, lo, BIN_HZ, args.sig, args.ref)
            finally:
                os.unlink(path)

            if not rows:
                print("!! 本批无有效采样（设备被占用？）", flush=True)
                if not args.loop:
                    return 1
                time.sleep(2)
                continue

            sig = [r[1] for r in rows]
            ref = [r[2] for r in rows]
            delta = [s - r for s, r in zip(sig, ref)]
            # 只统计"有突发"的样本：信号比噪声底高 6 dB 以上
            active = [d for d in delta if d > 6.0]

            print(f"\n[{time.strftime('%H:%M:%S')}] 批次 {round_no}", flush=True)
            print("  " + stats_line("信号", sig), flush=True)
            print("  " + stats_line("参考", ref), flush=True)
            print("  " + stats_line("差值", delta), flush=True)
            print(f"  突发样本(>6dB): {len(active)}/{len(delta)}"
                  + (f"  突发内差值中值={st.median(active):.2f} dB" if active else ""), flush=True)

            if baseline is None and len(active) >= 3:
                baseline = st.median(active)
                print(f"  基线建立: {baseline:.2f} dB", flush=True)
            elif baseline is not None and active:
                cur = st.median(active)
                d = cur - baseline
                flag = "  <-- 告警: 疑似天馈劣化/覆盖下降" if d < -args.alert_drop else ""
                print(f"  相对基线 {d:+.2f} dB{flag}", flush=True)

            if db is not None:
                db.executemany(
                    "INSERT INTO samples VALUES (?,?,?,?)",
                    [(r[0], r[1], r[2], r[1] - r[2]) for r in rows],
                )
                db.commit()

            if not args.loop:
                break
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        if db is not None:
            db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
