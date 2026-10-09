# BTF API 项目交接总文档

更新时间：2026-10-09（Asia/Shanghai）

本文档用于把当前 BTF API / New API 项目交给下一位开发者或 Claude 继续维护。文档以当前工作区、线上容器和数据库实测结果为准，不以历史聊天中的计划或未验证描述为准。

## 1. 项目结论

当前系统已经形成以下产品闭环：

```text
New API 用户账号
    |
    +-- 绑定 0..N 台硬件设备
              |
              +-- 设备唯一标识：MAC / device_id
              +-- 初始 API Key：MAC
              +-- 当前套餐：1小时 / 1天 / 3天 / 7天 / 30天
              +-- DeepSeek 默认聊天
              +-- GPT-5.5 可选聊天并自动联网
              +-- 月卡可使用图片生成
              +-- 设备状态、用量、订单、续费
```

系统不是另起一个用户系统，而是在现有 New API 控制台和登录体系上增加设备、套餐、订单和设备用量能力。

当前可以确认已经完成并线上运行：

- BTF Device Gateway Docker 服务。
- 现有 New API UI 和登录体系继续作为浏览器入口。
- 用户与设备的一对多绑定。
- MAC 作为设备唯一标识和初始 API Key。
- 单台创建、批量创建、绑定、启用、禁用、续期、换 Key。
- 设备套餐、请求限制、Token 限制、RPM、并发、生图额度。
- 过期设备查看状态和创建续费订单，但不能调用 `/v1`。
- 订单创建、手工确认、支付回调共用幂等的 `approve_order` 流程。
- DeepSeek 默认模型。
- GPT-5.5 可选模型，自动加入 `web_search` 工具。
- 月卡图片生成、图片落盘、图片公网 URL、图片列表。
- 用户只能看到自己绑定的设备、订单、用量和图片。
- 管理员可以查看和管理全部设备。
- 客户用量兼容 New API 原有日志、统计和图表接口。
- 全量 Python 测试见测试套件（快照日期 2026-10-09 时为 `224+ passed`）。不要在本文件硬编码精确测试数量，避免再次漂移。

尚未完成或需要外部资料才能完成：

- 微信支付和支付宝真实商户配置、真实支付回调验收。
- HTTPS 域名、支付回调域名和生产 Cookie 安全配置。
- Android 工程的最终 UI 真机验收。当前 `/opt` 没有 Android 工程、APK 或 Gradle 工程。
- 语音 STT/TTS/Android MCU 项目不在当前 BTF Gateway 仓库内，不能把它们当作本仓库已完成能力。

## 1.1 最近需求状态总表

下面按最近讨论过的真实需求列出状态。`已实现`表示代码和当前线上环境已经具备；`代码已准备`表示本仓库有实现，但还缺生产资料或外部工程验收；`未纳入本仓库`表示不能把它误报成 BTF Gateway 已完成。

