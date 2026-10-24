"""Start Pustakalaya locally and open the browser after the server is ready."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from werkzeug.serving import make_server

from app import create_app
from app.config import AppConfig
from app.infrastructure.database import apply_migrations


def _data_dir() -> Path:
    configured = os.getenv("PUSTAKALAYA_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Pustakalaya"
    return Path.home() / ".pustakalaya"


def main() -> int:
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("PUSTAKALAYA_DATA_DIR", str(data_dir))
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    url = f"http://{host}:{port}/auth/login"

    # A packaged deployment has no development database. Migrations are
    # idempotent and preserve an existing user's database during upgrades.
    cfg = AppConfig()
    apply_migrations(cfg.DATABASE_URL)
    server = make_server(host, port, create_app())
    thread = threading.Thread(target=server.serve_forever, name="pustakalaya-server", daemon=True)
    thread.start()
    for _ in range(50):
        try:
            with urllib.request.urlopen(url, timeout=0.5):
                break
        except urllib.error.HTTPError:
            # The application is listening even when it returns an expected
            # HTTP error (for example, before first-run setup is complete).
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.1)
    else:
        server.shutdown()
        raise RuntimeError(f"Pustakalaya did not start on {url}")

    webbrowser.open(url)
    print(f"Pustakalaya is running at {url}. Close this window to stop it.")
    try:
        while thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
