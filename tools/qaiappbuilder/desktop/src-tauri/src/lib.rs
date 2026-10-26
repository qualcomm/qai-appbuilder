// ---------------------------------------------------------------------
// Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
// SPDX-License-Identifier: BSD-3-Clause
// ---------------------------------------------------------------------

//! QAI ModelBuilder desktop shell.
//!
//! The window is only a container for the page the backend already serves (`http://localhost:<port>/`), so the
//! frontend is not touched. The shell:
//!   1. finds the app root + the venv python, and refuses to start when the port is taken (the SSO redirect_uri pins it);
//!   2. shows a small splash while a worker thread spawns the backend and waits for `/api/system/health`;
//!   3. navigates the window to the app; closing it asks "Quit / Minimize to tray";
//!   4. stops the backend gracefully (`POST /api/system/exit`) on quit; the Job Object reaps it if the shell dies.
mod backend;
mod close_dialog;
mod i18n;
mod layout;
mod settings;

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

const MAIN: &str = "main";
const READY_LIMIT: Duration = Duration::from_secs(90);

struct Shell {
    root: PathBuf,
    port: u16,
    lang: i18n::Lang,
    backend: Mutex<Option<backend::Backend>>,
    /// The app page is shown (false while the splash runs or after a start-up error).
    ready: AtomicBool,
    quitting: AtomicBool,
    /// The close question is on screen (a second close request must not stack another dialog).
    asking: AtomicBool,
}

type Sh = Arc<Shell>;

fn t(sh: &Shell) -> &'static i18n::T {
    i18n::texts(sh.lang)
}

fn base_url(port: u16) -> String {
    format!("http://localhost:{port}")
}

// ---------------------------------------------------------------------------------------------------------------
// start-up
// ---------------------------------------------------------------------------------------------------------------

fn set_splash(win: &WebviewWindow, title: &str, msg: &str, error: bool) {
    let js = format!(
        "document.getElementById('title').textContent={};document.getElementById('msg').textContent={};document.body.classList.toggle('err',{});",
        serde_json::to_string(title).unwrap_or_default(),
        serde_json::to_string(msg).unwrap_or_default(),
        error
    );
    let _ = win.eval(&js);
}

fn boot(app: AppHandle, sh: Sh) {
    let Some(win) = app.get_webview_window(MAIN) else { return };
    let tx = t(&sh);
    set_splash(&win, tx.app_title, tx.splash_starting, false);

    if backend::port_in_use(sh.port) {
        let m = tx.err_port_busy.replace("{port}", &sh.port.to_string());
        set_splash(&win, tx.splash_failed, &m, true);
        return;
    }
    let Some(python) = layout::venv_python(&sh.root) else {
        set_splash(&win, tx.splash_failed, tx.err_no_venv, true);
        return;
    };
    let b = match backend::spawn(&sh.root, &python, sh.port) {
        Ok(b) => b,
        Err(e) => {
            set_splash(&win, tx.splash_failed, &format!("{}\n{e}", tx.err_spawn), true);
            return;
        }
    };
    *sh.backend.lock().unwrap() = Some(b);

    let t0 = std::time::Instant::now();
    loop {
        if sh.quitting.load(Ordering::SeqCst) {
            return;
        }
        let exited = {
            let mut g = sh.backend.lock().unwrap();
            g.as_mut().and_then(backend::exited)
        };
        if let Some(code) = exited {
            let hint = format!("{}\n(exit code {})\n{}", tx.err_exited, code.map_or("?".into(), |c| c.to_string()), layout::log_dir(&sh.root).join("desktop-backend.log").display());
            set_splash(&win, tx.splash_failed, &hint, true);
            return;
        }
        if backend::healthy(sh.port) {
            break;
        }
        if t0.elapsed() > READY_LIMIT {
            set_splash(&win, tx.splash_failed, tx.err_timeout, true);
            return;
        }
        std::thread::sleep(Duration::from_millis(400));
    }
    sh.ready.store(true, Ordering::SeqCst);
    if let Ok(u) = format!("{}/chat", base_url(sh.port)).parse::<Url>() {
        let _ = win.navigate(u);
    }
    watch(app, sh);
}