| 需求 | 状态 | 当前结论 |
|---|---|---|
| Clone New API，并改成 BTF API 网关 | 已实现 | New API 继续负责账号、登录、渠道和模型路由；BTF Gateway 位于 `/v1` 前面做设备策略 |
| Docker 部署 | 已实现 | `btf-device-gateway`、`new-api`、PostgreSQL、Redis 当前运行 |
| 保留原 New API UI 和登录体系 | 已实现 | 设备接口嵌入现有 Dashboard，客户不需要第二套 BTF 浏览器登录 |
| 管理员和客户权限 | 已实现 | 管理员管理全部设备；客户只看自己的设备、订单、用量和图片 |
| 一个用户绑定多台设备 | 已实现 | `New API user 1 -> N BTF devices` |
| 用 MAC 作为设备唯一标识和初始 Key | 已实现 | 代码将 `device_id` 作为稳定标识；真实设备应使用真实 MAC |
| 单台/批量创建设备 | 已实现 | 支持单条、前缀批量、换行/逗号分隔导入 |
| 绑定、解绑、启用、禁用、换 Key | 已实现 | Key 轮换后旧 Key 立即失效 |
| 1 小时、1/3/7/30 天套餐 | 已实现 | 套餐时长、模型权限、生图权限、Token、请求、RPM、并发均由 BTF 强制检查 |
| 客户查看 Token 和使用日志 | 已实现 | 已接入客户范围过滤；历史事件通过 `user_id` 或设备归属兼容查询 |
| 设备过期仍可查看状态和创建续费订单 | 已实现 | 过期设备不能调用 `/v1`，但可查状态并续费 |
| 续费订单和确认开通 | 已实现 | 手工确认、Mock、支付回调共用幂等 `approve_order` |
| 微信/支付宝真实支付 | 代码已准备 | 签名、回调、金额校验和幂等已有；还没有商户号、证书、私钥、HTTPS 回调资料 |
| DeepSeek 默认聊天 | 已实现 | `deepseek-v4.1-flash` 是默认聊天模型 |
| GPT-5.5 联网聊天 | 已实现 | 选择 `gpt-5.5` 时网关自动追加 `web_search` |
| 套餐控制 DeepSeek/GPT/图片权限 | 已实现 | 套餐 `chat_models` 和 `image_models` 控制 `/v1/models` 及请求 |
| 图片生成、落盘、网页查看、图片列表 | 已实现 | 统一到 `[b2]gpt-image-2`，通过 `/device-images/...` 返回公网图片 |
| 500/403/429 关键错误处理 | 已实现 | 登录 Session refresh、设备权限、过期、禁用、额度和 RPM 均有对应处理；仍需持续观察生产日志 |
| Android 动态模型选择和图片调用 | 代码已准备 | 服务端协议已完成，但当前 `/opt` 没有 Android 工程，无法做真机验收 |
| 语音识别、语音合成、MCU 完整链路 | 未纳入本仓库 | 需要先定位独立 Android/MCU 项目和其外部服务配置 |

## 1.2 设备标识的重要澄清

当前预览数据里的 `BTF:AI:...` 是脚本生成的**合成设备标识**，不是手机或 SMT32 的真实 MAC。例如：

```text
BTF:AI:01:01:00:01
BTF:AI:02:01:00:04
BTF:AI:03:03:00:09
```

网关不会解析 MAC 的厂商段，它只把 `device_id` 当作不透明的稳定字符串并规范化为大写。因此：

- 真实硬件上线时，应把固化 MAC 写入 `device_id`，并把同一个 MAC 作为初始 `Authorization: Bearer` Key。
- `BTF:AI:...` 设备是演示/测试数据，不能据此断定它是实际硬件 MAC。
- `BTF:AI:02:01:00:04` 在当前预览数据库中归属 `customer_beta`，但这是测试数据映射，不代表正式客户资产。
- 设备 Key 轮换后，当前 Key 不再等于 MAC；硬件必须保存管理员返回的新 Key。
- 不要把完整设备 Key 写入交接文档、日志或公开截图。Claude 可在服务器上用数据库和受保护接口核对具体归属。

## 2. 目录和部署位置

### 2.1 BTF Gateway

```text
/opt/btf-device-gateway/
  app/
    main.py          FastAPI 入口、路由、代理、权限、用量、图片处理
    db.py            SQLite Schema、套餐默认值、数据库初始化和迁移
    service.py       设备、用户绑定、订单、支付状态、用量服务
    payments.py      Mock、微信支付、支付宝支付抽象及签名处理
    security.py      Key 哈希和安全辅助函数
    config.py        环境变量配置
    models.py        Pydantic/数据模型
    cli.py           CLI 管理命令
  data/
    btf-device-gateway.sqlite3 线上 BTF 主数据库，不能删除
    generated-images/           生成图片落盘目录
  new-api-dist/                 现有 New API 前端静态资源
  docker-compose.yml            BTF 容器编排
  Dockerfile
  requirements.txt
  tests/
  scripts/
  docs/
```

### 2.2 New API

```text
/opt/new-api/
  compose.yml
  .env                    New API、PostgreSQL、Redis 配置，包含敏感信息
  .admin-credentials      New API 管理员登录凭据，包含敏感信息
  data/
  logs/
```

New API 依赖：

- `new-api`
- `new-api-postgres`
- `new-api-redis`

BTF Gateway 依赖：

- 容器名：`btf-device-gateway`
- 公网端口：`80 -> 8080`
- New API 内部地址：`http://new-api:3000`
- BTF 数据目录：容器内 `/data`，宿主机挂载到 `/opt/btf-device-gateway/data`

当前 Git：

