from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import shutil
import sys
from pathlib import Path


def _resource_root() -> Path:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled)
    return Path(__file__).resolve().parents[2]


def _install(args: argparse.Namespace) -> int:
    try:
        service_account = pwd.getpwnam(args.service_user)
    except KeyError as error:
        raise ValueError(f"Service user does not exist: {args.service_user}") from error
    if os.geteuid() != 0 and os.getuid() != service_account.pw_uid:
        raise PermissionError("Run as root to install for a different service user")

    prefix = Path(args.prefix).expanduser().resolve()
    config_dir = Path(args.config_dir).expanduser().resolve()
    unit_dir = Path(args.unit_dir).expanduser().resolve()
    package_dir = prefix / "lib" / "pi_media_hub"
    config_path = config_dir / "config.json"
    unit_path = unit_dir / "pi-media-hub.service"
    resource_root = _resource_root()
    source_package = resource_root / "pi_media_hub"
    if not source_package.is_dir():
        source_package = Path(__file__).resolve().parent
    required_modules = ("__init__.py", "__main__.py", "server.py")
    missing_modules = [name for name in required_modules if not (source_package / name).is_file()]
    if missing_modules:
        raise FileNotFoundError(
            f"Bundled server package is incomplete at {source_package}: {', '.join(missing_modules)}"
        )
    sample_config = resource_root / "share" / "pi-media-hub" / "config.example.json"
    if not sample_config.is_file():
        sample_config = resource_root / "config.example.json"
    if not sample_config.is_file():
        raise FileNotFoundError(f"Could not find bundled sample config: {sample_config}")

    package_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_package, package_dir, dirs_exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config = json.loads(sample_config.read_text(encoding="utf-8"))
        config["media_root"] = str(Path(args.media_root).expanduser().resolve())
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o640)
    if os.geteuid() == 0:
        os.chown(config_path, service_account.pw_uid, service_account.pw_gid)
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = f"""[Unit]
Description=Pi Media Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={args.service_user}
Environment=PYTHONPATH={prefix / "lib"}
ExecStart=/usr/bin/python3 -m pi_media_hub.server --config {config_path}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
ReadOnlyPaths={Path(args.media_root).expanduser().resolve()}

[Install]
WantedBy=multi-user.target
"""
    unit_path.write_text(unit, encoding="utf-8")
    print(f"Installed application: {package_dir}")
    print(f"Configuration: {config_path}")
    print(f"Systemd unit: {unit_path}")
    print("Review the service user and config, then run:")
    print("  sudo systemctl daemon-reload")
    print("  sudo systemctl enable --now pi-media-hub")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install Pi Media Hub files and a systemd unit")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install", help="Install files; systemd commands are printed, not executed")
    install.add_argument("--prefix", default="/opt/pi-media-hub")
    install.add_argument("--config-dir", default="/etc/pi-media-hub")
    install.add_argument("--unit-dir", default="/etc/systemd/system")
    install.add_argument("--media-root", default="/srv/media")
    install.add_argument("--service-user", default=os.environ.get("SUDO_USER", getpass.getuser()))
    install.set_defaults(handler=_install)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
