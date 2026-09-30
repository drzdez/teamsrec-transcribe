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
  await waitFor(() => p.$("quit").disabled, "the page busy while the job runs");
  await waitFor(() => p.doc.querySelector(".card"), "the page reloading the new transcript by itself");
  await waitFor(() => p.$("status").textContent === "hotovo: přepis, titulky i zápis", "the result in the status line");
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
  p.$("saveSum").click();
  await waitFor(() => p.$("quit").disabled, "the page busy while the minutes are written");
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