- 分支：`master`
- 没有配置 Git remote。
- 工作区可能包含未提交修改，不能执行 `git reset --hard` 或覆盖这些修改。
- HEAD 会随时间推移；交接时以实际 `git log` 为准。快照日期 2026-10-09 时为
  `58407ae fix integration image test for signed-URL gate (w12 x w15)`。
- 近期关键提交以 `git log --oneline` 为准，不要依赖本文件里固定的哈希列表。
- 本文档 `docs/CLAUDE_HANDOFF.md` 是版本库中的既有文件，交接时应随代码一起保留。

## 3. 系统所有权边界

| 能力 | 系统 |
|---|---|
| 浏览器登录、用户名、密码、角色、Session | New API |
| 客户账号 | New API PostgreSQL `users` |
| New API Token 和上游渠道 | New API PostgreSQL |
| 设备 MAC、当前设备 Key、绑定关系 | BTF SQLite |
| 套餐、设备权限和设备限额 | BTF SQLite |
| 续费订单 | BTF SQLite |
| 支付意图和支付交易 | BTF SQLite |
| 微信/支付宝回调 | BTF Gateway |
| `/v1` 前置设备认证 | BTF Gateway |
| New API 上游模型路由 | New API |
| BTF 设备请求和 Token 用量 | BTF SQLite |
| 生成图片文件 | BTF `generated-images` |

关键安全边界：

- 客户只拿设备 Key。
- 客户不能拿到 `NEW_API_TOKEN`。
- 客户浏览器使用 New API 的 Access Token / HttpOnly Refresh Cookie。
- 设备 Key 和浏览器登录不是同一种凭据。
- 网关没有独立的 admin 密码/内部 admin token，管理操作复用 New API 会话。
## 4. 用户、设备和 MAC Key 模型

### 4.1 数据关系

```text
New API user.id
    |
    +-- BTF devices.user_id
              |
              +-- device_id       稳定设备身份，当前使用 MAC
              +-- key_hash        当前 Key 的 SHA-256
              +-- key_mask        显示用掩码
              +-- status          active / disabled
              +-- expires_at      套餐有效期
              +-- plan_id         当前套餐
              +-- request_count
              +-- prompt_tokens
              +-- completion_tokens
              +-- total_tokens
```

### 4.2 初始 Key

创建设备时：

```text
device_id = 设备 MAC（稳定标识）
current_key = device_id     # 初始便捷值，不是永久等价
```

硬件请求：

```http
Authorization: Bearer <MAC-or-current-device-key>
```

MAC 应规范化为大写并去除首尾空格。设备 Key 不是直接明文存储，数据库只保存
`sha256(key)` 哈希和显示掩码。`device_id` 是查找身份，本身不能单独作为网关凭据；
初始 Key 为了方便首次配网才等于 `device_id`，轮换后二者不再相等。

### 4.3 换 Key

管理员换 Key 后：

- 设备身份 `device_id` 不变。
- 用户绑定不变。
- 套餐、订单、用量不变。
- 新 Key 是随机值，不再等于 MAC / `device_id`。
- 旧 Key 立即失效。
- 硬件必须更新为新 Key。

### 4.4 绑定码（bind_code）

管理员可以在创建设备时配置绑定码（`bind_code` / `generate_bind_code`）。绑定码
是一个短口令，用于把已创建的设备认领到当前登录用户：

```text
POST /api/device/bind   {mac, bind_code}
```

网关只保存 `hash(bind_code)` 和 `bind_code_mask`，校验通过后写入 `user_id` 与
`bound_at`。绑定码不是设备 API Key，也不用于 `/v1` 调用。已绑定他用户的设备会
拒绝再次绑定。

## 5. 设备管理功能

已经实现的管理能力：

- 单台创建设备。
- 批量创建设备。
- 根据 MAC / 设备标识导入。
- 设备绑定 New API 用户。
- 设备解绑或更换归属。
- 启用设备。
- 禁用设备。
- 设备续期。
- 设备 Key 轮换。
- 查看设备状态。
- 查看设备套餐和能力。
- 查看设备订单。
- 查看设备用量。
- 查看设备生成图片。

管理 API 主要入口：

```text
GET    /api/device/
POST   /api/device/
PUT    /api/device/
POST   /api/device/batch
POST   /api/device/bind
POST   /api/device/manage
GET    /api/device/{device_id}
GET    /api/device/plans
PUT    /api/device/plans/{plan_id}
GET    /api/device/usage
GET    /api/device/images
```

