#!/usr/bin/env bash
# 通用入口：等价于 b60-detect.sh
exec bash "$(cd "$(dirname "$0")" && pwd)/b60-detect.sh" "$@"
