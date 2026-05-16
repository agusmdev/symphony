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
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s level=%(levelname)s %(name)s %(message)s",
    )
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


if __name__ == "__main__":
    raise SystemExit(main())