其中浏览器调用这些接口时使用 New API 登录 Session，不使用设备 Key。

## 6. 当前套餐

当前默认套餐如下。实际限额以线上 BTF SQLite 的 `plans` 表为准。

| 套餐 | 时长 | 聊天 | 图片 | Token 总额 | 日 Token | 请求总额 | 日请求 | 日生图 | RPM | 并发 |
|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1小时体验 | 0天特殊套餐 | DeepSeek + GPT-5.5 | 否 | 10,000 | 10,000 | 100 | 100 | 0 | 10 | 1 |
| 1天通行 | 1天 | DeepSeek + GPT-5.5 | 否 | 100,000 | 100,000 | 1,000 | 1,000 | 0 | 30 | 1 |
| 3天通行 | 3天 | DeepSeek + GPT-5.5 | 否 | 300,000 | 120,000 | 3,000 | 1,200 | 0 | 40 | 1 |
| 7天周卡 | 7天 | DeepSeek + GPT-5.5 | 否 | 800,000 | 150,000 | 8,000 | 1,500 | 0 | 50 | 2 |
| 30天月卡 | 30天 | DeepSeek + GPT-5.5 | 是 | 3,000,000 | 200,000 | 30,000 | 2,000 | 10 | 60 | 2 |

注意：

- `allow_deepseek` 是历史字段名，当前聊天套餐包含配置在 `chat_models` 中的 DeepSeek 和 GPT-5.5。
- 月卡图片模型当前为 `[b2]gpt-image-2`。
- 页面可隐藏内部限额，但网关实际会强制执行限额。
- New API 的渠道倍率和 BTF 设备套餐限额是两层概念，不能混为一谈。

## 7. 模型和 AI 能力

### 7.1 对外模型标识

当前设备 `/v1/models` 可能返回：

```text
btf-chat
deepseek-v4.1-flash
gpt-5.5
btf-image
```

产品层面不要把它们当成四个同类模型：

| 标识 | 产品含义 |
|---|---|
| `btf-chat` | DeepSeek 的兼容别名，给旧客户端使用 |
| `deepseek-v4.1-flash` | DeepSeek 真实模型名，默认聊天模型 |
| `gpt-5.5` | 可选聊天模型，自动联网 |
| `btf-image` | 图片生成兼容别名，不是聊天模型 |

Android 或其他客户端应展示：

```text
DeepSeek V4.1 Flash（默认）
GPT-5.5（联网）
图片生成（仅月卡）
```

不要把 `btf-chat` 和 `deepseek-v4.1-flash`显示为两个不同的聊天模型。

### 7.2 DeepSeek

请求：

```http
POST /v1/chat/completions
Authorization: Bearer <device-key>
Content-Type: application/json
```

```json
{
  "model": "deepseek-v4.1-flash",
  "messages": [
    {"role": "user", "content": "hello"}
  ],
  "stream": false
}
```

默认别名 `btf-chat` 会被网关转成 `deepseek-v4.1-flash`。

### 7.3 GPT-5.5 联网

客户端只需要选择：

```json
{
  "model": "gpt-5.5",
  "messages": [
    {"role": "user", "content": "请联网查询当前北京时间，并给出来源 URL。"}
  ],
  "stream": false
}
```

网关自动加入：

```json
"tools": [
  {"type": "web_search"}
]
```

已有其他 `tools` 时会追加，不重复加入。DeepSeek 不会被自动加入该工具。

GPT-5.5 响应中的 `model` 保持为 `gpt-5.5`；DeepSeek 默认响应可保持为 `btf-chat`，这是兼容行为。

### 7.4 图片生成

月卡可以请求：

```http
POST /v1/images/generations
Authorization: Bearer <device-key>
Content-Type: application/json
```

```json
{
  "model": "btf-image",
  "prompt": "一只白色的猫"
}
```

网关实际转发：

```text
btf-image -> [b2]gpt-image-2
```

图片处理：

```text
上游 b64_json 或 URL
    -> BTF 解码/下载
    -> /opt/btf-device-gateway/data/generated-images/YYYYMMDD/
    -> generated_images 数据表
    -> public_url
    -> /device-images/YYYYMMDD/filename.png
```

