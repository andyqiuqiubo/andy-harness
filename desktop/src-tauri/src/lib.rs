//! Tauri 桌面壳主逻辑（E11）。
//!
//! 职责很薄：
//! 1. 选一个可用端口（默认 127.0.0.1 回环，桌面场景不暴露到局域网）；
//! 2. 通过 `desktop/launcher/launch-backend.py` 启动后端；
//! 3. 暴露 `get_backend_url` 给前端，前端据此直连后端；
//! 4. 应用退出时终止整个后端进程树，不残留 uvicorn。

use std::net::TcpListener;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use tauri::Manager;

/// 后端子进程（启动器）。
///
/// 用 `Mutex` 包装：`tauri::State` 只提供对状态的**共享**引用（`Deref`，
/// 没有 `DerefMut`），而退出回调里需要把子进程句柄取出来（`Option::take`
/// 需要 `&mut`），直接写 `state.0.take()` 会触发 `error[E0596]`。
/// `Mutex<Option<Child>>` 既满足 `manage` 要求的 `Send + Sync`，
/// 又能在锁内完成可变访问。
struct BackendProcess(Mutex<Option<Child>>);

/// 后端监听端口。
struct BackendPort(u16);

/// 选一个空闲端口：先由系统绑定到 0 拿到端口，随即释放。
fn find_free_port() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").expect("无法绑定回环端口");
    let port = listener.local_addr().expect("无法读取本地端口").port();
    drop(listener);
    port
}

/// 解析后端 Python 解释器：
/// - dev：仓库 backend/.venv 里的解释器；
/// - release：优先环境变量 HARNESS_PYTHON，否则用 PATH 中的 python
///   （打包时改为随包 sidecar，见 desktop/README.md）。
fn resolve_python() -> PathBuf {
    if let Ok(explicit) = std::env::var("HARNESS_PYTHON") {
        return PathBuf::from(explicit);
    }
    if cfg!(debug_assertions) {
        let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let venv = if cfg!(target_os = "windows") {
            manifest_dir.join("../../backend/.venv/Scripts/python.exe")
        } else {
            manifest_dir.join("../../backend/.venv/bin/python")
        };
        if venv.exists() {
            return venv;
        }
    }
    PathBuf::from("python")
}

/// 启动后端启动器。
fn spawn_backend(port: u16) -> Result<Child, String> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let launcher = manifest_dir.join("../launcher/launch-backend.py");
    if !launcher.exists() {
        return Err(format!("找不到后端启动器: {}", launcher.display()));
    }
    Command::new(resolve_python())
        .arg(launcher)
        .arg("--port")
        .arg(port.to_string())
        .spawn()
        .map_err(|e| format!("启动后端失败: {e}"))
}

/// 停止后端：Windows 连子孙进程一起杀（启动器下还挂着 uvicorn），
/// 其它平台直接 kill 启动器进程。
fn stop_backend(child: &mut Child) {
    if cfg!(target_os = "windows") {
        let _ = Command::new("taskkill")
            .args(["/F", "/T", "/PID", &child.id().to_string()])
            .status();
    } else {
        let _ = child.kill();
    }
    let _ = child.wait();
}

/// 前端调用：拿到后端基地址。
#[tauri::command]
fn get_backend_url(state: tauri::State<'_, BackendPort>) -> String {
    format!("http://127.0.0.1:{}", state.0)
}

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            // 允许用固定端口，便于调试；否则挑空闲端口。
            let port = std::env::var("HARNESS_PORT")
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or_else(find_free_port);
            let child = spawn_backend(port)?;
            app.manage(BackendProcess(Mutex::new(Some(child))));
            app.manage(BackendPort(port));
            // 这两行会出现在 "andy-harness-desktop" 窗口（tauri dev 的 stdout）里：
            // 方便确认当前窗口对应的是哪个后端实例、壳是否刚刚被重新编译。
            println!(
                "[andy-harness] 壳已启动；后端基地址 http://127.0.0.1:{port}（回环、动态端口）"
            );
            println!(
                "[andy-harness] 前端来自 Vite dev server http://localhost:5173（HMR，改前端即时生效）"
            );
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_backend_url])
        .build(tauri::generate_context!())
        .expect("构建 Tauri 应用失败")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<BackendProcess>() {
                    // State 只能共享借用，改经 Mutex 取出句柄后再终止进程树。
                    if let Some(mut child) = state.0.lock().ok().and_then(|mut g| g.take()) {
                        println!("[andy-harness] 正在退出，终止后端进程树（taskkill /F /T）...");
                        stop_backend(&mut child);
                    }
                }
            }
        });
}
