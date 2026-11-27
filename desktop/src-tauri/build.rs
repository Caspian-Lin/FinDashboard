fn main() {
    let commands = &["desktop_operation", "desktop_open_workbench"];
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(commands)),
    )
    .expect("FinDashboard desktop capability configuration must be valid");
}
