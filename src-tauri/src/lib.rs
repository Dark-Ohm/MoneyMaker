use std::sync::Mutex;
use tauri::Manager;
use tauri_plugin_shell::{process::CommandChild, ShellExt};

struct SidecarHandle(Mutex<Option<CommandChild>>);
struct SidecarPort(Mutex<Option<u16>>);

#[tauri::command]
fn get_sidecar_port(port: tauri::State<SidecarPort>) -> Result<u16, String> {
    port.0
        .lock()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "Sidecar port not yet available".to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            let sidecar_command = app.shell().sidecar("moneymaker-sidecar").unwrap();
            let (mut rx, child) = sidecar_command.spawn().expect("Failed to spawn sidecar");

            app.manage(SidecarHandle(Mutex::new(Some(child))));
            app.manage(SidecarPort(Mutex::new(None)));

            let handle = app.handle().clone();

            tauri::async_runtime::spawn(async move {
                while let Some(event) = rx.recv().await {
                    match event {
                        tauri_plugin_shell::process::CommandEvent::Stdout(line) => {
                            let port_str = String::from_utf8_lossy(&line).trim().to_string();
                            if let Ok(port) = port_str.parse::<u16>() {
                                if let Some(state) = handle.try_state::<SidecarPort>() {
                                    if let Ok(mut guard) = state.0.lock() {
                                        *guard = Some(port);
                                    }
                                }
                                eprintln!("[MoneyMaker] Sidecar started on port {}", port);
                            }
                        }
                        tauri_plugin_shell::process::CommandEvent::Stderr(line) => {
                            eprintln!("[MoneyMaker] Sidecar: {}", String::from_utf8_lossy(&line));
                        }
                        tauri_plugin_shell::process::CommandEvent::Error(err) => {
                            eprintln!("[MoneyMaker] Sidecar error: {}", err);
                        }
                        _ => {}
                    }
                }
            });

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_sidecar_port])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if let Some(handle) = window.app_handle().try_state::<SidecarHandle>() {
                    if let Ok(mut guard) = handle.0.lock() {
                        if let Some(child) = guard.take() {
                            child.kill().ok();
                        }
                    }
                }
                api.prevent_close();
                window.close().ok();
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running MoneyMaker");
}
