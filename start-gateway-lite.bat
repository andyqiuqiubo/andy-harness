@echo off
setlocal
cd /d "%~dp0"
if not exist "backend\.venv\Scripts\python.exe" (
  echo [andy-gateway-lite] backend\.venv 不存在，请先安装后端依赖。
  exit /b 1
)
echo [andy-gateway-lite] 启动中... 默认 http://127.0.0.1:8010/v1
echo [andy-gateway-lite] 如需鉴权：set GATEWAY_LITE_KEY=agw-local-dev
backend\.venv\Scripts\python.exe tools\andy_gateway_lite.py --port 8010