/// After start-up: if the supervisor process ends while the shell is still running (crash, killed from outside), the page
/// would silently sit on a dead backend. Go back to the splash page with the reason, and show the window.
/// (A normal restart from the UI is handled inside the supervisor; the supervisor process itself stays alive.)
fn watch(app: AppHandle, sh: Sh) {
    loop {
        std::thread::sleep(Duration::from_secs(1));
        if sh.quitting.load(Ordering::SeqCst) {
            return;
        }
        let exited = {
            let mut g = sh.backend.lock().unwrap();
            match g.as_mut() {
                Some(b) => backend::exited(b),
                None => return,
            }
        };
        if let Some(code) = exited {
            sh.ready.store(false, Ordering::SeqCst);
            let tx = t(&sh);
            if let Some(win) = app.get_webview_window(MAIN) {
                if let Ok(u) = Url::parse("http://tauri.localhost/index.html") {
                    let _ = win.navigate(u);
                }
                let hint = format!(
                    "{}\n(exit code {})\n{}",
                    tx.err_exited,
                    code.map_or("?".into(), |c| c.to_string()),
                    layout::log_dir(&sh.root).join("desktop-backend.log").display()
                );
                // the splash page needs a moment to load before it can be written to
                std::thread::sleep(Duration::from_millis(800));
                set_splash(&win, tx.splash_failed, &hint, true);
                let _ = win.show();
                let _ = win.unminimize();
            }
            return;
        }
    }
}

// ---------------------------------------------------------------------------------------------------------------
// window + tray + quit
// ---------------------------------------------------------------------------------------------------------------

fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
}

fn is_same_origin(u: &Url, port: u16) -> bool {
    u.scheme() == "http" && matches!(u.host_str(), Some("localhost") | Some("127.0.0.1")) && u.port_or_known_default() == Some(port)
}

#[cfg(windows)]
fn open_external(url: &str) -> bool {
    use windows::core::{w, PCWSTR};
    use windows::Win32::UI::Shell::ShellExecuteW;
    use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
    let wide: Vec<u16> = url.encode_utf16().chain(std::iter::once(0)).collect();
    let r = unsafe { ShellExecuteW(None, w!("open"), PCWSTR(wide.as_ptr()), PCWSTR::null(), PCWSTR::null(), SW_SHOWNORMAL) };
    r.0 as isize > 32
}

#[cfg(not(windows))]
fn open_external(_url: &str) -> bool {
    false
}

/// Stop the backend (at most once) and leave. Runs on a worker thread so the UI thread never blocks on the HTTP stop.
fn quit(app: &AppHandle, sh: &Sh) {
    if sh.quitting.swap(true, Ordering::SeqCst) {
        return;
    }
    if let Some(w) = app.get_webview_window(MAIN) {
        let _ = w.hide();
    }
    let app = app.clone();
    let sh = sh.clone();
    std::thread::spawn(move || {
        if let Some(mut b) = sh.backend.lock().unwrap().take() {
            backend::stop(&mut b);
        }
        app.exit(0);
    });
}

/// The main window's close button.
fn on_close_requested(app: &AppHandle, sh: &Sh) {
    if sh.quitting.load(Ordering::SeqCst) {
        return;
    }
    // Before the app page is up (splash / error) there is nothing worth keeping alive: just leave.
    if !sh.ready.load(Ordering::SeqCst) {
        quit(app, sh);
        return;
    }
    match settings::load_close_action(&sh.root).as_str() {
        "quit" => quit(app, sh),
        "tray" => {
            if let Some(w) = app.get_webview_window(MAIN) {
                let _ = w.hide();
            }
        }
        _ => {
            if sh.asking.swap(true, Ordering::SeqCst) {
                return;
            }
            let app = app.clone();
            let sh = sh.clone();
            // TaskDialog runs its own message loop: keep it off the event-loop thread.
            std::thread::spawn(move || {
                let (choice, remember) = close_dialog::ask(t(&sh));
                sh.asking.store(false, Ordering::SeqCst);
                match choice {
                    close_dialog::Choice::Quit => {
                        if remember {
                            settings::save_close_action(&sh.root, "quit");
                        }
                        quit(&app, &sh);
                    }
                    close_dialog::Choice::MinimizeToTray => {
                        if remember {
                            settings::save_close_action(&sh.root, "tray");
                        }
                        if let Some(w) = app.get_webview_window(MAIN) {
                            let _ = w.hide();
                        }
                    }
                    close_dialog::Choice::Cancel => {}
                }
            });
        }
    }
}

