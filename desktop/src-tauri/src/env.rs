//! The Python environment of the installed suite.
//!
//! The installer (teamsrec-capture's MSI) puts next to this exe `uv.exe` and `transcribe\` – the teamsrec-transcribe
//! project with its exact lock (`uv.lock`) and `suite.json` (version + commit of the build). PyTorch, WhisperX and
//! pyannote are far too big for an installer (several GB), so the first start of the window builds the environment
//! with uv into `%LOCALAPPDATA%\teamsrec\env` (outside the program folder: an update replaces the program, not the
//! environment), showing uv's progress. After an update `suite.json` differs from the stamp in the environment and
//! the same command brings it up to date: only what changed is downloaded.
//!
//! A source checkout (development) has no `transcribe\` next to the exe and never gets here.

use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// `transcribe\` next to the exe: this is the installed suite.
pub fn bundled_project(exe_dir: &Path) -> Option<PathBuf> {
    let p = exe_dir.join("transcribe");
    p.join("pyproject.toml").exists().then_some(p)
}

/// Where the suite keeps its own data: $TEAMSREC_HOME, else %LOCALAPPDATA%\teamsrec.
pub fn home() -> PathBuf {
    if let Some(h) = std::env::var_os("TEAMSREC_HOME") {
        return PathBuf::from(h);
    }
    let base = std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(std::env::temp_dir);
    base.join("teamsrec")
}

pub fn env_dir(home: &Path) -> PathBuf {
    home.join("env")
}

/// The server command inside the environment.
pub fn server_exe(env: &Path) -> PathBuf {
    if cfg!(windows) {
        env.join("Scripts").join("teamsrec-transcribe.exe")
    } else {
        env.join("bin").join("teamsrec-transcribe")
    }
}

fn stamp_path(env: &Path) -> PathBuf {
    env.join("teamsrec-suite.json")
}

/// Is the environment there and made from this build's project? (`suite.json` of the bundle == the stamp)
pub fn is_current(project: &Path, env: &Path) -> bool {
    let want = std::fs::read_to_string(project.join("suite.json")).unwrap_or_default();
    let have = std::fs::read_to_string(stamp_path(env)).unwrap_or_default();
    !want.trim().is_empty() && want.trim() == have.trim() && server_exe(env).exists()
}

/// The uv command that creates or updates the environment from the lock: the transcription extras (whisperx with
/// CUDA PyTorch, the window video), no dev tools, the project installed as a package (not linked to the folder,
/// which the next update replaces) and always rebuilt (its version number does not change with every build).
pub fn sync_command(uv: &Path, project: &Path, env: &Path) -> Command {
    let mut c = Command::new(uv);
    c.args(["sync", "--frozen", "--no-dev", "--extra", "all", "--no-editable",
            "--reinstall-package", "teamsrec-transcribe", "--project"])
        .arg(project)
        .env("UV_PROJECT_ENVIRONMENT", env)
        .env("UV_PYTHON_PREFERENCE", "managed") // uv's own Python 3.12, whatever is (not) installed
        .env("NO_COLOR", "1");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(crate::CREATE_NO_WINDOW);
    }
    c
}

/// A uv output line worth showing (downloads, totals, errors), cleaned for the window.
pub fn progress_line(line: &str) -> Option<String> {
    let t = line.trim();
    if t.is_empty() {
        return None;
    }
    let keep = ["Downloading", "Downloaded", "Resolved", "Prepared", "Installed", "Uninstalled", "Using CPython",
                "Creating", "Building", "Built", "Audited", "error", "Error", "warning", "Caused by", "failed"];
    keep.iter().any(|k| t.starts_with(k) || t.contains(&format!(" {k}"))).then(|| t.to_string())
}

/// Run the sync; `progress` gets each interesting line. Ok(()) when the environment is ready (and stamped), or the
/// last lines of uv's output as the error.
pub fn sync(uv: &Path, project: &Path, env: &Path, mut progress: impl FnMut(&str)) -> Result<(), String> {
    std::fs::create_dir_all(env.parent().unwrap_or(env)).map_err(|e| format!("{}: {e}", env.display()))?;
    let mut child = sync_command(uv, project, env)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("uv se nepodařilo spustit ({}): {e}", uv.display()))?;
    // uv reports on stderr; stdout is drained on its own thread so neither pipe fills up
    let out = child.stdout.take();
    let drain = std::thread::spawn(move || {
        if let Some(o) = out {
            for _ in BufReader::new(o).lines() {}
        }
    });
    let mut tail: Vec<String> = Vec::new();
    if let Some(err) = child.stderr.take() {
        for line in BufReader::new(err).lines().map_while(Result::ok) {
            tail.push(line.clone());
            if tail.len() > 30 {
                tail.remove(0);
            }
            if let Some(p) = progress_line(&line) {
                progress(&p);
            }
        }
    }
    let _ = drain.join();
    let status = child.wait().map_err(|e| e.to_string())?;
    if !status.success() {
        return Err(tail.join("\n"));
    }
    let want = std::fs::read_to_string(project.join("suite.json")).unwrap_or_default();
    std::fs::write(stamp_path(env), want.trim()).map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_installed_suite_has_a_bundled_project() {
        let dir = std::env::temp_dir().join(format!("teamsrec-env-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("transcribe")).unwrap();
        assert!(bundled_project(&dir).is_none(), "a folder without pyproject.toml is not the project");
        std::fs::write(dir.join("transcribe").join("pyproject.toml"), "[project]").unwrap();
        assert_eq!(bundled_project(&dir), Some(dir.join("transcribe")));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_environment_is_current_only_with_the_same_stamp_and_its_server() {
        let dir = std::env::temp_dir().join(format!("teamsrec-env-stamp-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let (project, env) = (dir.join("transcribe"), dir.join("env"));
        std::fs::create_dir_all(&project).unwrap();
        std::fs::create_dir_all(server_exe(&env).parent().unwrap()).unwrap();
        std::fs::write(project.join("suite.json"), r#"{"version":"1.1.0","commit":"abc"}"#).unwrap();
        assert!(!is_current(&project, &env), "never set up");
        std::fs::write(stamp_path(&env), r#"{"version":"1.1.0","commit":"abc"}"#).unwrap();
        assert!(!is_current(&project, &env), "stamped, but the server is missing");
        std::fs::write(server_exe(&env), "").unwrap();
        assert!(is_current(&project, &env));
        std::fs::write(project.join("suite.json"), r#"{"version":"1.1.1","commit":"def"}"#).unwrap();
        assert!(!is_current(&project, &env), "an update changes the stamp");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_window_shows_downloads_and_totals_not_every_line() {
        assert_eq!(progress_line("Downloading torch (2.4GiB)").as_deref(), Some("Downloading torch (2.4GiB)"));
        assert_eq!(progress_line("Installed 152 packages in 41.2s").as_deref(), Some("Installed 152 packages in 41.2s"));
        assert_eq!(progress_line(" + numpy==2.1.3"), None);
        assert_eq!(progress_line(""), None);
        assert!(progress_line("error: Failed to download `torch`").is_some());
    }
}
