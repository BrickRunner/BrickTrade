#!/usr/bin/env python3
"""
Run daily calibration to analyze logs and recommend parameter adjustments.
"""
import asyncio
import json
import sys
from datetime import datetime

from arbitrage.system.calibrator import DailyCalibrator


async def main():
    """Run calibration for today or specified date."""
    date_str = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")

    print(f"Running calibration for {date_str}...")
    print("-" * 60)

    calibrator = DailyCalibrator()
    report = await calibrator.run(date_str)

    print(f"\n📊 CALIBRATION REPORT: {report.date}")
    print("=" * 60)

    # Metrics
    print("\n📈 METRICS:")
    for key, value in report.metrics.items():
        if isinstance(value, dict):
            print(f"\n  {key}:")
            for k, v in value.items():
                print(f"    {k}: {v}")
        else:
            print(f"  {key}: {value}")

    # Recommendations
    print("\n💡 RECOMMENDATIONS:")
    if not report.recommendations:
        print("  ✅ No adjustments needed — parameters look good!")
    else:
        for key, rec in report.recommendations.items():
            print(f"\n  🔧 {key}:")
            if isinstance(rec, dict):
                for k, v in rec.items():
                    print(f"      {k}: {v}")
            else:
                print(f"      {rec}")

    # Summary
    print("\n" + "=" * 60)
    print(f"Report saved to: logs/calibration/{report.date}.json")
    print("-" * 60)

    return report


if __name__ == "__main__":
    report = asyncio.run(main())
