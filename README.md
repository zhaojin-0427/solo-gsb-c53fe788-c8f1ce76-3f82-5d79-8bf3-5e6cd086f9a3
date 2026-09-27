# 🎲 多人公平抽签室（Fair Draw Room）

本地运行的多人公平抽签 Web 应用。主持人创建**不可变轮次**（固定成员顺序、奖项、承诺/揭示两个截止时间），成员在各自浏览器本地生成秘密、提交 SHA-256 承诺，全员承诺到齐后揭示；服务端用“配置 + 全员秘密”派生种子，以**拒绝采样的 Fisher-Yates 洗牌**得出唯一结果。任何阶段超时或承诺不符都会使**整轮作废**。全程可导出审计 JSON 并在浏览器中**离线独立复算**。

## 目录

- [快速开始（Docker Compose）](#快速开始docker-compose)
- [本地直接运行](#本地直接运行)
- [使用流程](#使用流程)
- [公平性与协议设计](#公平性与协议设计)
- [审计与独立复算](#审计与独立复算)
- [API 参考](#api-参考)
- [项目结构](#项目结构)
- [测试](#测试)

## 快速开始（Docker Compose）

需要 Docker 20.10+ 与 Docker Compose v2。

```bash
docker compose up --build
```

启动后浏览器访问：

| 页面 | 地址 |
| --- | --- |
| 首页（创建 / 加入 / 复算入口） | <http://localhost:8000/> |
| 独立审计复算页（纯前端，离线可用） | <http://localhost:8000/verify.html> |

数据持久化在 Docker volume `draw-data`（容器内 `/data/draw.db`，SQLite WAL 模式）。
停止：`docker compose down`；连同数据一起清除：`docker compose down -v`。

> 🔼 **旧版本数据自动迁移**：早期版本的 `events.seq` 使用全局 `AUTOINCREMENT`，
> 多轮次共用计数器，第二轮审计的序号不从 1 开始，会被复算页判为断号。升级后应用
> **首次打开既有数据库时自动迁移**：按原插入先后把每个轮次的事件重排为轮次内
> 1..N，保留全部事件的时间（`at`）与顺序，迁移在单个事务内完成且幂等；之后新建
> 轮次一律按轮独立编号。无需手工操作，旧数据可继续使用。

端口可在 `docker-compose.yml` 的 `ports` 中修改（如 `"8080:8000"`）。

## 本地直接运行

需要 Python 3.11+（标准库之外仅需 FastAPI / Uvicorn / Pydantic）：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库默认放在 `./data/draw.db`，可用环境变量覆盖：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DRAW_DB_PATH` | `./data/draw.db` | SQLite 文件路径 |
| `DRAW_SWEEP_INTERVAL` | `1.0` | 后台超时扫描间隔（秒） |

## 使用流程

### 主持人

1. 打开首页 → “主持人 · 创建轮次”，填写标题、**成员名单（每行一个，顺序固定）**、奖项（每行一个，按颁发顺序）、承诺/揭示截止时间（表单使用浏览器本地时区，提交时转为 UTC）。
2. 创建后跳转到主持台，页面 URL 中持有**主持人令牌**（同时保存在该浏览器 localStorage），它是管理与导出审计的唯一凭证，请妥善保存。
3. 主持台为每位成员显示**一次性邀请码/链接**，分别私发给对应成员。
4. 实时查看承诺/揭示进度、倒计时、事件序列；全员揭示后自动结算并展示奖项归属。
5. 随时可“下载审计 JSON”。

### 成员

1. 在任意设备打开邀请链接（或在首页粘贴邀请码）。邀请码**只能使用一次**：首次加入换发会话令牌并保存在该浏览器中，之后请始终在同一设备/浏览器操作。
2. 浏览器用 `crypto.getRandomValues` 在本地生成 128 位随机秘密（仅存本机 localStorage，可自行复制备份），并显示由公开编码计算出的 SHA-256 承诺，一键提交。
3. 全员承诺到齐后，页面自动进入揭示阶段；成员确认秘密后提交揭示。提交前浏览器会自检秘密与已提交承诺是否一致。
4. 全员有效揭示后页面自动展示结果。若有人超时、揭示了错误/冲突秘密，整轮作废并显示原因。

> ⚠️ 承诺/揭示都与成员的**固定位次**绑定，秘密丢失或更换浏览器/设备将导致该轮无法完成（超时作废）。这是承诺-揭示协议的固有安全属性。

## 公平性与协议设计

### 1. 统一的长度前缀编码（无分隔符歧义）

所有哈希输入都使用同一套嵌套长度前缀编码，浏览器与服务端逐字节一致：

```
frame(x) = 0x55('U') || uint32_be(len(x)) || x
nested([a, b, …]) = frame(frame(a) || frame(b) || …)
```

文本一律先 UTF-8 编码。承诺与种子使用不同的域标签（domain separation），互不混淆。

### 2. 承诺（commitment）

成员在浏览器计算：

```
commitment_i = SHA256(nested([
  "fair-draw-commitment-v1",
  nested([round_id, str(member_index), member_label, secret])
]))
```

- 绑定**轮次 ID 与固定位次/姓名**：秘密不能在轮次间或成员间重放；
- 承诺阶段服务端只收到 64 位十六进制哈希，得不到秘密；
- 承诺一旦提交不可更改；重复提交同一承诺是幂等的。

### 3. 阶段与作废规则

状态机：`committing → revealing → settled`，任何异常迁移到终态 `voided`。

- **全员承诺落库前**不接受任何揭示（HTTP 409）；
- 最后一个承诺与“进入揭示阶段”在**同一个数据库事务**中完成；
- 承诺截止时间到时承诺未齐 → 作废（`commit_timeout`）；
- 揭示截止时间到时揭示未齐 → 作废（`reveal_timeout`）；
- 揭示的秘密重算承诺与库中不符 → 作废（`commitment_mismatch`）；
- 同一成员先后揭示两个不同秘密 → 作废（`conflicting_reveal`）。

超时由 1 秒间隔的后台扫描器与每次请求/读快照时的惰性检查共同保证（SQLite 中规范化 UTC ISO 字符串可直接按字典序比较）。作废本身是一次合法的状态迁移，与随后的 4xx 错误在同一事务中提交，不会被回滚。

### 4. 种子（绑定配置与全员秘密）

全员有效揭示后，服务端在结算事务中计算：

```
seed = SHA256(nested([
  "fair-draw-seed-v1",
  nested([round_id, title, [members…], [prizes…],
          commit_deadline, reveal_deadline, [secret_0, secret_1, …]])
]))
```

秘密严格按**固定成员顺序**排列。轮次配置创建后不可变，因此：

- 主持人无法通过调成员/奖项/时间来偏向结果；
- 任何一名成员改变秘密都会改变种子与全部名次；
- 缺任一秘密都无法重建种子，“提前出结果”不可能发生。

### 5. 无模偏差的拒绝采样 Fisher-Yates

确定性随机源为 `HMAC-SHA256(key=seed, msg=uint64_be(counter))`，计数器从 0 开始、在所有步骤与拒绝之间单调递增：

1. 第 `i` 步（`i` 从 `n−1` 递减到 `1`）需要 `[0, i]` 上的均匀整数，令 `upper = i+1`；
2. 取 HMAC 摘要解释为 256 位整数 `c`；
3. 若 `c ≥ 2^256 − (2^256 mod upper)` 则拒绝，`counter += 1` 重试，消除取模偏差；
4. 否则 `j = c mod upper`，交换排列中 `i` 与 `j` 的位置。

最终排列前 `|P|` 位依次获得奖项（保证一人最多中一个奖）。每一步的 `counter / upper / candidate / accepted / value` 都作为 `trace` 写入结果与审计文件，可逐步复现。

### 6. 事务、版本与幂等

- 单 SQLite 连接 + 进程内互斥锁，所有迁移使用 `BEGIN IMMEDIATE`；
- 轮次带单调递增 `version`，每次状态迁移 +1，事件表（append-only）记录迁移时的版本；
- 事件序号 `seq` **按轮次独立分配**：每个轮次都从 1 连续递增，不同轮次互不影响。
  `events` 以复合主键 `(round_id, seq)`（WITHOUT ROWID）保证每轮内序号唯一；
  新序号在写事务内以 `COALESCE(MAX(seq),0)+1` 原子读出并插入，配合事务串行化，
  并发写入下既不会重复也不会跳号（多轮次并发补齐承诺有回归测试覆盖）；
- 重复提交相同承诺/揭示 → 幂等成功、不写重复事件；不同内容 → 409（揭示场景作废）；
- 并发补齐最后几个承诺时，阶段切换与事件插入在同一事务内，测试验证了 10 线程并发下阶段事件恰好一条；
- WebSocket 连接即推送当前快照（**重连恢复**），之后每次迁移向房间内所有连接者广播最新快照；客户端以快照整体重绘，天然处理断线期间的多次跳变。

### 7. 凭证模型

- **主持人令牌**：创建轮次时返回（`host_token`），以 SHA-256 哈希存库；用于主持台、REST 头 `X-Host-Token`、WS query 参数与审计导出。
- **一次性邀请码**：12 位（约 71 bit 熵），创建时明文存库（本地应用，主持人需要恢复邀请链接）；首次使用即标记 `used` 并换发**会话令牌**。
- **会话令牌**：加入时返回，SHA-256 存库，绑定轮次与成员位次，保存在成员浏览器 localStorage。

## 审计与独立复算

主持台可导出 `audit-<round_id>.json`：

```jsonc
{
  "format": "fair-draw-room/audit/v1",
  "config": {            // 参与哈希的全部不可变字段
    "round_id", "title", "members", "prizes",
    "commit_deadline", "reveal_deadline", "created_at"
  },
  "commitments": [ … ],  // 每位成员的 SHA-256 承诺
  "secrets": [ … ],      // 揭示的秘密（按位次）
  "events": [ … ],      // append-only 事件序列（每轮内 seq 从 1 连续递增；type/version/at/payload）
  "status", "void_reason", "version",
  "result": { "seed", "permutation", "winners", "trace", "algorithm" }
}
```

打开 `/verify.html`（或直接双击保存的页面），粘贴/选择审计 JSON，浏览器会**完全离线**执行：

1. 校验结构与字段；
2. 对每条秘密按公开编码重算承诺并比对；
3. 用配置 + 秘密重算种子；
4. 用 HMAC + 拒绝采样重放 Fisher-Yates，比对排列、奖项与逐步拒绝轨迹；
5. 核对事件序列（committed/revealed/settled 数量、`seq` 在该轮内从 1 连续递增且无重复、首条为 created）。

也可以点击“载入内置示例”查看一份合成但合法的审计文件的完整复算过程。

## API 参考

REST 基址 `/api`；成员接口需头 `X-Session-Token`，主持人接口需 `X-Host-Token`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rounds` | 创建轮次（主持人），返回 `round_id / host_token / invites[]` |
| POST | `/rounds/join` | `{code}` 一次性邀请码换会话令牌 |
| POST | `/rounds/{id}/commitments` | `{commitment}` 成员提交承诺（幂等） |
| POST | `/rounds/{id}/reveals` | `{secret}` 成员揭示（幂等；不符/冲突作废整轮） |
| GET | `/rounds/{id}/snapshot` | 当前完整快照（重连恢复） |
| GET | `/rounds/{id}/audit` | 导出审计 JSON（仅主持人） |
| WS | `/api/ws/{id}?host_token=…` 或 `?session_token=…` | 连接即收快照，迁移时广播；支持 `{"type":"snapshot"}`、`{"type":"ping"}` |
| GET | `/api/health` | 健康检查 |

错误统一为 `{"error": {"code", "message"}}`，HTTP 状态码 400/401/403/404/409/410/422。

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI：REST、WebSocket、静态页、后台扫描器
│   ├── service.py         # 轮次状态机（事务/版本/幂等/结算/审计/快照）
│   ├── crypto.py          # 编码、承诺、种子、拒绝采样 Fisher-Yates（纯标准库）
│   ├── db.py              # SQLite 连接/模式/事务（WAL + 互斥锁）
│   ├── ws.py              # WebSocket 连接管理与快照广播
│   ├── timeutil.py        # UTC ISO（秒精度、Z 结尾）
│   └── static/
│       ├── index.html  host.html  member.html  verify.html
│       ├── css/style.css
│       └── js/
│           ├── crypto.js  # crypto.py 的浏览器实现（Web Crypto）
│           ├── common.js  # API/WS 重连/倒计时/存储
│           ├── index.js  host.js  member.js  verify.js
├── tests/
│   ├── gen_vectors.py     # 生成 Python 协议测试向量
│   ├── crosscheck.js      # Node 端逐字节交叉验证编码/承诺/种子/洗牌/偏差
│   ├── service_test.py    # 服务层端到端（幂等/并发/超时/作废/认证）
│   ├── multiround_seq_test.py  # 多轮次事件序号回归（跨轮 1..N/并发不重号/作废轮）
│   ├── migration_test.py  # 旧库 AUTOINCREMENT → 每轮独立编号的兼容迁移
│   └── e2e_recompute.py   # Python 真实两轮 → 审计 JSON → JS 独立复算
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 测试

协议层交叉验证（Python ↔ JavaScript 逐字节比对编码、承诺、种子、HMAC 抽样点、
洗牌排列、拒绝轨迹，并对随机源做 30 万样本均匀性检查）：

```bash
python3 tests/gen_vectors.py > /tmp/vectors.json
node tests/crosscheck.js
```

服务层端到端测试（仅依赖 Python 标准库）：

```bash
DRAW_DB_PATH=/tmp/test.db python3 tests/service_test.py
```

多轮次事件序号回归（同一持久化库先后推进多个轮次、跨轮并发写入、作废轮）：

```bash
python3 tests/multiround_seq_test.py
```

旧数据库兼容迁移测试（构造旧版全局 AUTOINCREMENT 库，验证重编号/时间顺序保留/迁移后继续推进）：

```bash
python3 tests/migration_test.py
```

完整链路（Python 连续跑两轮真实抽签导出审计，再用浏览器同款 JS 独立复算并校验每轮 seq）：

```bash
python3 tests/e2e_recompute.py
```

## 安全边界说明

本应用面向**可信局域网 / 本机**场景：主持人令牌与邀请码是不记名能力凭证（存于 URL / localStorage），传输默认明文 HTTP；如需跨网络使用，请放在 HTTPS 反向代理之后。服务端不会也无法看到成员承诺阶段的秘密——成员自身仍需自行备份秘密，这是保证“无人能操纵结果”的另一面。
