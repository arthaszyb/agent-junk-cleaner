#!/bin/sh
cd "$(dirname "$0")" || exit 1
echo "Keep this window open while you use Disk Steward; close it to stop. | 使用 Disk Steward 期间请保持此窗口打开，关闭窗口即停止。"
if [ -d src/disk_steward ]; then
  PYTHONPATH=src exec /usr/bin/env python3 -m disk_steward portal --open
else
  exec disk-steward portal --open
fi
