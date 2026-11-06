#!/bin/bash
# 每 15 分钟记一行内存（11/06，部署 agent；起因见 docs《问题排查记录-服务器卡死-1106》）
# 由 admin 的 crontab 调用：*/15 * * * * bash /home/admin/QQBot/bot/tools/memlog.sh
# 记到 ~/QQBot/bot/data/logs/memlog.log，拉取服务器日志.bat 会一起下载。
# 每行：时间｜系统还能用多少｜机器人（qqbot）用了多少内存 + swap｜QQ（napcat）用了多少内存 + swap
CG=${MEMLOG_CGROUP:-/sys/fs/cgroup/system.slice}
LOG=${MEMLOG_FILE:-$HOME/QQBot/bot/data/logs/memlog.log}
MEMINFO=${MEMLOG_MEMINFO:-/proc/meminfo}

mb() { local v; v=$(cat "$1" 2>/dev/null) || { echo "?"; return; }; [ "$v" = "max" ] && { echo "不限"; return; }; echo "$((v / 1048576))M"; }
svc() { echo "$1 $(mb "$CG/$1.service/memory.current")+swap $(mb "$CG/$1.service/memory.swap.current")"; }

avail=$(awk '/^MemAvailable:/{print int($2/1024)}' "$MEMINFO")
swapused=$(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{print int((t-f)/1024)}' "$MEMINFO")
mkdir -p "$(dirname "$LOG")"
echo "$(date '+%F %T')｜可用 ${avail}M，swap 已用 ${swapused}M｜$(svc qqbot)｜$(svc napcat)" >> "$LOG"

# 只留最近约 40 天（15 分钟一行，一天 96 行）
if [ "$(wc -l < "$LOG")" -gt 4000 ]; then
  tail -n 3000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
