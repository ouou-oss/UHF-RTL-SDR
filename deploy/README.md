# 服务部署模板

这两个文件是**模板**，不能直接使用，先替换占位符/路径。

## macOS（launchd）

```bash
cp com.radio.dashboard.plist.example ~/Library/LaunchAgents/com.radio.dashboard.plist
# 编辑 ~/Library/LaunchAgents/com.radio.dashboard.plist：
#   __PROJECT_DIR__ -> /Users/you/Radio
#   __PYTHON__      -> /Users/you/Radio/.venv/bin/python
#   BREW_PREFIX     -> Intel Mac 改为 /usr/local
plutil -lint ~/Library/LaunchAgents/com.radio.dashboard.plist
launchctl load ~/Library/LaunchAgents/com.radio.dashboard.plist
launchctl list | grep radio
```

- 日志：`__PROJECT_DIR__/dashboard.log`、`dashboard.err.log`。
- 停止：`launchctl unload ~/Library/LaunchAgents/com.radio.dashboard.plist`。
- 修改 plist 后需要先 unload 再 load。

## Linux（systemd）

```bash
sudo cp radio-dashboard.service /etc/systemd/system/
# 编辑：User/Group/WorkingDirectory/ExecStart/BREW_PREFIX
sudo systemctl daemon-reload
sudo systemctl enable --now radio-dashboard
systemctl status radio-dashboard
journalctl -u radio-dashboard -f
```

- 运行用户需要 RTL-SDR 的 USB 访问权限。Debian/Ubuntu 通常加入 `plugdev` 组，
  并安装 rtl-sdr 的 udev 规则（`apt install rtl-sdr`）。
- 如果系统没有 `plugdev` 组，按发行版的实际组名修改 `Group=`。
- `--host 127.0.0.1` 是刻意设置：服务无鉴权，远程访问请用 SSH 隧道或带认证的反向代理。

## 部署后验证

```bash
curl -fsS http://127.0.0.1:8787/api/state \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["device"], len(d["channels"]))'
bash scripts/smoke_test.sh
```

详细说明和排障见 [README](../README.md) 与 [docs/AI_DEPLOY.md](../docs/AI_DEPLOY.md)。