图片目录不开放列表；`/device-images/...` 不再"知道路径即可访问"。每次生成的图片
链接都带一个绑定到该路径的 HMAC 签名和过期时间（`exp`/`sig` 查询参数），签名失配、
被篡改或过期一律返回 403，因此仅猜到完整路径无法读取图片。链接默认 24 小时有效
（`BTF_IMAGE_URL_TTL_SECONDS`，最小 60 秒），签名密钥由 `BTF_IMAGE_URL_SIGNING_KEY`
或数据目录内自动生成并持久化的密钥提供。

图片历史中曾经存在旧的 `[b]gpt-image-2` 失败记录；当前 New API 渠道、abilities 和 BTF 套餐已经统一到 `[b2]gpt-image-2`，旧模型已禁用。

## 8. 完整调用链路

### 8.1 设备聊天链路

```text
Android / 硬件
    |
    | Authorization: Bearer <device-key>
    v
BTF Gateway /v1/chat/completions
    |
    +-- 校验 Key
    +-- 查 device_id
    +-- 检查 status
    +-- 检查 expires_at
    +-- 检查套餐请求/Token/RPM/并发额度
    +-- 检查 model 是否在套餐中
    +-- GPT-5.5 时追加 web_search
    +-- 把 Authorization 替换为 NEW_API_TOKEN
    v
New API /v1/chat/completions
    |
    +-- 按 model / group 选择渠道
    +-- DeepSeek 或 GPT-5.5 上游
    v
BTF Gateway
    |
    +-- 返回 OpenAI 兼容响应
    +-- 记录 usage_events
    +-- 更新设备计数
    v
Android / 硬件
```

### 8.2 图片链路

```text
设备请求 btf-image
    -> BTF 套餐权限和图片额度检查
    -> 映射 [b2]gpt-image-2
    -> New API 图片渠道
    -> 上游返回 b64_json 或图片 URL
    -> BTF 下载/解码
    -> 本地落盘
    -> 记录 generated_images
    -> 返回公网图片 URL
```

### 8.3 登录和客户数据链路

```text
浏览器
    -> 现有 New API 登录页
    -> New API 签发 Access Token + Refresh Cookie
    -> BTF 对 dashboard API 的请求转发到 New API
    -> BTF 用 New API Session 获取 user.id / role
    -> 非管理员查询自动限制为 devices.user_id = 当前 user.id
```

客户不需要在设备管理页面再次输入设备 Token。设备 Key 只用于硬件调用 `/v1`、设备状态和公开续费查询。

### 8.4 续费链路

```text
客户打开 /device/pay
    -> 输入 MAC / 当前设备 Key
    -> GET /api/device/status
    -> 即使过期也能查看状态
    -> POST /api/device/orders
    -> 生成 pending 订单
    -> POST /api/device/orders/{id}/pay
    -> Mock / 微信 / 支付宝支付
    -> 支付回调
    -> 验证签名、金额、币种、订单号
    -> apply_payment_notification
    -> approve_order
    -> 从 max(old_expires_at, now) 延长
    -> 设置设备 active
    -> 设备恢复 /v1 调用
```

手工管理员确认也直接调用同一个 `approve_order` 逻辑。重复回调或重复确认不会重复延长设备有效期。

## 9. 关键 API 清单

### 9.1 公共和设备 API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | 服务健康检查（仅存活检查，无 I/O） |
| GET | `/ready` | 深度就绪检查（数据库 `SELECT 1` + 上游探测），失败返回 503 |
| GET | `/api/status` | New API 状态 |
| GET | `/api/device/plans` | 套餐列表 |
| GET | `/api/device/payment-providers` | 当前配置的支付提供商 |
| GET | `/api/device/status` | 设备状态，需要设备 Key |
| GET | `/device/pay` | 设备续费页面 |
| POST | `/api/device/orders` | 创建续费订单 |
| POST | `/api/device/orders/{id}/pay` | 发起支付 |
| POST | `/api/device/payments/wechat/notify` | 微信回调 |
| POST | `/api/device/payments/alipay/notify` | 支付宝回调 |
| POST | `/api/device/payments/mock/complete/{id}` | Mock 完成，仅测试环境 |
| GET/POST | `/v1/*` | OpenAI 兼容设备 API |
| GET | `/device-images/{path}` | 读取已落盘图片，需带有效签名的短期链接 |

