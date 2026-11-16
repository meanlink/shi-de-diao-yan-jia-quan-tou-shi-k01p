# 实地调研加权透视

加权透视汇总的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 逐点观察台账（别名归并、冲突裁定、版本化日表批次）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
四个贯穿全栈的实体：Observation 逐点观察、LedgerBatch 日表批次、Dataset 数据批次、Job 分析任务、Report 分发计划；辅助 PointMerge、Adjudication、IdentityCorrection、User、Audit、Delivery。

### 逐点观察台账
- **逐点观察**：每条观察有稳定编号（换名不改编号），记录日期、队伍、区域、结论与台账版本。同点同日同队只计一次；重复提交幂等，不重复计样。
- **归并别名**：服务点换名时把别名并入同一服务点。归并是追加事件，按台账版本回放；撤销某次归并只回退该条，之后独立建立的归并继续生效。提交前可预览会合并多少观察、是否会产生结论或区域冲突。
- **冲突裁定**：同一服务点的结论或区域不一致时进入“待裁定”，冲突未清不发布日表批次。裁定按证据固定；之后到达的矛盾证据会把裁定标为“新证据需重裁”，但历史版本仍按当时的裁定回放。
- **日表批次**：从“待核对”生成并固定一个完整台账版本；发布后作为不可变 Dataset 供原加权透视复用。发布幂等且可续作：中断后用同一幂等键重试会恢复原次处理，不会重复建批。
- **并发与晚回**：新观察与归并提升台账版本；旧版本的计算结果晚回时被拒绝，不能覆盖新结论。批次固定在生成时的版本，台账前进后需重算才能发布。
- **身份更正**：更正观察编号会使其关联的未批准分析失效，拒绝批准、新建分发与再计算；已批准资料保留原值。
- **兼容**：原有日表导入与分析流程保持不变；台账发布的批次可直接用原加权透视计算。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18218；后端文档 http://localhost:19218/docs；数据库宿主端口 17718。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、读取与下载；不能批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；PostgreSQL 16；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed/invalid。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。
台账实现集中在 backend/src/{models,services,routes}/ledger.py：服务层为唯一校验与回放入口，路由只做权限与序列化。归并、撤销、裁定、发布仅 admin；记录观察与建批为 admin/analyst；读取对三者开放。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=survey-pivot；FRONTEND_PORT=18218；BACKEND_PORT=19218；DB_PORT=17718；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 测试
台账契约测试位于 backend/tests/（`python -m pytest tests -q`，使用临时 SQLite，不需要 Redis/Postgres）：覆盖重复计样、别名归并、撤销单次归并保留后续归并、冲突裁定与发布门禁、版本固定与晚回拒绝、发布中断续作、身份更正失效范围、原日表导入兼容，以及 HTTP 权限与主流程。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。验收截图与容器记录按项目约定另存，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
