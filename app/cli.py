from __future__ import annotations

import argparse
from datetime import datetime

from app.config import load_settings
from app.service import (
    DeviceError,
    approve_order,
    create_device,
    disable_device,
    enable_device,
    ensure_ready,
    get_device,
    list_devices,
    renew_device,
)


def fmt_ts(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def cmd_init_db(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    print(f"database={settings.database_path}")


def cmd_create_device(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    key, device = create_device(settings.database_path, args.device_id, args.days, args.note)
    print(f"device_id={device['device_id']}")
    print(f"api_key={key}")
    print(f"base_url={settings.public_base_url}/v1")
    print("model=deepseek-v4.1-flash")
    print(f"expires_at={fmt_ts(device['expires_at'])}")


def print_device(device: dict) -> None:
    print(f"device_id={device['device_id']}")
    print(f"status={device['status']}")
    print(f"key={device['key_mask']}")
    print(f"expires_at={fmt_ts(device['expires_at'])}")
    print(f"request_count={device['request_count']}")
    print(f"total_tokens={device['total_tokens']}")


def cmd_get_device(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    print_device(get_device(settings.database_path, args.device_id))


def cmd_list_devices(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    for device in list_devices(settings.database_path):
        print(
            "\t".join(
                [
                    device["device_id"],
                    device["status"],
                    device["key_mask"],
                    fmt_ts(device["expires_at"]),
                    str(device["request_count"]),
                    str(device["total_tokens"]),
                ]
            )
        )


def cmd_renew_device(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    print_device(renew_device(settings.database_path, args.device_id, args.days))


def cmd_disable_device(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    print_device(disable_device(settings.database_path, args.device_id))


def cmd_enable_device(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    print_device(enable_device(settings.database_path, args.device_id, args.days_if_expired))


def cmd_approve_order(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    order = approve_order(settings.database_path, args.order_id)
    print(f"order_id={order['id']}")
    print(f"device_id={order['device_id']}")
    print(f"status={order['status']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="btf-gateway")
    sub = parser.add_subparsers(required=True)

    init_db = sub.add_parser("init-db")
    init_db.set_defaults(func=cmd_init_db)

    create = sub.add_parser("create-device")
    create.add_argument("device_id")
    create.add_argument("--days", type=int, default=30)
    create.add_argument("--note", default="")
    create.set_defaults(func=cmd_create_device)

    get = sub.add_parser("get-device")
    get.add_argument("device_id")
    get.set_defaults(func=cmd_get_device)

    list_cmd = sub.add_parser("list-devices")
    list_cmd.set_defaults(func=cmd_list_devices)

    renew = sub.add_parser("renew-device")
    renew.add_argument("device_id")
    renew.add_argument("--days", type=int, default=30)
    renew.set_defaults(func=cmd_renew_device)

    disable = sub.add_parser("disable-device")
    disable.add_argument("device_id")
    disable.set_defaults(func=cmd_disable_device)

    enable = sub.add_parser("enable-device")
    enable.add_argument("device_id")
    enable.add_argument("--days-if-expired", type=int, default=30)
    enable.set_defaults(func=cmd_enable_device)

    approve = sub.add_parser("approve-order")
    approve.add_argument("order_id")
    approve.set_defaults(func=cmd_approve_order)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except DeviceError as exc:
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()

