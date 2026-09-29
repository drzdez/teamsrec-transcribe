// A background job seen through the server's event stream: started, finished, the page reloads on its own,
// and the history lists what happened, newest first.
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_NEW, WEEKLY, openPage, waitFor } from "./page.mjs";

test("transcribing a new recording: live status over SSE, reload when done, history", async t => {
  const p = await openPage(t, BOARD_NEW);
  const go = await waitFor(() => p.doc.querySelector("#main button.primary-btn"), "the offer to transcribe");
  assert.match(p.doc.querySelector("#main").textContent, /ještě nemá přepis/);
  go.click();
  await waitFor(() => p.$("quit").disabled, "the page busy while the job runs");
  await waitFor(() => p.doc.querySelector(".card"), "the page reloading the new transcript by itself");
  await waitFor(() => !p.$("quit").disabled, "the page not busy any more");
  assert.equal(p.$("status").textContent, "hotovo: přepis, titulky i zápis", "the result stays in the status line");
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
  await waitFor(() => !p.$("quit").disabled, "the minutes done");
  assert.ok(/zápis přegenerován/.test(p.$("status").textContent), p.$("status").textContent);
  assert.ok(!p.requests.some(r => r.startsWith("GET /api/status")), "no polling while the stream works");
});
