//! teamsrec review page in a desktop window.
//!
//! A thin shell on purpose: the page (`src/teamsrec_transcribe/web/index.html`) and its REST/SSE server are the
//! very same ones the browser uses, so everything is written once. This program only
//!   1. finds the running review server (the lock file `%TEMP%/teamsrec-review.json` the server writes) or starts
//!      one without a browser (`teamsrec-transcribe review --no-browser`),
//!   2. shows its page in a native window (`?app=desktop` tells the page it runs here),
//!   3. opens links to anything else in the default browser, and
//!   4. stops the server it started when the window closes (the server refuses while a job still runs; it then
//!      ends by itself once the job is done and no page is left).
//!
//! `teamsrec-review.exe --browser` opens the page in the default browser instead and exits (the tray icon of
//! teamsrec-capture uses it when `[capture] tray_open = "web"`). A second start of the window only brings the open
//! window to the front.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::{Manager, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

/// desktop/src-tauri of the checkout this was built from: the server command is found next to it
const MANIFEST_DIR: &str = env!("CARGO_MANIFEST_DIR");
const START_TIMEOUT: Duration = Duration::from_secs(90); // the first start imports torch & co.

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn lock_path() -> PathBuf {
    std::env::temp_dir().join("teamsrec-review.json")
}

/// The URL in the server's lock file, without the trailing slash.
fn lock_url() -> Option<String> {
    let text = std::fs::read_to_string(lock_path()).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    Some(v.get("url")?.as_str()?.trim_end_matches('/').to_string())
}

/// A minimal HTTP request to the local server (no HTTP library for two calls). The raw response, or None.
fn http(base: &str, method: &str, path: &str) -> Option<String> {
    let url = Url::parse(base).ok()?;
    let addr: SocketAddr = format!("{}:{}", url.host_str()?, url.port()?).parse().ok()?;
    let mut s = TcpStream::connect_timeout(&addr, Duration::from_millis(800)).ok()?;
    s.set_read_timeout(Some(Duration::from_secs(5))).ok()?;
    let body = if method == "GET" { "" } else { "{}" };
    write!(
        s,
        "{method} {path} HTTP/1.0\r\nHost: {addr}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    )
    .ok()?;
    let mut out = String::new();
    s.read_to_string(&mut out).ok()?;
    Some(out)
}

/// Is a teamsrec review server answering at this URL?
fn alive(base: &str) -> bool {
    http(base, "GET", "/api/status")
        .map(|r| r.starts_with("HTTP/1.") && r.contains(" 200 ") && r.contains("teamsrec-review"))
        .unwrap_or(false)
}

/// How to start the server: $TEAMSREC_TRANSCRIBE_CMD, else bin\teamsrec-transcribe.cmd of the checkout this was
/// built from (it runs the checkout's venv), else `teamsrec-transcribe` on PATH.
fn server_command() -> Command {
    let exe = std::env::var("TEAMSREC_TRANSCRIBE_CMD").ok().map(PathBuf::from).or_else(|| {
        let cmd = PathBuf::from(MANIFEST_DIR).join("..").join("..").join("bin").join("teamsrec-transcribe.cmd");
        cmd.exists().then_some(cmd)
    });
    let mut c = match exe {
        Some(p) if p.extension().is_some_and(|e| e.eq_ignore_ascii_case("cmd") || e.eq_ignore_ascii_case("bat")) => {
            let mut c = Command::new("cmd");
            c.arg("/c").arg(p);
            c
        }
        Some(p) => Command::new(p),
        None => {
            let mut c = Command::new("cmd");
            c.args(["/c", "teamsrec-transcribe"]);
            c
        }
    };
    c.args(["review", "--no-browser"]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(CREATE_NO_WINDOW);
    }
    c
}

/// The running server's URL, starting one when there is none. Ok(url, started_here) or a message for the window.
fn ensure_server() -> Result<(String, bool), String> {
    if let Some(url) = lock_url() {
        if alive(&url) {
            return Ok((url, false));
        }
    }
    let _ = std::fs::remove_file(lock_path()); // a stale lock of a server that is gone
    let mut cmd = server_command();
    let mut child = cmd.spawn().map_err(|e| format!("Server stránky se nepodařilo spustit: {e}"))?;
    let start = Instant::now();
    while start.elapsed() < START_TIMEOUT {
        if let Some(url) = lock_url() {
            if alive(&url) {
                return Ok((url, true));
            }
        }
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!(
                "Server stránky skončil hned po startu ({status}). Zkuste v konzoli: teamsrec-transcribe review"
            ));
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    Err("Server stránky se nerozběhl do 90 s. Zkuste v konzoli: teamsrec-transcribe review".into())
}

/// Links to anything but the review server (help on GitHub, links in minutes) go to the default browser.
fn is_internal(url: &Url, server: &Option<String>) -> bool {
    match url.scheme() {
        "tauri" | "about" | "data" => true,
        "http" | "https" => {
            let host = url.host_str().unwrap_or("");
            host == "tauri.localhost"
                || server.as_deref().is_some_and(|s| url.as_str().starts_with(s))
        }
        _ => false,
    }
}

fn open_external(url: &Url) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = Command::new("rundll32")
            .args(["url.dll,FileProtocolHandler", url.as_str()])
            .creation_flags(CREATE_NO_WINDOW)
            .spawn();
    }
}

