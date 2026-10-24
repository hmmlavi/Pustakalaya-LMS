"""Process expired READY_FOR_PICKUP reservations safely and idempotently."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.infrastructure.database import create_db_engine, create_session_factory
from app.services.reservation_service import ReservationService


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Expire unclaimed pickup holds and reallocate copies to next FIFO waiting members."
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()

    cfg = AppConfig(database_url=args.database_url)
    engine = create_db_engine(cfg.DATABASE_URL)
    try:
        session_factory = create_session_factory(engine)
        svc = ReservationService(
            session_factory,
            library_timezone=cfg.LIBRARY_TIMEZONE,
            pickup_hold_days=cfg.PICKUP_HOLD_DAYS,
        )
        expired = svc.process_expired_reservations()
        print(f"[OK] Processed {len(expired)} expired pickup reservation(s).")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
