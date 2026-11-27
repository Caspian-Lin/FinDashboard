#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod operator;

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

use operator::{execute, Invocation, Operation, OperationResult, Progress};
use tauri::{
    ipc::Channel, webview::NewWindowResponse, AppHandle, Manager, State, Url, WebviewUrl,
    WebviewWindow, WebviewWindowBuilder,
};

#[derive(Default)]
struct Operations(Arc<AtomicBool>);

struct Permit(Arc<AtomicBool>);

impl Drop for Permit {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

impl Operations {
    fn acquire(&self) -> Result<Permit, String> {
        self.0
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .map_err(|_| "已有操作正在运行，请稍后重试。".to_owned())?;
        Ok(Permit(Arc::clone(&self.0)))
    }
}

fn is_management_page(url: &Url) -> bool {
    url.scheme() == "https"
        && url.host_str() == Some("tauri.localhost")
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && matches!(url.path(), "/" | "/index.html")
}

fn authorize(window: &WebviewWindow) -> Result<(), String> {
    let url = window.url().map_err(|_| "无法确认管理界面来源。")?;
    if window.label() == "management" && is_management_page(&url) {
        Ok(())
    } else {
        Err("当前视图无权执行主机操作。".into())
    }
}

fn operator_path(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    let path = app
        .path()
        .resource_dir()
        .map_err(|_| "无法定位安装资源。")?
        .join("edition/finboard.ps1");
    if path.is_file() {
        return Ok(path);
    }
    #[cfg(debug_assertions)]
    {
        let development =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("resources/edition/finboard.ps1");
        if development.is_file() {
            return Ok(development);
        }
    }
    Err("研究版安装资源缺失，请重新安装 MSI。".into())
}

#[tauri::command]
async fn desktop_operation(
    app: AppHandle,
    window: WebviewWindow,
    operations: State<'_, Operations>,
    operation: Operation,
    on_progress: Channel<Progress>,
) -> Result<OperationResult, String> {
    authorize(&window)?;
    let _permit = operations.acquire()?;
    let invocation = Invocation::try_from(operation)?;
    let path = operator_path(&app)?;
    let closes_workbench = invocation.closes_workbench();
    let result = tauri::async_runtime::spawn_blocking(move || {
        execute(&path, &invocation, |progress| {
            let _ = on_progress.send(progress);
        })
    })
    .await
    .map_err(|_| "本机操作意外终止，可刷新状态后重试。")??;
    if result.exit_code == 0 && closes_workbench {
        if let Some(workbench) = app.get_webview_window("research") {
            let _ = workbench.close();
        }
    }
    Ok(result)
}

fn is_workbench_origin(url: &Url, port: u16) -> bool {
    url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.port() == Some(port)
        && url.username().is_empty()
        && url.password().is_none()
}

#[tauri::command]
async fn desktop_open_workbench(
    app: AppHandle,
    window: WebviewWindow,
    operations: State<'_, Operations>,
) -> Result<(), String> {
    authorize(&window)?;
    let _permit = operations.acquire()?;
    let path = operator_path(&app)?;
    let status =
        tauri::async_runtime::spawn_blocking(move || execute(&path, &Invocation::Status, |_| {}))
            .await
            .map_err(|_| "无法确认研究服务状态。")??;
    let port = status
        .status
        .filter(|state| state.running && state.port >= 1024)
        .map(|state| state.port)
        .ok_or_else(|| "研究服务尚未就绪，请启动后重试。".to_owned())?;

    if let Some(workbench) = app.get_webview_window("research") {
        if workbench
            .url()
            .is_ok_and(|url| is_workbench_origin(&url, port))
        {
            return workbench
                .set_focus()
                .map_err(|_| "无法切换到研究工作台。".into());
        }
        workbench.destroy().map_err(|_| "无法关闭旧研究工作台。")?;
    }
    let url = Url::parse(&format!("http://127.0.0.1:{port}/research")).map_err(|_| "端口无效。")?;
    WebviewWindowBuilder::new(&app, "research", WebviewUrl::External(url))
        .title("FinDashboard 研究工作台")
        .inner_size(1240.0, 800.0)
        .incognito(true)
        .on_navigation(move |url| is_workbench_origin(url, port))
        .on_new_window(|_, _| NewWindowResponse::Deny)
        .build()
        .map_err(|_| "无法打开研究工作台，请检查 WebView2。".into())
        .map(|_| ())
}

fn main() {
    tauri::Builder::default()
        .manage(Operations::default())
        .invoke_handler(tauri::generate_handler![
            desktop_operation,
            desktop_open_workbench
        ])
        .setup(|app| {
            WebviewWindowBuilder::from_config(app, &app.config().app.windows[0])?
                .on_navigation(is_management_page)
                .on_new_window(|_, _| NewWindowResponse::Deny)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("FinDashboard requires the Windows WebView2 runtime");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn management_origin_excludes_loopback_and_external_navigation() {
        assert!(is_management_page(
            &Url::parse("https://tauri.localhost/").unwrap()
        ));
        for url in [
            "https://tauri.localhost/.well-known/",
            "http://127.0.0.1:8176/",
            "https://tauri.localhost.example.test/",
            "https://tauri.localhost:8180/",
        ] {
            assert!(!is_management_page(&Url::parse(url).unwrap()));
        }
        assert!(is_workbench_origin(
            &Url::parse("http://127.0.0.1:8176/research").unwrap(),
            8176
        ));
        for url in [
            "http://127.0.0.1:8177/",
            "http://127.0.0.1.example.test:8176/",
            "http://user@127.0.0.1:8176/",
        ] {
            assert!(!is_workbench_origin(&Url::parse(url).unwrap(), 8176));
        }
    }

    #[test]
    fn only_one_host_operation_runs_at_a_time() {
        let operations = Operations::default();
        let permit = operations.acquire().unwrap();
        assert!(operations.acquire().is_err());
        drop(permit);
        assert!(operations.acquire().is_ok());
    }
}
