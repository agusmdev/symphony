from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from symphony.app import SymphonyApp
from symphony.errors import SymphonyError
from symphony.workflow import select_workflow_path


def main() -> int:
    parser = argparse.ArgumentParser(prog="symphony")
    parser.add_argument("workflow", nargs="?", help="path to WORKFLOW.md")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--log-file",
        default=None,
        help="also append symphony logs to this file (in addition to stderr)",
    )
    args = parser.parse_args()
    _configure_logging(args.log_level, args.log_file)
    path = select_workflow_path(args.workflow).resolve()
    if args.workflow is not None and not Path(args.workflow).expanduser().exists():
        print(f"workflow file does not exist: {path}", file=sys.stderr)
        return 2
    try:
        app = SymphonyApp(path)
        asyncio.run(app.run())
    except KeyboardInterrupt:
        return 0
    except SymphonyError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"symphony failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _configure_logging(level: str, log_file: str | None) -> None:
    log_level = getattr(logging, str(level).upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s level=%(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(log_level)
    for existing in list(root.handlers):
        root.removeHandler(existing)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


if __name__ == "__main__":
    raise SystemExit(main())