fn show_error(window: &WebviewWindow, text: &str) {
    let js = format!(
        "var m = document.getElementById('msg'); if (m) {{ m.className = 'err'; m.textContent = {}; }}",
        serde_json::Value::String(text.to_string())
    );
    let _ = window.eval(&js);
}

/// `--browser`: the page in the default browser, no window. The server stays until the browser tab closes.
fn open_in_browser() {
    match ensure_server().and_then(|(url, _)| Url::parse(&format!("{url}/")).map_err(|e| e.to_string())) {
        Ok(page) => open_external(&page),
        Err(msg) => eprintln!("{msg}"),
    }
}

fn main() {
    if std::env::args().skip(1).any(|a| a == "--browser") {
        open_in_browser();
        return;
    }
    // the server this window shows (for the navigation filter) and whether this program started it
    let server: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let started_here = Arc::new(Mutex::new(false));

    tauri::Builder::default()
        // a second start (the tray icon, the Start menu) brings the open window to the front
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .setup({
            let server = server.clone();
            let started_here = started_here.clone();
            move |app| {
                let nav_server = server.clone();
                let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("teamsrec – kontrola přepisů")
                    .inner_size(1400.0, 900.0)
                    .min_inner_size(520.0, 400.0)
                    .on_navigation(move |url| {
                        let current = nav_server.lock().map(|s| s.clone()).unwrap_or(None);
                        if is_internal(url, &current) {
                            true
                        } else {
                            open_external(url);
                            false
                        }
                    })
                    .build()?;

                let close_server = server.clone();
                let close_started = started_here.clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { .. } = event {
                        let started = close_started.lock().map(|s| *s).unwrap_or(false);
                        let url = close_server.lock().map(|s| s.clone()).unwrap_or(None);
                        if let (true, Some(url)) = (started, url) {
                            let _ = http(&url, "POST", "/api/quit"); // refused while a job runs: it ends later
                        }
                    }
                });

                std::thread::spawn(move || match ensure_server() {
                    Ok((url, started)) => {
                        if let Ok(mut s) = server.lock() {
                            *s = Some(url.clone());
                        }
                        if let Ok(mut s) = started_here.lock() {
                            *s = started;
                        }
                        match Url::parse(&format!("{url}/?app=desktop")) {
                            Ok(page) => {
                                if let Err(e) = window.navigate(page) {
                                    show_error(&window, &format!("Stránku se nepodařilo otevřít: {e}"));
                                }
                            }
                            Err(e) => show_error(&window, &format!("Neplatná adresa serveru {url}: {e}")),
                        }
                    }
                    Err(msg) => show_error(&window, &msg),
                });
                Ok(())
            }
        })
        .run(tauri::generate_context!())
        .expect("teamsrec desktop failed to start");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn u(s: &str) -> Url {
        Url::parse(s).unwrap()
    }

    #[test]
    fn the_review_server_and_the_splash_stay_in_the_window_everything_else_goes_to_the_browser() {
        let server = Some("http://127.0.0.1:55245".to_string());
        assert!(is_internal(&u("http://127.0.0.1:55245/?app=desktop#2026-09-30_0915_archi-standup"), &server));
        assert!(is_internal(&u("http://tauri.localhost/index.html"), &server));
        assert!(is_internal(&u("tauri://localhost/index.html"), &server));
        assert!(!is_internal(&u("http://127.0.0.1:9999/"), &server), "another local server is not the page");
        assert!(!is_internal(&u("https://github.com/drzdez/teamsrec-transcribe/blob/main/docs/install.md"), &server));
        assert!(!is_internal(&u("mailto:jan.novak@example.org"), &server));
        assert!(!is_internal(&u("http://127.0.0.1:55245/"), &None), "before the server is known only the splash");
    }
}
