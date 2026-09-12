# PromptMaster 服务镜像（FastAPI 控制台 + 优化闭环）
#
# 设计要点：
# - 只装**运行时**依赖：用 `pip install .`（依赖声明在 pyproject.toml）而不是
#   `pip install -r requirements.txt` —— 后者会把 pytest/ruff/mypy 一起搬进镜像。
# - 记录与产物都在 /data 下（PM_TASK_DB 指向 SQLite、PM_LOG_DIR 指向报告目录），
#   挂一个卷就能持久化，也天然允许 `--workers > 1`（各 worker 共享同一份记录）。
# - 以非 root 用户运行；默认只跑 1 个 worker，多 worker 见下方注释。
#
# 构建与运行：
#   docker build -t prompt-master .
#   docker run --rm -p 8080:8080 -v "$PWD/data:/data" \
#     -e PM_API_KEY=sk-xxx -e PM_TARGET_MODEL=deepseek-v3 prompt-master
#
# 多 worker（记录走 SQLite，不要用默认的内存记录）：
#   docker run ... prompt-master python run_server.py --host 0.0.0.0 --workers 4
#
# 注意：映射到宿主机时务必设 PM_API_TOKEN，否则等于把"能烧 API 余额"的入口公开出去。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PM_LOG_DIR=/data/logs \
    PM_TASK_DB=/data/tasks.db

WORKDIR /app

# 先装依赖再拷代码：改代码不会让依赖层失效
COPY pyproject.toml ./
COPY pm/ ./pm/
RUN pip install . \
    && useradd --create-home --uid 10001 pmuser \
    && mkdir -p /data/logs \
    && chown -R pmuser /data

# 入口脚本与用例模板（CLI 与 API 都用得上）
COPY run.py run_server.py ./
COPY case_templates/ ./case_templates/

USER pmuser
EXPOSE 8080

# 健康检查走 /api/health（不消耗任何模型调用）
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2).status == 200 else 1)"

CMD ["python", "run_server.py", "--host", "0.0.0.0", "--port", "8080"]
