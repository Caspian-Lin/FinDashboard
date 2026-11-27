use std::sync::atomic::{AtomicU16, Ordering};

use tauri::{State, Url, WebviewWindow};

use crate::{is_management_page, is_workbench_origin};

#[derive(Default)]
pub(crate) struct WorkbenchPort(AtomicU16);

impl WorkbenchPort {
    pub(crate) fn set(&self, port: u16) {
        self.0.store(port, Ordering::Relaxed);
    }
}

pub(crate) fn initialization(window: &str, origin: &str) -> String {
    let args = serde_json::json!({
        "window": window,
        "origin": origin,
        "stylesheet": include_str!("../../ui/window-frame.css"),
    });
    format!("({})({args});", include_str!("../../ui/window-frame.js"))
}

#[derive(serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum WindowAction {
    IsMaximized,
    Minimize,
    ToggleMaximize,
    Close,
    StartDragging,
}

fn can_control_window(label: &str, url: &Url, workbench_port: u16) -> bool {
    match label {
        "management" => is_management_page(url),
        "research" => workbench_port != 0 && is_workbench_origin(url, workbench_port),
        _ => false,
    }
}

#[tauri::command]
pub(crate) async fn desktop_window_action(
    window: WebviewWindow,
    workbench_port: State<'_, WorkbenchPort>,
    action: WindowAction,
) -> Result<Option<bool>, String> {
    let url = window.url().map_err(|_| "无法确认窗口来源。")?;
    if !can_control_window(
        window.label(),
        &url,
        workbench_port.0.load(Ordering::Relaxed),
    ) {
        return Err("当前页面无权控制窗口。".into());
    }

    // Tauri 注入调用方窗口。请求参数只指定动作，无法指定目标窗口。
    let result = match action {
        WindowAction::IsMaximized => window.is_maximized().map(Some),
        WindowAction::Minimize => window.minimize().map(|_| None),
        WindowAction::ToggleMaximize => window.is_maximized().and_then(|maximized| {
            if maximized {
                window.unmaximize().map(|_| Some(false))
            } else {
                window.maximize().map(|_| Some(true))
            }
        }),
        WindowAction::Close => window.close().map(|_| None),
        WindowAction::StartDragging => window.start_dragging().map(|_| None),
    };
    result.map_err(|_| "窗口操作未完成。".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn caption_actions_require_the_calling_window_and_active_origin() {
        let launcher = Url::parse("https://tauri.localhost/").unwrap();
        let research = Url::parse("http://127.0.0.1:8376/research/topics").unwrap();
        assert!(can_control_window("management", &launcher, 0));
        assert!(can_control_window("research", &research, 8376));
        assert!(!can_control_window("research", &research, 0));
        assert!(!can_control_window("research", &research, 8276));
        assert!(!can_control_window("research", &launcher, 8376));
        assert!(!can_control_window("management", &research, 8376));
        assert!(!can_control_window("unrelated", &research, 8376));
    }
}
