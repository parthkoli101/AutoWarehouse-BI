"""
quality.py
Generates a consolidated data quality report (JSON + printed summary)
across all source tables, saved to QUALITY_REPORT_DIR for inclusion
in your project report / IEEE paper appendix.
"""

import json
import logging
from datetime import datetime
from config import QUALITY_REPORT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("quality")


def generate_report(quality_reports: list) -> dict:
    overall_avg_score = round(
        sum(r["quality_score"] for r in quality_reports) / len(quality_reports), 1
    ) if quality_reports else 0

    report = {
        "generated_at": datetime.now().isoformat(),
        "overall_quality_score": overall_avg_score,
        "tables": quality_reports,
    }

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = QUALITY_REPORT_DIR / f"quality_report_{timestamp}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"Quality report saved to {out_path}")
    logger.info(f"Overall warehouse quality score: {overall_avg_score}/100")

    print("\n" + "=" * 70)
    print(f"{'TABLE':<22}{'ROWS':>10}{'NULL%':>10}{'DUPE%':>10}{'OUTLIER%':>10}{'SCORE':>8}")
    print("=" * 70)
    for r in quality_reports:
        print(f"{r['table']:<22}{r['rows']:>10,}{r['null_pct']:>10}{r['duplicate_pct']:>10}"
              f"{r['outlier_pct']:>10}{r['quality_score']:>8}")
    print("=" * 70)
    print(f"{'OVERALL':<22}{'':>10}{'':>10}{'':>10}{'':>10}{overall_avg_score:>8}\n")

    return report


if __name__ == "__main__":
    from extract import extract_all
    from clean import clean_all
    raw = extract_all()
    _, reports = clean_all(raw)
    generate_report(reports)
