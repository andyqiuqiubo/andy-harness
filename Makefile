.PHONY: install backend frontend dev test lint format clean

# 一键安装前后端依赖
install:
	cd backend && uv sync --extra dev
	cd frontend && pnpm install

# 启动后端开发服务器（端口 8000）
backend:
	cd backend && uv run uvicorn harness.main:app --reload --host 0.0.0.0 --port 8000

# 启动前端开发服务器（端口 5173）
frontend:
	cd frontend && pnpm dev

# 同时启动前后端
dev:
	@echo "请分别在两个终端执行: make backend 和 make frontend"

# 运行后端测试
test:
	cd backend && uv run pytest -v

# 代码检查
lint:
	cd backend && uv run ruff check . && uv run mypy harness
	cd frontend && pnpm lint

# 代码格式化
format:
	cd backend && uv run ruff format . && uv run ruff check --fix .
	cd frontend && pnpm format

# 清理
clean:
	rm -rf backend/.venv backend/.ruff_cache backend/.mypy_cache backend/.pytest_cache
	rm -rf frontend/node_modules frontend/dist
	rm -rf .uv-cache .pnpm-store
