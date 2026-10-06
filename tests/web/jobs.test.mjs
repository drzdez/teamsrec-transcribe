// A background job seen through the server's event stream: started, finished, the page reloads on its own,
// and the history lists what happened, newest first.
import assert from "node:assert/strict";
import { test } from "node:test";
import { writeFileSync } from "node:fs";
import { BOARD_NEW, WEEKLY, openPage, waitFor } from "./page.mjs";

/** what teamsrec-capture writes; the test server watches this file (TEAMSREC_TEST_CAPTURE) */
function writeCapture(status) {
  writeFileSync(process.env.TEAMSREC_TEST_CAPTURE, JSON.stringify({ app: "teamsrec-capture", pid: Number(process.env.TEAMSREC_TEST_PID),
                                                                     running: true, ...status }));
}

test("transcribing a new recording: live status over SSE, reload when done, history", async t => {
  const p = await openPage(t, BOARD_NEW);
  const go = await waitFor(() => p.doc.querySelector("#main button.primary-btn"), "the offer to transcribe");
  assert.match(p.doc.querySelector("#main").textContent, /ještě nemá přepis/);
  go.click();
  assert.match(p.$("status").textContent, /potvrďte/, "a long job asks first");
  assert.ok(!p.requests.some(r => r.includes("/process")), "nothing started by the first click");
  go.click();
  await waitFor(() => p.$("quit").disabled, "the page busy while the job runs");
  await waitFor(() => p.doc.querySelector(".card"), "the page reloading the new transcript by itself");
  await waitFor(() => /^hotovo: přepis a titulky; zápis počká/.test(p.$("status").textContent), "the result in the status line");
  assert.ok(!p.$("quit").disabled, "the page is not busy any more");
  assert.ok(p.doc.querySelector("#main").textContent.includes("Nová nahrávka je přepsaná."));

  assert.equal(p.streams.length, 1, "the page listens to the event stream");
  assert.ok(!p.requests.some(r => r.startsWith("GET /api/status")), "no polling while the stream works");

  p.$("histBtn").click();
  const rows = [...p.$("events").querySelectorAll(".ev .tx")].map(e => e.textContent);
  assert.ok(!p.$("events").hidden);
  assert.match(rows[0], /zpracováno/, "newest first");
  assert.ok(rows.some(r => /zpracování spuštěno/.test(r)));
});

test("regenerating the minutes waits for the job's end event, not by polling", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "speaker cards");
  p.input(p.doc.querySelector('.side [data-key="nick"]'), "Kája");  // an unsaved change
  assert.ok(p.$("dirty").classList.contains("changed"));
  p.$("saveSum").click();
  assert.match(p.$("saveSum").textContent, /Potvrdit/, "regenerating the minutes asks first");
  assert.ok(!p.requests.some(r => r.startsWith("PUT ")), "nothing saved by the first click");
  p.$("saveSum").click();
  await waitFor(() => p.$("quit").disabled, "the page busy while the minutes are written");
  assert.ok(p.$("dirty").classList.contains("clean"), "the names are saved: no 'neuložené změny' while the minutes run");
  await waitFor(() => /uloženo, zápis přegenerován/.test(p.$("status").textContent), "the page's own message at the end");
  assert.ok(!p.$("quit").disabled);
  assert.ok(!p.requests.some(r => r.startsWith("GET /api/status")), "no polling while the stream works");
});

test("transcribing again from scratch: armed button, REST call, the page reloads the new speakers", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelectorAll(".card").length === 3, "the three speakers");
  const again = await waitFor(() => p.button("Přepsat znovu od nuly"), "the button");
  again.click();
  assert.ok(!p.$("quit").disabled, "the first click only arms it");
  p.button("Potvrdit nový přepis").click();
  await waitFor(() => p.$("quit").disabled, "the page busy while the job runs");
  await waitFor(() => p.$("status").textContent === "hotovo: nový přepis, mluvčí jsou znovu k pojmenování", "the job done");
  assert.ok(!p.$("quit").disabled);
  assert.ok(p.requests.includes(`POST /api/recordings/${WEEKLY}/process`));
  await waitFor(() => p.doc.querySelector("#main").textContent.includes("Nová nahrávka je přepsaná."), "the new transcript");
});

test("while teamsrec-capture records, a red dot and the title show next to the status line", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the page");
  assert.ok(p.$("recBadge").hidden, "nothing recorded at first");
  writeCapture({ recording: true, title: "Plánování sprintu", stem: "2026-09-30_1827_planovani-sprintu",
                 source: "onsite", started: "2026-09-30T18:27:05" });
  await waitFor(() => !p.$("recBadge").hidden, "the red dot");
  assert.equal(p.$("recText").textContent, "Nahrávání probíhá · Plánování sprintu · od 18:27 · na místě");
  writeCapture({ recording: false });
  await waitFor(() => p.$("recBadge").hidden, "the dot gone when the recording ends");
});

test("a job started elsewhere (or before a reload) shows as running, a second one as queued", async t => {
  const base = process.env.TEAMSREC_REVIEW_URL;
  const post = (path, body) => fetch(new URL(path, base), { method: "POST", headers: { "Content-Type": "application/json" },
                                                         body: JSON.stringify(body) }).then(r => r.json());
  const a = await post(`/api/recordings/${BOARD_NEW}/process`, {});
  const b = await post(`/api/recordings/${WEEKLY}/summaries`, { provider: "anthropic", model: "claude-opus-5-5" });
  assert.equal(a.position, 1);
  assert.equal(b.position, 2, "queued, not refused");
  const p = await openPage(t, WEEKLY);  // like a reload while the jobs run
  await waitFor(() => !p.$("jobBadge").hidden, "the running job on a page that did not start it");
  assert.match(p.$("jobText").textContent, /běží: .*zpracování spuštěno · od \d\d:\d\d · ve frontě: 1/);
  await waitFor(() => p.$("jobText").textContent.includes("zápis claude-opus-5-5 se generuje") &&
                      !p.$("jobText").textContent.includes("ve frontě"), "the queued one running next", 6000);
  await waitFor(() => p.$("jobBadge").hidden, "nothing left", 6000);
});

test("the history closes with a click anywhere outside it, or Esc", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the page");
  p.$("histBtn").click();
  assert.ok(!p.$("events").hidden);
  p.$("events").dispatchEvent(new p.window.MouseEvent("click", { bubbles: true }));
  assert.ok(!p.$("events").hidden, "a click inside keeps it open");
  p.doc.querySelector(".card").dispatchEvent(new p.window.MouseEvent("click", { bubbles: true }));
  assert.ok(p.$("events").hidden, "a click elsewhere closes it");
  p.$("histBtn").click();
  p.doc.dispatchEvent(new p.window.KeyboardEvent("keydown", { key: "Escape" }));
  assert.ok(p.$("events").hidden, "Esc closes it");
});

test("an armed action goes back by itself or with Zrušit, and does nothing", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the page");
  const b = p.$("saveSum");
  b.click();
  const cancel = b.nextElementSibling;
  assert.equal(cancel.textContent, "Zrušit");
  cancel.click();
  assert.equal(b.textContent, "Uložit a přegenerovat zápis");
  assert.equal(p.$("status").textContent, "", "the question leaves the status line");
  b.click();
  await waitFor(() => b.textContent === "Uložit a přegenerovat zápis", "back after 6 s", 8000);
  assert.ok(!p.requests.some(r => r.startsWith("PUT ")), "nothing happened");
});

