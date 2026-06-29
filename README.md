# Django CRM Sales Management

这是一个脱敏后的 Django CRM 示例项目，用于客户资料、跟进记录、报价、合同收款、公海/回收站和经营看板管理。

公开仓库不包含生产数据库、导入文件、媒体附件、日志、真实账号、真实客户资料或生产环境变量。

## 本地运行

1. 复制环境变量模板：

   ```bash
   cp env.example .env
   ```

2. 修改 `.env` 中的密钥、数据库密码和域名。

3. 使用 Docker 启动：

   ```bash
   docker compose up -d --build
   ```

4. 进入应用容器执行数据库迁移：

   ```bash
   python manage.py migrate
   python manage.py createsuperuser
   ```

## 目录说明

- `app/`：Django 项目代码。
- `docs/`：业务规则和迁移说明。
- `env.example`：环境变量示例，不包含生产密钥。
- `docker-compose.yml`：本地或服务器部署示例。

## 注意

飞书同步、客户导入和外部系统对接需要在部署环境中单独配置真实凭证。不要把 `.env`、数据库、导入文件、附件目录提交到仓库。
