/* iqcap.c —— 最小 IQ 采集器，用于绕过 rtl_power / rtl_sdr 的增益处理
 * 编译: clang -O2 -o iqcap iqcap.c -I/opt/homebrew/include -L/opt/homebrew/lib -lrtlsdr
 * 用法: ./iqcap <增益(0.1dB)> <AGC(0|1)> <采样数，0=无限> [中心Hz] [采样率] > out.bin
 *
 * 为什么需要它:
 *   rtl_sdr/rtl_power 不暴露 rtlsdr_set_agc_mode()。而在 FC0012 电视棒上
 *   调谐器增益档位实际无效，RTL2832U 的内部数字 AGC 是唯一的电平控制手段。
 *   AGC 打开时削顶 ~0.3%，关闭时 >30%。所以必须能控制这个开关。
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <rtl-sdr.h>

int main(int argc, char **argv) {
    if (argc < 4) { fprintf(stderr, "usage: %s gain agc samples [center_hz] [rate]\n", argv[0]); return 2; }
    int gain = atoi(argv[1]), agc = atoi(argv[2]);
    long long n = atoll(argv[3]);
    unsigned int center = (argc > 4) ? (unsigned int)atol(argv[4]) : 421990000u;
    unsigned int rate = (argc > 5) ? (unsigned int)atol(argv[5]) : 2048000u;

    rtlsdr_dev_t *d = NULL;
    if (rtlsdr_open(&d, 0) != 0) { fprintf(stderr, "open fail (device busy?)\n"); return 1; }

    rtlsdr_set_center_freq(d, center);
    rtlsdr_set_sample_rate(d, rate);
    rtlsdr_set_tuner_gain_mode(d, 1);
    rtlsdr_set_agc_mode(d, agc);
    rtlsdr_set_tuner_gain(d, gain);
    rtlsdr_reset_buffer(d);

    uint8_t *buf = malloc(262144);
    if (!buf) return 1;
    while (1) {
        int r = 0, want = (n > 0 && n < 262144) ? (int)n : 262144;
        if (rtlsdr_read_sync(d, buf, want, &r) != 0 || r <= 0) break;
        if (fwrite(buf, 1, r, stdout) != (size_t)r) break;
        if (n > 0) { n -= r; if (n <= 0) break; }
        fflush(stdout);
    }
    free(buf);
    rtlsdr_close(d);
    return 0;
}
