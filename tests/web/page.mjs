// The review page (web/index.html) in jsdom, talking to a real review server. tests/test_web_page.py starts the
// server on a prepared recordings folder and passes its address in TEAMSREC_REVIEW_URL.
import { readFileSync } from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";

export const BASE = process.env.TEAMSREC_REVIEW_URL;
if (!BASE) throw new Error("TEAMSREC_REVIEW_URL is not set: run these through pytest (tests/test_web_page.py)");
const HTML = readFileSync(new URL("../../src/teamsrec_transcribe/web/index.html", import.meta.url), "utf8");

// the recordings folder test_web_page.py prepares
export const WEEKLY = "2026-09-04_1400_tydenni-sync";   // transcribed, summary, three speakers
export const BOARD_NEW = "2026-09-03_0900_archi-board";  // not transcribed yet
export const BOARD_OLD = "2026-09-02_0900_archi-board";  // transcribed

export const sleep = ms => new Promise(r => setTimeout(r, ms));

/** wait until fn() is truthy; fails with `what` so a timeout says what never happened */
export async function waitFor(fn, what, ms = 4000) {
  const until = Date.now() + ms;
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() > until) throw new Error(`timed out waiting for: ${what}`);
    await sleep(20);
  }
}

/** the server, asked directly (to check what the page changed) */
export async function server(path, init) {
  const r = await fetch(new URL(path, BASE), init);
  return r.json();
}

/**
 * jsdom has no EventSource: a small one over fetch streaming, so the page gets the server's real SSE stream
 * (`event:` / `id:` / `data:` frames) instead of falling back to polling.
 */
function eventSourceFor(streams) {
  return class TestEventSource {
    constructor(url) {
      this.listeners = {};
      this.onerror = null;
      this.ctrl = new AbortController();
      streams.push(this);
      this.run(new URL(url, BASE));
    }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    close() { this.ctrl.abort(); }
    async run(url) {
      try {
        const r = await fetch(url, { signal: this.ctrl.signal, headers: { Accept: "text/event-stream" } });
        const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += value.replace(/\r\n/g, "\n");
          let end;
          while ((end = buf.indexOf("\n\n")) >= 0) {
            this.dispatch(buf.slice(0, end));
            buf = buf.slice(end + 2);
          }
        }
      } catch (e) {
        if (!this.ctrl.signal.aborted && this.onerror) this.onerror(e);
      }
    }
    dispatch(frame) {
      let type = "message", id = "";
      const data = [];
      for (const line of frame.split("\n")) {
        if (!line || line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const key = line.slice(0, colon), value = line.slice(colon + 1).replace(/^ /, "");
        if (key === "event") type = value;
        else if (key === "id") id = value;
        else if (key === "data") data.push(value);
      }
      for (const fn of this.listeners[type] || []) fn({ type, data: data.join("\n"), lastEventId: id });
    }
  };
}

/**
 * Open the page (optionally on one recording) for test t and wait until the recordings list is in. Returns the
 * window, the document, every request the page made and its open event streams. The page is closed when the test ends, pass or fail, and
 * the test fails on any JS error the page raised.
 */
export async function openPage(t, stem = "", { desktop = false, query = "" } = {}) {
  const errors = [], requests = [], streams = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", e => errors.push(e.message));
  const dom = new JSDOM(HTML, {
    runScripts: "dangerously", url: `${BASE}/${desktop || query ? "?" + [desktop ? "app=desktop" : "", query].filter(Boolean).join("&") : ""}${stem ? "#" + stem : ""}`, virtualConsole: vc,
    beforeParse(window) {
      window.fetch = (url, init) => {
        requests.push(`${(init && init.method) || "GET"} ${url}`);
        return fetch(new URL(String(url), BASE), init);
      };
      window.EventSource = eventSourceFor(streams);
      window.HTMLMediaElement.prototype.pause = () => {};
      window.HTMLMediaElement.prototype.play = async () => {};
    },
  });
  const { window } = dom, doc = window.document;
  t.after(() => {
    for (const s of streams) s.close();  // an open event stream would keep node running
    window.close();
    if (errors.length) throw new Error("JS errors on the page: " + errors.join(" | "));
  });
  await waitFor(() => doc.querySelector("#pick option"), "the recordings list");
  return {
    window, doc, requests, streams,
    $: id => doc.getElementById(id),
    button: (text, root = doc) => [...root.querySelectorAll("button")].find(b => b.textContent.startsWith(text)),
    input(el, value) { el.value = value; el.dispatchEvent(new window.Event("input", { bubbles: true })); },
    change(el, value) { el.value = value; el.dispatchEvent(new window.Event("change", { bubbles: true })); },
  };
}
