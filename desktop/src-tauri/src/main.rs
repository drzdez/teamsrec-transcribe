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
//! In the installed suite (`transcribe\` and `uv.exe` next to this exe) the server runs from the suite's own Python
//! environment, which the first start creates and an update refreshes (env.rs), with uv's progress in the window.
//!
//! `teamsrec-review.exe --browser` opens the page in the default browser instead and exits (the tray icon of
//! teamsrec-capture uses it when `[capture] tray_open = "web"`). A second start of the window only brings the open
//! window to the front. `--settings` opens the page's Nastavení (the tray's Settings…), `--wizard` its setup wizard
//! (the tray's Setup wizard…), in either case.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod env;

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::{Manager, State, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder, WindowEvent};

/// desktop/src-tauri of the checkout this was built from: the server command is found next to it
const MANIFEST_DIR: &str = env!("CARGO_MANIFEST_DIR");
const START_TIMEOUT: Duration = Duration::from_secs(90); // the first start imports torch & co.

#[cfg(windows)]
pub(crate) const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn exe_dir() -> PathBuf {
    std::env::current_exe().ok().and_then(|p| p.parent().map(PathBuf::from)).unwrap_or_default()
}

/// The installed suite: (its uv, its project, the environment) – None in a source checkout.
fn suite() -> Option<(PathBuf, PathBuf, PathBuf)> {
    let dir = exe_dir();
    let project = env::bundled_project(&dir)?;
    Some((dir.join(if cfg!(windows) { "uv.exe" } else { "uv" }), project, env::env_dir(&env::home())))
}

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

