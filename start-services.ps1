# andy-harness 一键启动（浏览器模式：后端 8000 + 前端 5173）
# 双击本文件，或执行：powershell -ExecutionPolicy Bypass -File H:\jsd-andyharness\src\start-services.ps1
# 停止：关闭弹出的两个黑色命令行窗口即可（或执行 stop-services.ps1）
# 说明：模式与仓库自带 start-all.bat 一致（两个独立窗口分别跑后端/前端），
#       不重定向输出、不改动环境变量，避免 PowerShell 5.1 的 Start-Process 兼容坑。
$root = 'H:\jsd-andyharness\src'

# 后端：FastAPI / uvicorn，端口 8000（窗口标题 andy-harness-backend）
Start-Process -FilePath 'cmd.exe' `
  -ArgumentList '/k','title andy-harness-backend && cd /d',$root + '\backend','&& .venv\Scripts\python.exe -m uvicorn harness.main:app --reload --host 127.0.0.1 --port 8000'

# 前端：Vite dev server，端口 5173（窗口标题 andy-harness-frontend）
Start-Process -FilePath 'cmd.exe' `
  -ArgumentList '/k','title andy-harness-frontend && cd /d',$root + '\frontend','&& pnpm dev'

# 打开浏览器（注意：必须用 localhost，Vite 绑定的是 IPv6 [::1]，127.0.0.1 连不上）
Start-Process 'http://localhost:5173'
Write-Output 'backend -> http://127.0.0.1:8000   frontend -> http://localhost:5173'
