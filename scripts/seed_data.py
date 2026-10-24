"""Deterministically seed Pustakalaya branches, tiers, holidays, 2,000 books, copies, 500 members, and historical source fixture."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.infrastructure.database import (
    apply_migrations,
    create_db_engine,
    create_session_factory,
)
from app.infrastructure.seed import seed_operational_dataset


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed deterministic Pustakalaya dataset and generate historical source fixture."
    )
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--reference-date",
        default=None,
        help="ISO date (YYYY-MM-DD) anchoring the 1-year historical window.",
    )
    parser.add_argument("--historical-days", type=int, default=None)
    parser.add_argument(
        "--historical-output",
        default=None,
        help="Output path for historical source JSON fixture.",
    )
    args = parser.parse_args()

    cfg = AppConfig(database_url=args.database_url)
    resolved_url = apply_migrations(cfg.DATABASE_URL)

    seed_val = args.seed if args.seed is not None else cfg.SEED_RANDOM_SEED
    ref_date = (
        date.fromisoformat(args.reference_date)
        if args.reference_date
        else cfg.HISTORICAL_REFERENCE_DATE
    )
    hist_days = (
        args.historical_days
        if args.historical_days is not None
        else cfg.HISTORICAL_DAYS
    )
    out_path = (
        Path(args.historical_output)
        if args.historical_output
        else cfg.HISTORICAL_SOURCE_PATH
    )

    engine = create_db_engine(resolved_url)
    try:
        session_factory = create_session_factory(engine)
        summary = seed_operational_dataset(
            session_factory,
            random_seed=seed_val,
            reference_date=ref_date,
            historical_days=hist_days,
            historical_output_path=out_path,
        )
        print("=== Pustakalaya Seed Summary ===")
        print(f"  Branches           : {summary.branches}")
        print(f"  Membership Tiers   : {summary.tiers}")
        print(f"  Library Holidays   : {summary.holidays}")
        print(f"  Authors            : {summary.authors}")
        print(f"  Catalog Books      : {summary.books}")
        print(f"  Physical Copies    : {summary.copies}")
        print(f"  Registered Members : {summary.members}")
        print(f"  Historical Source  : {summary.historical_source_file}")
        print(f"  Source Records     : {summary.historical_records_generated}")
    finally:
        engine.dispose()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
