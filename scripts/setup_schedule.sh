#!/usr/bin/env bash
# 一次性运行此脚本完成所有定时设置
# 用法：bash scripts/setup_schedule.sh

echo "=== AI Daily Pipeline 定时任务配置 ==="
echo ""

# 1. pmset：每天 05:50 唤醒 Mac（launchd 任务 06:00 才开始，留 10 分钟余量）
echo "1. 配置每天 05:50 自动唤醒..."
sudo pmset repeat wakeorpoweron MTWRFSU 05:50:00 && \
    echo "   ✓ 唤醒计划已设置：每天 05:50" || \
    echo "   ✗ 设置失败（权限不足？）"

# 2. 验证 launchd 任务
echo ""
echo "2. 检查 launchd 定时任务..."
if launchctl list | grep -q "com.cuttingedgeai.daily"; then
    echo "   ✓ launchd 任务已加载（每天 06:00 执行）"
else
    echo "   ✗ launchd 任务未加载，正在加载..."
    launchctl load ~/Library/LaunchAgents/com.cuttingedgeai.daily.plist && \
        echo "   ✓ 已加载" || echo "   ✗ 加载失败"
fi

# 3. 验证 daily_run.sh 可执行
echo ""
echo "3. 检查脚本权限..."
SCRIPT="$(dirname "$0")/daily_run.sh"
chmod +x "$SCRIPT"
echo "   ✓ $SCRIPT"

# 4. 当前 pmset 唤醒计划
echo ""
echo "4. 当前唤醒计划："
pmset -g sched | grep -v "^Scheduled" | head -5 | sed 's/^/   /'

echo ""
echo "=== 配置完成 ==="
echo ""
echo "工作方式："
echo "  • 每天 05:50：Mac 自动从睡眠唤醒"
echo "  • 每天 06:00：开始采集分析，约 08:00 生成日报"
echo "  • 运行期间：caffeinate 阻止息眠"
echo "  • 运行完成：Mac 自动回到息眠（由系统息眠设置控制）"
echo ""
echo "注意事项："
echo "  • Mac 需处于睡眠状态（合盖），不能完全关机"
echo "  • 建议充电过夜，低电量可能导致唤醒失败"
echo "  • 关机状态下无法自动唤醒（需手动运行）"
