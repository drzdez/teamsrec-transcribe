# teamsrec desktop (Tauri)

The review page as a desktop window. It is a thin shell on purpose: the page (`src/teamsrec_transcribe/web/index.html`)
and its REST + Server-Sent Events server (`web/review.py`) are exactly the ones the browser uses, so every feature is
written once and works in both.

What the shell does (`src-tauri/src/main.rs`, ~200 lines):

1. finds the running review server through its lock file (`%TEMP%\teamsrec-review.json`), or starts one without a
   browser: `bin\teamsrec-transcribe.cmd review --no-browser` of the checkout it was built from
   (`TEAMSREC_TRANSCRIBE_CMD` overrides the command, `teamsrec-transcribe` on PATH is the last resort);
2. shows the page in a native window (WebView2) with `?app=desktop`, which tells the page it runs here;
3. opens links to anything but the server (help on GitHub, links in minutes) in the default browser;
4. when the window closes, stops the server it started itself (a server that was already running, for example for a
   browser tab, keeps running). While a job runs the server refuses and ends by itself after the job.

`teamsrec-review.exe --browser` opens the page in the default browser instead (no window) and exits; a second start
of the window only brings the open one to the front. The tray icon of teamsrec-capture starts one or the other on a
double click, as `[capture] tray_open` (`app` | `web`) says.

What the page does differently with `?app=desktop`: no Zavřít button (closing the window does it) and no
`target="_blank"` on links (the window routes them). Covered by `tests/web/desktop.test.mjs`.

## Build and run

Needs Rust (rustup, MSVC toolchain), Node.js and WebView2 (part of Windows 11).

```
cd desktop
npm install
npm run build          # -> src-tauri\target\release\teamsrec-review.exe
npm run dev            # debug build with a console
cargo test --manifest-path src-tauri\Cargo.toml
```

The icon set in `src-tauri/icons` is generated from `icon-source.png` with `npm run icons`. The build is an .exe
only (`bundle.active = false`); an installer (MSI/NSIS) can be switched on in `tauri.conf.json` later.
