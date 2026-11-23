# 微页典藏·馆藏征集号整理

按内存预算生成磁盘 runs，多轮有界归并并记录 I/O 与稳定顺序。面向演示数据和本地复现，场景、操作剧本和业务边界见 [BUSINESS.md](BUSINESS.md)。

## 业务模块

- 登录批次
- 内存预算
- 有界归并
- 上架顺序单
- 备份恢复演练

## 本地运行

```sh
cp -n .env.example .env
docker compose up -d --build --wait
```

- 工作台 `http://127.0.0.1:18296`
- FastAPI 文档 `http://127.0.0.1:19296/docs`
- PostgreSQL 16 端口 `127.0.0.1:17796`，账号 `app`，演示库 `workflow`
- `docker compose exec -T backend python -m scripts.seed_demo` 手动重跑演示数据
- `docker compose logs -f backend worker` 查看运行情况
- `docker compose down` 停容器并保留演示卷

### 演示账号

统一密码 `Demo-Walkthrough-2026`。

| 账号 | 角色 | 能执行的操作 |
|---|---|---|
| `operator` | 经办员 | 读取档案、创建实验输入、提交执行、导出结果、建立备份、发起恢复演练与确认重新入队 |
| `reviewer` | 复核员 | 读取、导出和签署结果，查看备份与演练记录 |
| `viewer` | 观察员 | 读取、导出已有结果与备份，查验演练目标库 |

`SEED_DEMO=true` 启动时生成账号与一个业务样例。工作台新建档案会预填样例。可编辑表单中的数值、文本和结构化 JSON 后提交。业务说明中的原始文件是上游示例，演示数据通过表单录入。角色枚举位于后端 `src/constants/workflow.py` 与前端 `src/constants/workflow.js`。执行状态枚举也位于这两个文件，使用于队列、列表和执行详情。证据校验码是报告 JSON 按 UTF-8、键排序且省略多余空格序列化后的 SHA-256。

## 工程结构

| 服务 | 实现 |
|---|---|
| frontend | Nginx 与原生 HTML、CSS、JavaScript |
| backend | Python 3.11、FastAPI、SQLAlchemy |
| worker | Python 持久队列执行与 Redis 唤醒信号 |
| db | PostgreSQL 16，持久卷保存档案、执行、证据及审计 |
| redis | Redis 7，追加日志卷保存队列唤醒信号 |

编辑 `backend/src/services/domain.py` 可修改试算方法与业务表单说明。API 路由、数据定义和页面位于 `backend/src/routes/`、`backend/src/models/` 与 `frontend/src/pages/`。备份与恢复演练实现在 `backend/src/services/backup.py`，演练页在 `frontend/src/pages/drill.js`。开发时更新源文件后重新构建对应服务。

### 备份与恢复演练

工作台「备份恢复演练」页或 API 可建立一致性备份并恢复到独立空库。

- `POST /backups` 在只读快照事务中同一时点读取登录批次、执行、上架顺序单和操作记录，写下带校验码的备份文件；`GET /backups/{id}/download` 导出该文件。
- `POST /backups/{id}/restores`（或 `POST /backups/snapshot/{snapshot_id}/restores`）整份校验后写入目标空库，可传 `{"target": "workflow_drill_x"}` 指定库名，留空自动分配。源库与已有内容的目标库会被拒绝。
- `GET /restores/{id}` 回读目标库中的上架结果与签署；`POST /restores/{id}/requeue` 确认备份时排队或执行中的作业重新入队，可传 `{"runs": [id, ...]}` 只确认部分作业，重复确认不会重复入队。

备份文件保存在 `artifacts` 卷的 `backups/` 下。恢复需要数据库账号具备建库权限；目标库不会自动清理，供演练后继续查验。

`.env.example` 提供 `FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_PASSWORD`、`AUTH_KEY` 、`SEED_DEMO` 与 `NETWORK_SUBNET`。修改 `NETWORK_SUBNET` 可避开本机现有容器网段。所有发布的端口绑定本机回环地址。接入独立部署前需配置自己的凭据并按业务范围做独立验收。

[MIT License](LICENSE)
