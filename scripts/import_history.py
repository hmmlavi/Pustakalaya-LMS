"""Run the idempotent historical data validation and import pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.infrastructure.database import create_db_engine, create_session_factory
from app.services.import_service import DataImportService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and import historical library records into Pustakalaya."
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--source-file",
        default=None,
        help="Path to historical JSON source file (default: instance/historical_source_records.json).",
    )
    args = parser.parse_args()

    cfg = AppConfig(database_url=args.database_url)
    source_path = (
        Path(args.source_file) if args.source_file else cfg.HISTORICAL_SOURCE_PATH
    )

    engine = create_db_engine(cfg.DATABASE_URL)
    try:
        session_factory = create_session_factory(engine)
        svc = DataImportService(
            session_factory, library_timezone=cfg.LIBRARY_TIMEZONE
        )
        report = svc.import_from_file(source_path)

        print("=== Historical Data Import Validation Report ===")
        print(f"  Batch Key          : {report.batch_key}")
        print(f"  Source File        : {report.source_filename}")
        print(f"  Total Source Rows  : {report.total_records}")
        print(f"  Accepted           : {report.accepted_count}")
        print(f"  Rejected           : {report.rejected_count}")
        print(f"  Quarantined        : {report.quarantined_count}")
        print(f"  Duplicates Skipped : {report.duplicate_skipped_count}")
        print(f"  Unexpected Errors  : {report.error_count}")
        if report.issue_counts_by_code:
            print("  Findings by Issue Code:")
            for code, cnt in sorted(report.issue_counts_by_code.items()):
                print(f"    - {code:<34}: {cnt}")
    finally:
        engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
