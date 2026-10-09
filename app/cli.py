from __future__ import annotations

import argparse
from datetime import datetime

from app.config import load_settings
from app.service import (
    DeviceError,
    approve_order,
    batch_create_devices,
    bind_device_user,
    create_device,
    create_user,
    disable_device,
    enable_device,
    ensure_ready,
    get_device,
    import_devices,
    list_orders,
    list_usage,
    list_devices,
    list_users,
    parse_optional_int,
    renew_device,
    rotate_device_key,
)


def fmt_ts(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def cmd_init_db(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    print(f"database={settings.database_path}")


def cmd_create_user(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    user = create_user(settings.database_path, args.username, args.display_name, args.contact, args.note)
    print(f"user_id={user['id']}")
    print(f"username={user['username']}")
    print(f"display_name={user['display_name']}")


def cmd_list_users(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    for user in list_users(settings.database_path):
        print(
            "\t".join(
                [
                    str(user["id"]),
                    user["username"],
                    user["display_name"],
                    user["contact"],
                    user["status"],
                ]
            )
        )


def cmd_create_device(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    key, device = create_device(
        settings.database_path,
        args.device_id,
        args.days,
        args.note,
        args.user_id,
        device_key=getattr(args, "key", None),
    )
    print(f"device_id={device['device_id']}")
    print(f"api_key={key}")
    print(f"base_url={settings.public_base_url}/v1")
    print("model=deepseek-v4.1-flash")
    print(f"expires_at={fmt_ts(device['expires_at'])}")


def cmd_batch_create(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    rows = batch_create_devices(settings.database_path, args.prefix, args.count, args.days, args.note, args.user_id)
    print("device_id,api_key,base_url,model,expires_at")
    for key, device in rows:
        print(
            f"{device['device_id']},{key},{settings.public_base_url}/v1,"
            f"deepseek-v4.1-flash,{fmt_ts(device['expires_at'])}"
        )


def cmd_import_devices(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    if args.file == "-":
        import sys

        raw = sys.stdin.read()
    else:
        with open(args.file, "r", encoding="utf-8") as handle:
            raw = handle.read()
    rows = import_devices(settings.database_path, raw.splitlines(), args.days, args.note, args.user_id)
    print("device_id,api_key,base_url,model,expires_at")
    for key, device in rows:
        print(
            f"{device['device_id']},{key},{settings.public_base_url}/v1,"
            f"deepseek-v4.1-flash,{fmt_ts(device['expires_at'])}"
        )


def print_device(device: dict) -> None:
    print(f"device_id={device['device_id']}")
    print(f"user_id={device.get('user_id') or ''}")
    print(f"owner={device.get('owner_display_name') or device.get('owner_username') or ''}")
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
                    str(device.get("user_id") or ""),
                    device.get("owner_display_name") or device.get("owner_username") or "",
                    device["status"],
                    device["key_mask"],
                    fmt_ts(device["expires_at"]),
                    str(device["request_count"]),
                    str(device["total_tokens"]),
                ]
            )
        )


def cmd_bind_device(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    print_device(bind_device_user(settings.database_path, args.device_id, parse_optional_int(args.user_id)))


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


def cmd_list_orders(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    for order in list_orders(settings.database_path, args.limit):
        print(
            "\t".join(
                [
                    order["id"],
                    order["device_id"],
                    order["plan_id"],
                    order["status"],
                    str(order["amount"]),
                    order["currency"],
                    fmt_ts(order["created_at"]),
                ]
            )
        )


def cmd_rotate_key(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    key, device = rotate_device_key(settings.database_path, args.device_id)
    print(f"device_id={device['device_id']}")
    print(f"api_key={key}")
    print(f"base_url={settings.public_base_url}/v1")
    print("model=deepseek-v4.1-flash")


def cmd_list_usage(args: argparse.Namespace) -> None:
    settings = load_settings()
    ensure_ready(settings.database_path)
    for event in list_usage(settings.database_path, args.device_id, args.limit):
        print(
            "\t".join(
                [
                    str(event["id"]),
                    event["device_id"],
                    event["path"],
                    str(event["status_code"]),
                    str(event["total_tokens"]),
                    fmt_ts(event["created_at"]),
                ]
            )
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="btf-gateway")
    sub = parser.add_subparsers(required=True)

    init_db = sub.add_parser("init-db")
    init_db.set_defaults(func=cmd_init_db)

    create_user_cmd = sub.add_parser("create-user")
    create_user_cmd.add_argument("username")
    create_user_cmd.add_argument("--display-name", required=True)
    create_user_cmd.add_argument("--contact", default="")
    create_user_cmd.add_argument("--note", default="")
    create_user_cmd.set_defaults(func=cmd_create_user)

    list_users_cmd = sub.add_parser("list-users")
    list_users_cmd.set_defaults(func=cmd_list_users)

    create = sub.add_parser("create-device")
    create.add_argument("device_id")
    create.add_argument("--days", type=int, default=30)
    create.add_argument("--note", default="")
    create.add_argument("--user-id", type=int)
    create.add_argument(
        "--key",
        default=None,
        help="explicit auth key (defaults to device_id for hardware compatibility)",
    )
    create.set_defaults(func=cmd_create_device)

    batch = sub.add_parser("batch-create")
    batch.add_argument("prefix")
    batch.add_argument("--count", type=int, required=True)
    batch.add_argument("--days", type=int, default=30)
    batch.add_argument("--note", default="")
    batch.add_argument("--user-id", type=int)
    batch.set_defaults(func=cmd_batch_create)

    import_cmd = sub.add_parser("import-devices")
    import_cmd.add_argument("file", help="path to newline separated device ids, or - for stdin")
    import_cmd.add_argument("--days", type=int, default=30)
    import_cmd.add_argument("--note", default="")
    import_cmd.add_argument("--user-id", type=int)
    import_cmd.set_defaults(func=cmd_import_devices)

    get = sub.add_parser("get-device")
    get.add_argument("device_id")
    get.set_defaults(func=cmd_get_device)

    list_cmd = sub.add_parser("list-devices")
    list_cmd.set_defaults(func=cmd_list_devices)

    bind = sub.add_parser("bind-device")
    bind.add_argument("device_id")
    bind.add_argument("--user-id", default="")
    bind.set_defaults(func=cmd_bind_device)

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

    orders = sub.add_parser("list-orders")
    orders.add_argument("--limit", type=int, default=200)
    orders.set_defaults(func=cmd_list_orders)

    rotate = sub.add_parser("rotate-key")
    rotate.add_argument("device_id")
    rotate.set_defaults(func=cmd_rotate_key)

    usage = sub.add_parser("list-usage")
    usage.add_argument("--device-id")
    usage.add_argument("--limit", type=int, default=200)
    usage.set_defaults(func=cmd_list_usage)

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