### 9.2 管理和客户 Dashboard API

```text
GET    /api/device/
POST   /api/device/
PUT    /api/device/
POST   /api/device/batch
POST   /api/device/bind
POST   /api/device/manage
GET    /api/device/{id}
GET    /api/device/usage
GET    /api/device/images
GET    /api/device/orders
POST   /api/device/orders/approve
POST   /api/device/orders/{id}/close
GET    /api/device/users/usage
PUT    /api/device/users/{user_id}/limits
GET    /api/log/self
GET    /api/log/self/stat
GET    /api/data/self
GET    /api/data/flow/self
```

所有 Dashboard API 使用 New API 浏览器认证。管理员和客户权限在 BTF Gateway 再做一次校验。

## 10. 当前线上快照

以下是 2026-09-29 检查时的数据库快照，属于运行数据，不是固定配置：

| 项目 | 数量 |
|---|---:|
| BTF 用户 | 3 |
| BTF 设备 | 10 |
| 套餐 | 5 |
| 订单 | 2 |
| usage_events | 291 |
| generated_images | 41 |
| 未过期可调用设备 | 4 |
| 已过期但状态可查设备 | 6 |

设备归属：

| New API user ID | 当前用户名 | BTF 设备数 |
|---:|---|---:|
| 4 | `customer_alpha` | 4 |
| 5 | `customer_beta` | 3 |
| 6 | `customer_gamma` | 3 |

客户账号密码不写入本文档。管理员凭据文件在服务器：

```text
/opt/new-api/.admin-credentials
```

只在服务器上读取，不要把密码提交到 Git、聊天记录或交接文档。

New API 当前主要渠道：

| 渠道 | 模型 | 状态 |
|---|---|---|
| BTF DSAI Anthropic | `deepseek-v4.1-flash` | 启用 |
| BTF GPT Image | `[b2]gpt-image-2` | 启用 |
| BTF GPT 5.5联网 | `gpt-5.5` | 启用 |

## 11. 支付现状

支付代码和测试链路已经存在，但线上真实支付资料未配置。

当前表现：

```text
GET /api/device/payment-providers
data.items = []
```

这表示当前没有完整可用的 Mock、微信或支付宝支付配置，不表示订单系统不存在。

代码已经覆盖：

- 订单创建。
- 订单过期。
- 订单关闭。
- 支付意图创建。
- provider order id 保存。
- 金额和币种校验。
- 微信 API v3 签名和回调解密代码。
- 支付宝 RSA2 回调验签代码。
- Mock 支付签名。
- provider transaction id。
- 幂等键。
- 重复回调不重复续期。
- 手工确认订单。

真实支付上线还需要：

1. HTTPS 域名。
2. 微信商户号、应用 ID、商户证书、平台证书/API v3 Key。
3. 支付宝 App ID、应用私钥、支付宝公钥。
4. 公网可访问的回调地址。
5. 在 BTF `.env` 设置对应环境变量。
6. 重建并重启 BTF。
7. 用低金额订单做真实支付回调验收。

相关环境变量名称见 `docs/OPERATIONS.md`，不要把值写进代码或文档。

## 12. 测试和验证结果

### 12.1 自动化测试

当前全量测试（不要硬编码精确数量，以下为快照日期 2026-10-09 的结果）：

```text
224+ passed
```

覆盖：

- 设备创建、生命周期、批量创建。
- 用户绑定和解绑。
- 过期设备。
- Key 轮换和旧 Key 失效。
- 订单创建、关闭、过期、批准。
- Mock 支付签名、金额错误、重复回调。
- 微信/支付宝签名代码。
- 套餐模型权限。
- 请求、Token、RPM、并发和用户级限额。
- 客户用量接口。
- 图片保存和客户隔离。
- 路径穿越防护。
- CLI 输出脱敏（`api_key` 默认掩码）、模型来源派生和导入上限。

运行：

```bash
cd /opt/btf-device-gateway
docker run --rm \
  --network container:btf-device-gateway \
  -v /opt/btf-device-gateway:/app \
  -w /app \
  -e BTF_DATA_DIR=/app/data \
  btf-device-gateway-btf-device-gateway \
  sh -c 'python -m pip install --no-cache-dir pytest==8.3.4 && python -m pytest -q'
```