/// How to start the server: $TEAMSREC_TRANSCRIBE_CMD (a developer's checkout), else the installed suite's
/// environment, else bin\teamsrec-transcribe.cmd of the checkout this was built from (it runs the checkout's
/// venv), else `teamsrec-transcribe` on PATH.
fn server_command() -> Command {
    let exe = std::env::var("TEAMSREC_TRANSCRIBE_CMD").ok().map(PathBuf::from)
        .or_else(|| suite().map(|(_, _, env)| env::server_exe(&env)))
        .or_else(|| {
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
    // the suite's own ffmpeg for the server and its jobs (a developer's TEAMSREC_FFMPEG_DIR or PATH wins)
    let ff = exe_dir().join("ffmpeg");
    if std::env::var_os("TEAMSREC_FFMPEG_DIR").is_none() && ff.join("ffmpeg.exe").exists() {
        c.env("TEAMSREC_FFMPEG_DIR", ff);
    }
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

fn js_str(text: &str) -> String {
    serde_json::Value::String(text.to_string()).to_string()
}

fn show_error(window: &WebviewWindow, text: &str) {
    let _ = window.eval(&format!("typeof showError === 'function' && showError({})", js_str(text)));
}

/// What the start screen shows while the environment is being prepared.
fn show_progress(window: &WebviewWindow, headline: &str, line: Option<&str>) {
    let line = line.map(js_str).unwrap_or_else(|| "null".into());
    let _ = window.eval(&format!("typeof showProgress === 'function' && showProgress({}, {line})", js_str(headline)));
}

/// What the window needs to (re)start: the server it shows, whether it started it, what to open.
struct Shared {
    server: Arc<Mutex<Option<String>>>,
    started_here: Arc<Mutex<bool>>,
    open: Option<&'static str>,
    stem: Option<String>,
}

const FIRST_START: &str = "První spuštění: připravuji prostředí pro zpracování hlasu (Python, PyTorch, WhisperX, \
    pyannote – asi 4–6 GB). Podle připojení 5–20 minut; okno nechte otevřené.";
const AFTER_UPDATE: &str = "Po aktualizaci: doplňuji prostředí pro zpracování hlasu (stahuje se jen to, co se změnilo).";

/// The suite's environment when it is missing or from an older build, then the server, then the page.
fn start(window: WebviewWindow, shared: &Shared) {
    if std::env::var_os("TEAMSREC_TRANSCRIBE_CMD").is_none() {
        if let Some((uv, project, envdir)) = suite() {
            if !env::is_current(&project, &envdir) {
                let headline = if env::server_exe(&envdir).exists() { AFTER_UPDATE } else { FIRST_START };
                show_progress(&window, headline, None);
                if let Err(e) = env::sync(&uv, &project, &envdir, |l| show_progress(&window, headline, Some(l))) {
                    show_error(&window, &format!("Prostředí se nepodařilo připravit:\n{e}"));
                    return;
                }
                show_progress(&window, "Prostředí je připravené, spouštím stránku…", None);
            }
        }
    }
    match ensure_server() {
        Ok((url, started)) => {
            if let Ok(mut s) = shared.server.lock() {
                *s = Some(url.clone());
            }
            if let Ok(mut s) = shared.started_here.lock() {
                *s = started;
            }
            let extra = shared.open.map(|o| format!("&open={o}")).unwrap_or_default();
            let hash = shared.stem.as_ref().map(|s| format!("#{s}")).unwrap_or_default();
            match Url::parse(&format!("{url}/?app=desktop{extra}{hash}")) {
                Ok(page) => {
                    if let Err(e) = window.navigate(page) {
                        show_error(&window, &format!("Stránku se nepodařilo otevřít: {e}"));
                    }
                }
                Err(e) => show_error(&window, &format!("Neplatná adresa serveru {url}: {e}")),
            }
        }
        Err(msg) => show_error(&window, &msg),
    }
}

/// "Zkusit znovu" on the start screen after a failed preparation (no internet, a full disk).
#[tauri::command]
fn retry(window: WebviewWindow, shared: State<'_, Arc<Shared>>) {
    let shared = shared.inner().clone();
    std::thread::spawn(move || start(window, &shared));
}

/// What the page opens over the recordings: Nastavení (--settings) or the setup wizard (--wizard).
fn open_target(args: &[String]) -> Option<&'static str> {
    if args.iter().skip(1).any(|a| a == "--wizard") {
        Some("wizard")
    } else if args.iter().skip(1).any(|a| a == "--settings") {
        Some("settings")
    } else {
        None
    }
}

/// `--open <stem>`: the recording to show (a click on the "Saved …" balloon of the capture app). Only characters a
/// stem has (date_time_slug), because it ends up in a URL and in JavaScript.
fn open_stem(args: &[String]) -> Option<String> {
    let i = args.iter().position(|a| a == "--open")?;
    let stem = args.get(i + 1)?;
    (!stem.is_empty() && stem.chars().all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')).then(|| stem.clone())
}

/// `--browser`: the page in the default browser, no window. The server stays until the browser tab closes.
fn open_in_browser(open: Option<&str>, stem: Option<String>) {
    let query = open.map(|o| format!("?open={o}")).unwrap_or_default();
    let hash = stem.map(|s| format!("#{s}")).unwrap_or_default();
    match ensure_server().and_then(|(url, _)| Url::parse(&format!("{url}/{query}{hash}")).map_err(|e| e.to_string())) {
        Ok(page) => open_external(&page),
        Err(msg) => eprintln!("{msg}"),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let open = open_target(&args);
    let stem = open_stem(&args);
    // --browser needs a ready environment; a first start or an update prepares it in the window instead
    let env_ready = std::env::var_os("TEAMSREC_TRANSCRIBE_CMD").is_some()
        || suite().is_none_or(|(_, project, envdir)| env::is_current(&project, &envdir));
    if args.iter().skip(1).any(|a| a == "--browser") && env_ready {
        open_in_browser(open, stem);
        return;
    }
    // the server this window shows (for the navigation filter) and whether this program started it
    let server: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let started_here = Arc::new(Mutex::new(false));

    let shared = Arc::new(Shared { server: server.clone(), started_here: started_here.clone(), open,
                                   stem: stem.clone() });

    tauri::Builder::default()
        .manage(shared.clone())
        .invoke_handler(tauri::generate_handler![retry])
        // a second start (the tray icon, the Start menu) brings the open window to the front
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
                match open_target(&args) {
                    Some("settings") => { let _ = w.eval("typeof openSettings === 'function' && openSettings()"); }
                    Some("wizard") => { let _ = w.eval("typeof openWizard === 'function' && openWizard()"); }
                    _ => {}
                }
                if let Some(stem) = open_stem(&args) {
                    let _ = w.eval(&format!("location.hash = '{stem}'")); // the page switches on hashchange
                }
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

                let shared = shared.clone();
                std::thread::spawn(move || start(window, &shared));
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
    fn open_takes_a_stem_and_nothing_else() {
        let a = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert_eq!(open_stem(&a(&["x.exe", "--open", "2026-09-30_1827_zina"])), Some("2026-09-30_1827_zina".into()));
        assert_eq!(open_stem(&a(&["x.exe", "--open", "a'; alert(1); '"])), None);
        assert_eq!(open_stem(&a(&["x.exe", "--open"])), None);
        assert_eq!(open_stem(&a(&["x.exe", "--settings"])), None);
        assert_eq!(open_target(&a(&["x.exe", "--settings"])), Some("settings"));
        assert_eq!(open_target(&a(&["x.exe", "--wizard"])), Some("wizard"));
        assert_eq!(open_target(&a(&["x.exe", "--open", "x"])), None);
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
