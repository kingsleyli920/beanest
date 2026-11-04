import argparse
import asyncio
import json
from pathlib import Path
import uuid

from .adapters import doctor
from .coordinator import demo


def main():
    parser = argparse.ArgumentParser(description="Local Agent Coordinator bounded prototype")
    parser.add_argument("command", choices=["doctor", "demo", "show"])
    parser.add_argument("--runtime", type=Path, default=Path("runtime"))
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if args.timeout <= 0 or args.timeout > 600:
        parser.error("timeout must be in (0, 600]")
    if args.command == "doctor":
        result = doctor()
    elif args.command == "show":
        try:
            run_id = str(uuid.UUID(args.run_id or ""))
        except ValueError:
            parser.error("show requires a UUID --run-id")
        root = args.runtime.resolve()
        target = (root / run_id / "summary.json").resolve()
        if not target.is_relative_to(root):
            parser.error("summary must be inside runtime")
        try:
            result = json.loads(target.read_text())
        except (OSError, ValueError):
            parser.exit(1, "Run summary unavailable or invalid; inspect the selected runtime directory.\n")
    else:
        try:
            result = asyncio.run(demo(args.runtime.resolve(), args.timeout))
        except KeyboardInterrupt:
            raise SystemExit(130)
        except OSError:
            parser.exit(1, "Runtime storage unavailable; check directory access and free space.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") in {"failed", "cancelled"} or result.get("ready") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