### 12.2 线上实测

已验证：

- BTF `/health` 返回 200。
- New API 容器 healthy。
- Dashboard 页面、设备页、支付页可访问。
- 管理员登录和 Refresh 成功。
- 登录后核心 Dashboard API 全部成功。
- 10 台设备逐台检查状态。
- 过期设备状态接口可用，`/v1` 返回 403。
- 未过期设备 `/v1/models` 可用。
- DeepSeek 请求成功。
- GPT-5.5 联网请求成功并返回来源。
- GPT-5.5 响应模型名为 `gpt-5.5`。
- DeepSeek 默认兼容别名为 `btf-chat`。
- 用量正确写入 `requested_model` 和 `upstream_model`。
- 图片实际成功生成、落盘并可通过公网 URL 查看。
- 错误设备 Key 返回 403。
- 当前测试窗口没有登录 500。

本次交接前再次做的只读检查（2026-09-29）：

```text
/health                         200
/device/pay                     200
/admin                          200
/dashboard/overview             200
/api/status                     200
/api/device/plans               200
/api/device/payment-providers   200, items=[]
```

本次在 Docker 测试环境重新运行（快照日期 2026-10-09，数量以测试套件为准）：

```text
224+ passed
```

宿主机 `/opt/btf-device-gateway` 的系统 Python 没有安装 pytest，因此测试使用
`btf-device-gateway-btf-device-gateway` 镜像执行；这不是项目测试失败。

测试注意事项：

- 上游图片服务忽略了测试请求中的 `n=0`，一次“只测路由”的请求实际生成了图片并消耗了 1 次图片额度。以后禁止用真实图片接口做无成本探测。
- 线上历史用量中存在旧 `[b]gpt-image-2` 的失败记录；当前配置已修正为 `[b2]gpt-image-2`。
- 当前 6 台设备过期是时间自然流逝和测试数据造成的预期状态，不是鉴权 bug。

## 13. Android 和语音部分的边界

当前 `/opt` 没有发现：

- Android Gradle 工程。
- `gradlew`。
- APK/AAB。
- Kotlin/Java Android 源码。

因此服务端已经准备了客户端需要的协议：

```text
GET  /v1/models
POST /v1/chat/completions
POST /v1/images/generations
GET  /api/device/status
```

Android 端接手要求：

1. 启动时用设备 Key 请求 `/v1/models`。
2. 过滤或隐藏 `btf-chat`，展示成 DeepSeek 兼容选项。
3. 将 `gpt-5.5`显示为“GPT-5.5（联网）”。
4. 仅当返回 `btf-image` 时展示图片生成能力。
5. 所有请求使用设备 Key，不使用 `NEW_API_TOKEN`。
6. 图片优先使用返回的公网 `url` 或 `btf_local_path`拼接的公网地址。
7. 处理 401/403/429/5xx，并区分过期、禁用、限额和上游失败。

语音识别、语音合成、`/mcu/stt`、`/mcu/tts`、`/mcu/voice_chat` 等此前讨论过的能力不在本仓库目录内。此前记录显示语音链路涉及腾讯云 ASR、Windows SAPI 等外部服务，但当前不能仅凭 BTF Gateway 仓库确认其现状。Claude 接手时应先定位 MCU/Android 仓库，再单独审计。

## 14. 已知风险和后续任务

### P0：真实支付上线前

- 配置 HTTPS 域名。
- 配置可信 Origin。
- 配置微信或支付宝生产资料。
- 验证支付回调签名、金额、币种、交易号和幂等。
- 禁止在生产开启 Mock 支付。

> 注：会话 Cookie 的安全属性由 New API 容器配置，本仓库没有
> `SESSION_COOKIE_SECURE` 环境变量，不要在本项目设置它。

### P1：Android 客户端

- 找到 Android 工程。
- 实现动态 `/v1/models`。
- 统一模型展示名称。
- 完成 DeepSeek、GPT-5.5、图片生成、过期/禁用/限额错误处理。
- 真机测试设备 Key、图片 URL、长耗时联网和生图。

### P1：数据和初始化

- `init_db()`对已存在的自定义套餐不会无条件覆盖 `chat_models`，这是保护管理员自定义配置的设计。
- 如果要迁移旧套餐，应只对空值或严格等于旧默认值的记录追加 GPT-5.5，不能覆盖客户定制套餐。
- 当前线上 SQLite 是权威数据，操作前必须备份。

