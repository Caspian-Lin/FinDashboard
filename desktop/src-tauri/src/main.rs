#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(windows)]
mod native_caption;
mod operator;
mod window_frame;

use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

use operator::{
    execute, open_in_default_browser, Invocation, Operation, OperationResult, Progress,
};
use serde::Deserialize;
use tauri::{
    ipc::Channel, webview::NewWindowResponse, AppHandle, Manager, State, Url, WebviewUrl,
    WebviewWindow, WebviewWindowBuilder,
};
use window_frame::WorkbenchPort;

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

fn with_local_webview_profile<'a, M: Manager<tauri::Wry>>(
    builder: WebviewWindowBuilder<'a, tauri::Wry, M>,
) -> std::io::Result<WebviewWindowBuilder<'a, tauri::Wry, M>> {
    if cfg!(feature = "standalone") {
        let profile = std::env::current_exe()?.with_file_name("webview2-profile");
        return Ok(builder.data_directory(dunce::simplified(&profile).to_owned()));
    }
    Ok(builder)
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

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum WorkbenchAction {
    InApp,
    Browser,
    Address,
    ReleaseBundles,
}

fn workbench_url(status: &OperationResult) -> Result<Url, String> {
    let port = status
        .status
        .as_ref()
        .filter(|state| status.exit_code == 0 && state.running && state.port >= 1024)
        .map(|state| state.port)
        .ok_or_else(|| "研究服务尚未就绪，请启动后重试。".to_owned())?;
    Url::parse(&format!("http://127.0.0.1:{port}/research")).map_err(|_| "端口无效。".into())
}

fn release_bundles_url(mut origin: Url) -> Url {
    origin.set_path("/data");
    origin.set_query(Some("tab=sync"));
    origin.set_fragment(Some("release-bundles"));
    origin
}

#[tauri::command]
async fn desktop_open_workbench(
    app: AppHandle,
    window: WebviewWindow,
    operations: State<'_, Operations>,
    workbench_port: State<'_, WorkbenchPort>,
    action: WorkbenchAction,
    appearance: Option<window_frame::DesktopAppearance>,
) -> Result<Option<String>, String> {
    authorize(&window)?;
    let _permit = operations.acquire()?;
    let path = operator_path(&app)?;
    let status =
        tauri::async_runtime::spawn_blocking(move || execute(&path, &Invocation::Status, |_| {}))
            .await
            .map_err(|_| "无法确认研究服务状态。")??;
    let mut url = workbench_url(&status)?;
    let navigate = matches!(&action, WorkbenchAction::ReleaseBundles);
    match action {
        WorkbenchAction::Address => return Ok(Some(url.into())),
        WorkbenchAction::Browser => {
            tauri::async_runtime::spawn_blocking(move || open_in_default_browser(&url))
                .await
                .map_err(|_| "浏览器未能打开，可复制地址后重试。")??;
            return Ok(None);
        }
        WorkbenchAction::InApp => {}
        WorkbenchAction::ReleaseBundles => url = release_bundles_url(url),
    }
    show_workbench(
        &app,
        &workbench_port,
        url,
        navigate,
        appearance.unwrap_or_default(),
    )?;
    Ok(None)
}

fn show_workbench(
    app: &AppHandle,
    workbench_port: &WorkbenchPort,
    url: Url,
    navigate: bool,
    appearance: window_frame::DesktopAppearance,
) -> Result<(), String> {
    let port = url.port().ok_or("端口无效。")?;
    if let Some(workbench) = app.get_webview_window("research") {
        let current = workbench.url().ok();
        let recovering = current.as_ref().is_some_and(|url| {
            url.as_str() == window_frame::WORKBENCH_UNAVAILABLE && workbench_port.get() == port
        });
        if recovering
            || current
                .as_ref()
                .is_some_and(|url| is_workbench_origin(url, port))
        {
            if recovering || navigate {
                workbench
                    .navigate(url)
                    .map_err(|_| "无法重新打开研究工作台。")?;
            }
            if workbench.is_minimized().unwrap_or(false) {
                workbench.unminimize().map_err(|_| "无法还原研究工作台。")?;
            }
            return workbench
                .set_focus()
                .map_err(|_| "无法切换到研究工作台。".into());
        }
        workbench.destroy().map_err(|_| "无法关闭旧研究工作台。")?;
    }
    let frame = window_frame::initialization(
        "research",
        &url.origin().ascii_serialization(),
        Some(appearance),
    );
    let builder = WebviewWindowBuilder::new(app, "research", WebviewUrl::External(url))
        .title("FinDashboard 研究工作台")
        .inner_size(1240.0, 800.0)
        .min_inner_size(330.0, 240.0)
        .decorations(false)
        .use_https_scheme(true)
        .incognito(true)
        .initialization_script(frame)
        .on_navigation(move |url| {
            is_workbench_origin(url, port) || url.as_str() == window_frame::WORKBENCH_UNAVAILABLE
        })
        .on_new_window(|_, _| NewWindowResponse::Deny);
    let builder =
        with_local_webview_profile(builder).map_err(|_| "无法确定 WebView2 配置目录。")?;
    workbench_port.set(port);
    let window = builder.build().map_err(|_| {
        workbench_port.set(0);
        "无法打开研究工作台，请检查 WebView2。"
    })?;
    #[cfg(windows)]
    native_caption::attach(&window).map_err(|_| "无法初始化 Windows 窗口控制。")?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .manage(Operations::default())
        .manage(WorkbenchPort::default())
        .invoke_handler(tauri::generate_handler![
            desktop_operation,
            desktop_open_workbench,
            window_frame::desktop_window_action
        ])
        .setup(|app| {
            let builder = WebviewWindowBuilder::from_config(app, &app.config().app.windows[0])?;
            with_local_webview_profile(builder)?
                .initialization_script(window_frame::initialization(
                    "management",
                    "https://tauri.localhost",
                    None,
                ))
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
    #[test]
    fn release_bundle_location_keeps_the_validated_instance_origin() {
        let origin = super::Url::parse("http://127.0.0.1:18191/research").unwrap();
        let location = super::release_bundles_url(origin);
        assert!(super::is_workbench_origin(&location, 18191));
        assert_eq!(location.path(), "/data");
        assert_eq!(location.query(), Some("tab=sync"));
        assert_eq!(location.fragment(), Some("release-bundles"));
    }

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
    fn browser_and_clipboard_require_current_ready_instance() {
        let mut result = OperationResult {
            exit_code: 0,
            elapsed_seconds: 0.0,
            command: String::new(),
            source: None,
            connection: None,
            workspace: None,
            status: Some(
                serde_json::from_value(serde_json::json!({
                    "docker": "linux", "installed": true, "has_instance": true,
                    "phase": "ready", "running": true, "port": 8376
                }))
                .unwrap(),
            ),
        };
        assert_eq!(
            workbench_url(&result).unwrap().as_str(),
            "http://127.0.0.1:8376/research"
        );
        result.status.as_mut().unwrap().running = false;
        assert!(workbench_url(&result).is_err());
        let state = result.status.as_mut().unwrap();
        state.running = true;
        state.port = 9184;
        assert_eq!(
            workbench_url(&result).unwrap().as_str(),
            "http://127.0.0.1:9184/research"
        );
        result.status.as_mut().unwrap().port = 80;
        assert!(workbench_url(&result).is_err());
        result.status.as_mut().unwrap().port = 9184;
        result.exit_code = 1;
        assert!(workbench_url(&result).is_err());
        assert!(serde_json::from_str::<WorkbenchAction>(r#""https://example.test""#).is_err());
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
