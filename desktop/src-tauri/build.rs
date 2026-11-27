fn main() {
    let commands = &[
        "desktop_operation",
        "desktop_open_workbench",
        "desktop_window_action",
    ];
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(commands)),
    )
    .expect("FinDashboard desktop capability configuration must be valid");
}
