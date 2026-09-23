from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.db import connect, init_db, now_ts
from app.security import hash_key


CUSTOMERS = [
    (4, "customer_alpha", "客户A"),
    (5, "customer_beta", "客户B"),
    (6, "customer_gamma", "客户C"),
]

PLANS = [1, 3, 7, 30]


def plan_name(days: int) -> str:
    return f"{days}天套餐"


def main() -> None:
    settings = load_settings()
    init_db(settings.database_path)
    ts = now_ts()

    with connect(settings.database_path) as conn:
        user_ids: list[int] = []
        for user_id, username, display_name in CUSTOMERS:
            conn.execute(
                """
                insert into users (id, username, display_name, contact, status, created_at, updated_at, note)
                values (?, ?, ?, '', 'active', ?, ?, 'new-api真实登录客户')
                on conflict(username) do update set
                  id = excluded.id,
                  display_name = excluded.display_name,
                  status = 'active',
                  updated_at = excluded.updated_at,
                  note = excluded.note
                """,
                (user_id, username, display_name, ts, ts),
            )
            row = conn.execute("select id from users where username = ?", (username,)).fetchone()
            user_ids.append(int(row["id"]))

        index = 0
        for user_idx, user_id in enumerate(user_ids):
            for device_idx in range(3):
                index += 1
                days = PLANS[(index - 1) % len(PLANS)]
                mac = f"BTF:AI:{user_idx + 1:02X}:{device_idx + 1:02X}:00:{index:02X}"
                name = f"客户{chr(ord('A') + user_idx)}-{device_idx + 1}号设备"
                note = f"{name} / {plan_name(days)}"
                expires_at = ts + days * 86400
                request_count = index * 13
                prompt_tokens = index * 120
                completion_tokens = index * 80
                total_tokens = prompt_tokens + completion_tokens
                last_seen_at = ts - index * 1800
                conn.execute(
                    """
                    insert into devices (
                      device_id, user_id, key_hash, key_mask, status, expires_at,
                      created_at, updated_at, last_seen_at, request_count,
                      prompt_tokens, completion_tokens, total_tokens, note
                    )
                    values (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    on conflict(device_id) do update set
                      user_id = excluded.user_id,
                      key_hash = excluded.key_hash,
                      key_mask = excluded.key_mask,
                      status = 'active',
                      expires_at = excluded.expires_at,
                      updated_at = excluded.updated_at,
                      last_seen_at = excluded.last_seen_at,
                      request_count = excluded.request_count,
                      prompt_tokens = excluded.prompt_tokens,
                      completion_tokens = excluded.completion_tokens,
                      total_tokens = excluded.total_tokens,
                      note = excluded.note
                    """,
                    (
                        mac,
                        user_id,
                        hash_key(mac),
                        mac,
                        expires_at,
                        ts,
                        ts,
                        last_seen_at,
                        request_count,
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        note,
                    ),
                )

    print("seeded customers=3 devices=9")


if __name__ == "__main__":
    main()