fn build_tray(app: &AppHandle, sh: &Sh) -> tauri::Result<()> {
    let tx = t(sh);
    let show = MenuItem::with_id(app, "show", tx.tray_show, true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", tx.tray_quit, true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &quit_i])?;
    let sh_menu = sh.clone();
    let mut b = TrayIconBuilder::with_id("main")
        .tooltip(tx.tray_tip)
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, ev| match ev.id().as_ref() {
            "show" => show_main(app),
            "quit" => quit(app, &sh_menu),
            _ => {}
        })
        .on_tray_icon_event(|tray, ev| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = ev {
                show_main(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        b = b.icon(icon.clone());
    }
    b.build(app)?;
    Ok(())
}

pub fn run() {
    let lang = i18n::detect();
    let root = layout::find_app_root();
    let port = root.as_deref().map(layout::backend_port).unwrap_or(8989);
    let sh: Sh = Arc::new(Shell {
        root: root.clone().unwrap_or_default(),
        port,
        lang,
        backend: Mutex::new(None),
        ready: AtomicBool::new(false),
        quitting: AtomicBool::new(false),
        asking: AtomicBool::new(false),
    });
    let have_root = root.is_some();

    let sh_setup = sh.clone();
    let sh_event = sh.clone();
    let app = tauri::Builder::default()
        // A second start brings the running window to the front instead of starting another backend.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| show_main(app)))
        .setup(move |app| {
            let sh = sh_setup.clone();
            let handle = app.handle().clone();
            let sh_nav = sh.clone();
            let sh_new = sh.clone();
            let tx = t(&sh);
            WebviewWindowBuilder::new(app, MAIN, WebviewUrl::App("index.html".into()))
                .title(tx.app_title)
                .inner_size(1360.0, 860.0)
                .min_inner_size(900.0, 600.0)
                .center()
                // Links: the app's own origin stays in the window; every other http(s) link opens in the browser;
                // any other scheme (file:, javascript:, ...) is refused.
                .on_navigation(move |u| {
                    if u.scheme() == "tauri" || u.host_str() == Some("tauri.localhost") || is_same_origin(u, sh_nav.port) {
                        return true;
                    }
                    if u.scheme() == "http" || u.scheme() == "https" {
                        let _ = open_external(u.as_str());
                    }
                    false
                })
                .on_new_window(move |u, _features| {
                    if is_same_origin(&u, sh_new.port) {
                        // SSO sign-in and other same-origin popups: let the page have its own small window.
                        return tauri::webview::NewWindowResponse::Allow;
                    }
                    if u.scheme() == "http" || u.scheme() == "https" {
                        let _ = open_external(u.as_str());
                    }
                    tauri::webview::NewWindowResponse::Deny
                })
                .build()?;
            build_tray(&handle, &sh)?;
            if !have_root {
                if let Some(w) = handle.get_webview_window(MAIN) {
                    // the page is loaded asynchronously; give it a moment before writing into it
                    let (w, msg, title) = (w, tx.err_no_root, tx.splash_failed);
                    std::thread::spawn(move || {
                        std::thread::sleep(Duration::from_millis(700));
                        set_splash(&w, title, msg, true);
                    });
                }
            } else {
                let h = handle.clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(500));
                    boot(h, sh);
                });
            }
            Ok(())
        })
        .on_window_event(move |window, ev| {
            if window.label() != MAIN {
                return;
            }
            if let WindowEvent::CloseRequested { api, .. } = ev {
                api.prevent_close();
                on_close_requested(window.app_handle(), &sh_event);
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build the QAI ModelBuilder shell");

    let sh_run = sh.clone();
    app.run(move |app_handle, ev| {
        // Any exit we did not start (e.g. the OS ending the session): still stop the backend.
        if let RunEvent::ExitRequested { api, .. } = &ev {
            if !sh_run.quitting.load(Ordering::SeqCst) {
                api.prevent_exit();
                quit(app_handle, &sh_run);
            }
        }
    });
}


