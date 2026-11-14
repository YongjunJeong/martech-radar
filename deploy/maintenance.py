"""Portable daily review entrypoint; uses the deployment's explicit KST schedule."""

from datetime import datetime
import argparse
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
from radar import operations, workspace

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['review'])
    parser.add_argument('--output-dir', type=Path, default=workspace.path('data/operations/daily-reviews'))
    args = parser.parse_args()
    day = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    sys.argv = [
        sys.argv[0],
        "--day",
        day,
        "--output",
        str(args.output_dir / (day + '.json')),
    ]
    operations.main()
