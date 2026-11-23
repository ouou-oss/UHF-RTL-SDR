#!/usr/bin/env python3
"""
多信道 RSSI 监控 —— 一次采样覆盖全部 18 个信道
================================================

设计要点（三条都是被实测逼出来的）：
  1. 不用 rtl_power：它不暴露 RTL AGC 开关，实测会导致本底跳变 8 dB、
     各信道 delta 乱跳 10~25 dB。
  2. 必须开 RTL2832U 内部 AGC：这颗 FC0012 的调谐器增益档实际无效
     （-9.9 与 19.2 dB 差不到 1 dB），AGC 是唯一有效的电平控制。
     开 AGC 削顶 0.36%，关 AGC 削顶 30%+。
  3. 只信"差分"：每个信道功率减去同帧全带中位噪声底。AGC 和温漂同时
     作用于两者，相减抵消。实测 10 秒内 sigma 0.07~0.11 dB。

用法:
    python3 rtl_multi_monitor.py --every 5
    python3 rtl_multi_monitor.py --db rssi.db --every 60
"""

import argparse
import csv
import math
import os
import sqlite3
import subprocess
import sys
import time

import numpy as np

N = 8192                      # FFT 点数
FS = 2_048_000.0
FC = 421_990_000.0            # 覆盖 421.1125~422.8625 全部 18 信道
CHAN_HALF_HZ = 6_250.0        # 12.5 kHz 信道半宽
HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, ".bin", "iqcap")


def build_capture_tool() -> None:
    src = os.path.join(HERE, "iqcap.c")
    if os.path.exists(EXE) and os.path.getmtime(EXE) > os.path.getmtime(src):
        return
    os.makedirs(os.path.dirname(EXE), exist_ok=True)
    cc = os.environ.get("CC", "clang")
    brew = os.environ.get("BREW_PREFIX", "/opt/homebrew")
    subprocess.run(
        [cc, "-O2", "-o", EXE, src,
         "-I%s/include" % brew, "-L%s/lib" % brew, "-lrtlsdr"],
        check=True,
    )


def load_channels(path: str):
    out = []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            f = float(r["downlink_mhz"])
            if f > (FC + FS / 2) / 1e6 or f < (FC - FS / 2) / 1e6:
                continue
            out.append({
                "site": r["site"], "rep": r["repeater_id"],
                "ctrl": r["is_control"] == "1", "mhz": f,
            })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", default=os.path.join(HERE, "channels.csv"))
    ap.add_argument("--gain", type=int, default=192, help="调谐器增益 (0.1 dB)，实测无效，保留")
    ap.add_argument("--agc", type=int, default=1, help="RTL AGC，必须为 1")
    ap.add_argument("--every", type=float, default=5.0, help="每隔多少秒打印一次表")
    ap.add_argument("--db", default="", help="SQLite 输出路径")
    args = ap.parse_args()

    build_capture_tool()
    chans = load_channels(args.channels)
    win = np.hanning(N).astype(np.float32)
    freqs = FC + np.fft.fftshift(np.fft.fftfreq(N, 1.0 / FS))
    masks = {c["mhz"]: np.abs(freqs - c["mhz"] * 1e6) < CHAN_HALF_HZ for c in chans}

    db = None
    if args.db:
        db = sqlite3.connect(args.db)
        db.execute("CREATE TABLE IF NOT EXISTS samples("
                   "ts REAL, site TEXT, rep TEXT, mhz REAL, delta REAL)")

    proc = subprocess.Popen([EXE, str(args.gain), str(args.agc), "0"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames_per_sec = int(FS) // N
    print("中心 %.3f MHz | 采样 %.3f MS/s | 信道 %d 个 | AGC %d"
          % (FC / 1e6, FS / 1e6, len(chans), args.agc), flush=True)
    print("每 %.0f 秒输出一次；delta 单位为 dB（相对全带中位噪声底）\n" % args.every, flush=True)

    buf = bytearray()
    acc = None
    nacc = 0
    clip = 0
    tot = 0
    last_print = time.time()
    try:
        while True:
            chunk = os.read(proc.stdout.fileno(), 1 << 18)
            if not chunk:
                print("设备输出中断", flush=True)
                break
            buf += chunk
            nframe = len(buf) // (2 * N)
            if nframe == 0:
                continue
            use = nframe * 2 * N
            a = np.frombuffer(bytes(buf[:use]), dtype=np.uint8).astype(np.float32) - 127.5
            del buf[:use]
            z = a.reshape(nframe, N, 2)
            iq = z[:, :, 0] + 1j * z[:, :, 1]
            clip += int(np.sum((np.abs(iq.real) > 126) | (np.abs(iq.imag) > 126)))
            tot += iq.size
            spec = np.fft.fftshift(np.fft.fft(iq * win, axis=1), axes=1)
            p = (spec.real ** 2 + spec.imag ** 2)
            acc = p.sum(axis=0) if acc is None else acc + p.sum(axis=0)
            nacc += nframe

            if time.time() - last_print < args.every:
                continue
            last_print = time.time()

            floor = float(np.median(acc / nacc))
            rows = []
            for c in chans:
                pch = float(np.sum(acc[masks[c["mhz"]]] / nacc))
                delta = 10 * math.log10(pch / max(floor, 1e-12))
                rows.append((c, delta))
            rows.sort(key=lambda r: -r[1])
            print("[%s] 削顶 %.2f%%  %d 信道" %
                  (time.strftime("%H:%M:%S"), 100.0 * clip / max(tot, 1), len(rows)), flush=True)
            for c, d in rows:
                tag = "控制" if c["ctrl"] else "    "
                bar = "█" * max(0, int((d - 12) / 2))
                print("   %s-%d %9.4f MHz %s %6.1f dB %s"
                      % (c["site"], int(c["rep"]), c["mhz"], tag, d, bar), flush=True)
            print(flush=True)
            if db is not None:
                db.executemany("INSERT INTO samples VALUES (?,?,?,?,?)",
                               [(time.time(), c["site"], c["rep"], c["mhz"], d)
                                for c, d in rows])
                db.commit()
            acc, nacc, clip, tot = None, 0, 0, 0
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    finally:
        proc.terminate()
        if db is not None:
            db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
