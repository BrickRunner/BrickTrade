#!/bin/bash
# BrickTrade Safe Cleanup Script
# Создает backup перед удалением лишних файлов

set -e  # Exit on error

BACKUP_DIR="backup_$(date +%Y-%m-%d_%H%M%S)"
ARCHIVE_NAME="${BACKUP_DIR}.tar.gz"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║         BrickTrade Safe Cleanup Script                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Check if we're in the right directory
if [ ! -f "main.py" ]; then
    echo "❌ Error: Not in BrickTrade directory!"
    echo "   Please run from: /Users/macbookairdmitri/BrickTrade"
    exit 1
fi

echo "📁 Creating backup directory: ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"

echo ""
echo "📦 Moving files to backup..."

# Category 1: Test scripts
echo "  🔴 Test scripts..."
for file in test_audit_fixes.py test_improvements.py test_enhancements.py \
            test_htx_api.py test_htx_balance.py test_short_symbols.py; do
    if [ -f "$file" ]; then
        mv "$file" "${BACKUP_DIR}/" && echo "     ✓ $file"
    fi
done

# Category 2: Check/close utilities
echo "  🔴 Check/close utilities..."
for file in check_htx_positions.py check_live.py check_short_config.py \
            close_htx_positions.py; do
    if [ -f "$file" ]; then
        mv "$file" "${BACKUP_DIR}/" && echo "     ✓ $file"
    fi
done

# Category 3: Shell scripts
echo "  🔴 Shell scripts..."
for file in launch_bybit.sh; do
    if [ -f "$file" ]; then
        mv "$file" "${BACKUP_DIR}/" && echo "     ✓ $file"
    fi
done

# Category 4: Auxiliary files
echo "  🟢 Auxiliary files..."
for file in claude_terminal.py SUMMARY_SHORT_EXPANSION.txt; do
    if [ -f "$file" ]; then
        mv "$file" "${BACKUP_DIR}/" && echo "     ✓ $file"
    fi
done

# Category 5: Outdated documentation (optional)
echo ""
read -p "🟡 Archive outdated documentation? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "  🟡 Outdated docs..."
    for file in QUICK_START_ARBITRAGE.md CORRECTNESS_REPORT.md \
                OPTIMIZATION_RESULTS.md REPORT.md PLAN.md TODO.md \
                ENHANCEMENTS_SUMMARY.txt READY_FOR_TEST_LAUNCH_ANALYSIS.md \
                SWITCH_TO_TRADING_MODE.md trading_system_spec.md; do
        if [ -f "$file" ]; then
            mv "$file" "${BACKUP_DIR}/" && echo "     ✓ $file"
        fi
    done
fi

# Category 6: Old logs (optional)
echo ""
read -p "🔵 Archive old MI logs? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "  🔵 Old MI logs..."
    for file in logs/market_intelligence_prev_snapshot.json \
                logs/market_intelligence.jsonl \
                logs/mi_structured.jsonl; do
        if [ -f "$file" ]; then
            mv "$file" "${BACKUP_DIR}/" && echo "     ✓ $file"
        fi
    done
fi

# Create archive
echo ""
echo "📦 Creating archive: ${ARCHIVE_NAME}"
tar -czf "${ARCHIVE_NAME}" "${BACKUP_DIR}/"

# Remove backup directory
rm -rf "${BACKUP_DIR}"

echo ""
echo "✅ Cleanup complete!"
echo ""
echo "📊 Summary:"
echo "   • Archive created: ${ARCHIVE_NAME}"
echo "   • Files can be restored from archive if needed"
echo ""
echo "🗑️  To permanently delete backup after testing:"
echo "   rm ${ARCHIVE_NAME}"
echo ""
echo "📋 Next steps:"
echo "   1. Run tests: pytest tests/ -v"
echo "   2. Check bot: ps aux | grep 'python main.py'"
echo "   3. Test dashboard: streamlit run dashboard.py"
echo ""
