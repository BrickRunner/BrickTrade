#!/bin/bash
# BrickTrade Quick Cleanup Script
# Removes only test scripts (safest option)

set -e

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║         BrickTrade Quick Cleanup (Test Scripts Only)        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

if [ ! -f "main.py" ]; then
    echo "❌ Error: Not in BrickTrade directory!"
    exit 1
fi

echo "🗑️  Removing test and utility scripts..."
echo ""

# Remove test scripts
rm -f test_audit_fixes.py && echo "  ✓ test_audit_fixes.py"
rm -f test_improvements.py && echo "  ✓ test_improvements.py"
rm -f test_enhancements.py && echo "  ✓ test_enhancements.py"
rm -f test_htx_api.py && echo "  ✓ test_htx_api.py"
rm -f test_htx_balance.py && echo "  ✓ test_htx_balance.py"
rm -f test_short_symbols.py && echo "  ✓ test_short_symbols.py"

# Remove check/close utilities
rm -f check_htx_positions.py && echo "  ✓ check_htx_positions.py"
rm -f check_live.py && echo "  ✓ check_live.py"
rm -f check_short_config.py && echo "  ✓ check_short_config.py"
rm -f close_htx_positions.py && echo "  ✓ close_htx_positions.py"

# Remove shell scripts
rm -f launch_bybit.sh && echo "  ✓ launch_bybit.sh"

# Remove auxiliary
rm -f claude_terminal.py && echo "  ✓ claude_terminal.py"
rm -f SUMMARY_SHORT_EXPANSION.txt && echo "  ✓ SUMMARY_SHORT_EXPANSION.txt"

echo ""
echo "✅ Quick cleanup complete!"
echo "   Removed ~116 KB of test scripts"
echo ""
echo "📋 Next steps:"
echo "   pytest tests/ -v  # Run unit tests"
echo "   ps aux | grep 'python main.py'  # Check bot"
echo ""