### P1：测试数据清理

- 当前客户 A 有 4 台设备，原因是额外的 `Workflow Test`设备。
- 如果要正式演示“三个客户各三台”，应先确认是否删除或保留该额外测试设备。
- 不要直接删除设备记录，应通过管理接口或备份后数据库操作，并确认订单、用量和图片关联。

### P2：可观测性

- 为 BTF 增加结构化请求 ID和更明确的 5xx 日志。
- 增加上游渠道健康检查和模型可用性告警。
- 将设备限额、用户限额、上游错误、图片耗时分别统计。
- 增加异步生图或任务状态接口，避免客户端长时间同步等待。

### P2：安全

- 生产环境使用 HTTPS。
- 轮换曾经在聊天或命令输出中暴露过的上游 Key。
- 确认 `.env` 和 `.admin-credentials` 权限为 0600。
- 不要把真实 Key、密码、Cookie、数据库连接串写入交接文档或 Git。
- 确认公网不暴露 SQLite、`.env`、源码和备份目录。

## 15. 接手操作规则

Claude 或下一位开发者接手时必须遵守：

1. 先读取本文件和 `docs/ARCHITECTURE.md`、`docs/OPERATIONS.md`。
2. 先检查 `git status`，不要清理或回滚已有未提交修改。
3. 先读取线上配置和数据库结构，不直接猜账号、套餐或设备归属。
4. 修改前先说明影响范围。
5. 修改后运行全量 `pytest`。
6. 需要部署时执行 build、up、health、核心 API 验证。
7. 真实生图和真实支付必须先说明会消耗额度或产生资金动作。
8. 不打印或复制任何真实密钥、密码、内部 Token。
9. 如果要改 New API PostgreSQL，先确认 SQL、备份和回滚方式。
10. 不要另建一套独立 UI；设备功能应继续嵌入现有 New API UI。

## 16. 给 Claude 的接手提示词

可以把下面内容连同本文档一起交给 Claude：

```text
你正在接手 /opt/btf-device-gateway 和 /opt/new-api 上运行的 BTF API 项目。

请先完整读取：
- /opt/btf-device-gateway/docs/CLAUDE_HANDOFF.md
- /opt/btf-device-gateway/docs/ARCHITECTURE.md
- /opt/btf-device-gateway/docs/OPERATIONS.md
- /opt/btf-device-gateway/docs/CUSTOMER_GUIDE.md
- /opt/btf-device-gateway/README.md

当前架构：
- New API 负责浏览器用户、登录、角色、Session、上游渠道和模型路由。
- BTF Gateway 负责设备 MAC、设备 Key、用户绑定、套餐、限额、订单、支付回调、设备用量和图片落盘。
- 客户和管理员使用现有 New API Dashboard，不要新建独立风格的后台。
- 设备通过 Authorization: Bearer <device-key> 调用 /v1。
- 初始设备 Key 等于 MAC；换 Key 后 Key 变为随机值且不再等于 MAC，旧 Key 必须失效。
- DeepSeek 默认；gpt-5.5 可选并自动追加 web_search；月卡可使用 btf-image。

开始任何修改前：
1. 执行 git status，保留所有已有工作区修改。
2. 检查 Docker、SQLite、PostgreSQL、Redis 状态。
3. 不输出任何真实密码、Key、Token、私钥或数据库连接串。
4. 不执行 git reset --hard、git checkout -- 或删除生产数据库。
5. 先说明要改哪些文件和为什么。

当前优先级：
1. 不要重复实现已经完成的设备、套餐、用量和模型逻辑。
2. 先完成 HTTPS、真实支付资料接入和支付回调验收。
3. 找到 Android/MCU 工程后，完成动态模型选择、图片和错误状态。
4. 继续保持现有 New API UI 和路由，不创建孤立的 BTF 页面。
5. 每次修改后运行全量 pytest，并做线上健康、登录、设备、模型和权限验收。

危险操作：
- 真实图片请求会消耗图片额度。
- 真实支付请求可能产生资金动作。
- 修改 New API abilities/channels 可能改变生产模型路由。
- 修改 BTF SQLite 可能影响客户设备、订单和用量。
```
