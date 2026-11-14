"""Render systemd units into a NEW folder. Never install, enable, or restart them."""

import argparse
from pathlib import Path
import re


def render(destination, deployment, workspace):
    for value in (deployment, workspace):
        if not value.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(value)):
            raise ValueError("Use an absolute path without spaces or shell metacharacters")
    destination.mkdir(mode=0o700)
    compose = f"/usr/bin/docker compose --env-file {deployment}/hosting.env -f {deployment}/compose.hosting.yaml"
    for job, at, delay in [
        ("scan", "03:00:00", "20m"),
        ("backup", "02:30:00", "5m"),
        ("review", "09..15:15:00", "0"),
    ]:
        service = {"scan": "scheduled", "backup": "backup", "review": "review"}[job]
        condition = (
            f"ConditionPathExists={workspace}/.collection-enabled\n" if job == "scan" else ""
        )
        name = f"radar-hosting-{job}"
        unit = f"""[Unit]
Description=MarTech Radar hosting {job}
Requires=docker.service
After=docker.service network-online.target
ConditionPathExists={workspace}/.radar-hosting
{condition}
[Service]
Type=oneshot
WorkingDirectory={deployment}
ExecStart={compose} --profile jobs run --rm --no-deps --name {name} {service}
TimeoutStartSec=infinity
TimeoutStopSec=150
ExecStop=-/usr/bin/docker stop --time 120 {name}
Restart=no
UMask=0077
"""
        (destination / (name + ".service")).write_text(unit)
        (destination / (name + ".timer")).write_text(f"""[Unit]
Description=MarTech Radar hosting {job} schedule

[Timer]
OnCalendar=*-*-* {at} Asia/Seoul
RandomizedDelaySec={delay}
Persistent=true
Unit={name}.service

[Install]
WantedBy=timers.target
""")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    print(render(args.destination, args.deployment, args.workspace))
