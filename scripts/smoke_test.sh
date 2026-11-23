#!/usr/bin/env bash
# 端到端冒烟检查：后端语法 + 静态页 + 三个 JSON 接口。
# 用法:
#   bash scripts/smoke_test.sh
#   BASE_URL=http://127.0.0.1:8787 bash scripts/smoke_test.sh
# 退出码 0 表示全部通过。

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${BASE_URL:-http://127.0.0.1:8787}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
section() { printf '\n%s\n' "$1"; }

cd "$ROOT" || exit 1

section "1. Python 语法"
if python3 -m py_compile dashboard.py 2>"$TMP/py.err"; then
  ok "dashboard.py 可编译"
else
  bad "dashboard.py 语法错误：$(head -3 "$TMP/py.err")"
fi

section "2. 前端 JS 语法（可选）"
if command -v node >/dev/null 2>&1; then
  if node --check web/app.js 2>"$TMP/js.err"; then
    ok "web/app.js 语法通过"
  else
    bad "web/app.js 语法错误：$(head -3 "$TMP/js.err")"
  fi
else
  printf '  SKIP 未安装 Node，跳过 node --check\n'
fi

section "3. HTTP 静态页 ($BASE_URL/)"
if curl -fsS -m 5 -o "$TMP/index.html" "$BASE_URL/"; then
  if grep -q 'id="viewChannel"' "$TMP/index.html"; then
    ok "首页可访问，包含信道分析视图"
  else
    bad "首页内容不完整（找不到 viewChannel）"
  fi
else
  bad "无法访问 $BASE_URL/，服务是否已启动？"
  printf '\n结果: %d 通过, %d 失败\n' "$PASS" "$FAIL"
  exit 1
fi

section "4. /api/state"
STATE_OK=0
if curl -fsS -m 5 -o "$TMP/state.json" "$BASE_URL/api/state"; then
  if python3 - "$TMP/state.json" >"$TMP/state.txt" 2>"$TMP/state.err" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert isinstance(d.get("ts"), (int, float)), "缺少 ts"
assert isinstance(d.get("device"), bool), "缺少 device"
ch = d.get("channels")
assert isinstance(ch, list) and ch, "channels 为空"
assert all(k in ch[0] for k in ("key", "site", "mhz", "level", "active")), "channels 字段缺失"
v = d.get("verdict") or {}
assert "level" in v and "sites" in v, "verdict 字段缺失"
print("device=%s channels=%d verdict=%s" % (d["device"], len(ch), v.get("level")))
print(ch[0]["key"])
PY
  then
    DEVINFO="$(sed -n 1p "$TMP/state.txt")"
    KEY="$(sed -n 2p "$TMP/state.txt")"
    ok "state 正常：$DEVINFO"
    STATE_OK=1
  else
    bad "state JSON 校验失败：$(head -3 "$TMP/state.err")"
  fi
else
  bad "GET /api/state 失败"
fi

section "5. /api/chan-trend?range=300"
if curl -fsS -m 5 -o "$TMP/trend.json" "$BASE_URL/api/chan-trend?range=300"; then
  if python3 - "$TMP/trend.json" >"$TMP/trend.txt" 2>"$TMP/trend.err" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert "from" in d and "to" in d, "缺少 from/to"
t = d.get("chan_trend")
assert isinstance(t, dict) and t, "chan_trend 为空"
k = next(iter(t))
assert isinstance(t[k], list), "曲线不是数组"
print("channels=%d points=%d" % (len(t), len(t[k])))
PY
  then
    ok "chan-trend 正常：$(cat "$TMP/trend.txt")"
  else
    bad "chan-trend JSON 校验失败：$(head -3 "$TMP/trend.err")"
  fi
else
  bad "GET /api/chan-trend 失败"
fi

section "6. /api/chan-detail"
if [ "$STATE_OK" = "1" ]; then
  if curl -fsS -m 10 -o "$TMP/detail.json" \
      "$BASE_URL/api/chan-detail?key=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' "$KEY")&range=900"; then
    if python3 - "$TMP/detail.json" >"$TMP/detail.txt" 2>"$TMP/detail.err" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d.get("key"), "缺少 key"
assert "points" in d and isinstance(d["points"], list), "points 不是数组"
assert "level" in d and "text" in d, "缺少判定字段"
print("%s level=%s points=%d baseline=%s recent=%s delta=%s samples=%d" % (
    d["key"], d["level"], len(d["points"]), d.get("baseline"),
    d.get("recent"), d.get("delta"), d.get("samples", 0)))
PY
    then
      ok "chan-detail 正常：$(cat "$TMP/detail.txt")"
    else
      bad "chan-detail JSON 校验失败：$(head -3 "$TMP/detail.err")"
    fi
  else
    bad "GET /api/chan-detail 失败"
  fi
else
  bad "state 不可用，跳过 chan-detail"
fi

printf '\n结果: %d 通过, %d 失败\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
