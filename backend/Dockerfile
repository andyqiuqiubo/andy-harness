# 后端 Dockerfile
FROM python:3.12-slim

WORKDIR /app

# 安装 uv
RUN pip install uv

# 复制项目文件
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev

# 复制源码
COPY harness/ ./harness/
COPY plugins/ ./plugins/
COPY requirements-sandbox.txt ./

# 创建数据目录
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 8000

# 启动命令
CMD ["uv", "run", "uvicorn", "harness.main:app", "--host", "0.0.0.0", "--port", "8000"]
