// A transcribed recording queued or processed again: a banner says what is shown and what will be replaced,
// saving is off while it runs (own file: the jobs change the server's recordings).
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_OLD, WEEKLY, openPage, server, waitFor } from "./page.mjs";

test("queued: a slim pinned line, the page as it was; running: the page empties like a new recording", async t => {
  const p = await openPage(t, BOARD_OLD);
  await waitFor(() => p.doc.querySelector(".card"), "the speakers of the old board recording");
  const json = { method: "POST", headers: { "Content-Type": "application/json" } };
  await server(`/api/recordings/${WEEKLY}/process`, { ...json, body: JSON.stringify({ force: true }) });     // runs 2 s
  await server(`/api/recordings/${BOARD_OLD}/process`, { ...json, body: JSON.stringify({ force: true }) });  // waits
  const banner = () => p.doc.getElementById("queueBanner");
  await waitFor(() => banner() && /ve frontě 1\./.test(banner().textContent), "queued");
  assert.ok(p.$("subtabs").contains(banner()), "pinned with the sticky sub-tabs");
  assert.match(banner().textContent, /nový přepis od nuly/);
  assert.ok(p.doc.querySelector(".card"), "the current state stays readable");
  assert.ok(!p.$("save").disabled, "and editable");
  assert.ok(p.button("Zrušit zpracování", banner()), "the queue buttons are there too");
  await waitFor(() => p.doc.querySelector("#main [data-running]"), "running: the page empties", 6000);
  assert.ok(!p.doc.querySelector(".card") && !banner(), "no old cards, no banner");
  assert.match(p.$("main").textContent, /se právě zpracovává znovu/);
  assert.ok(p.$("save").disabled && p.$("saveSum").disabled, "no saving while it runs");
  await waitFor(() => p.doc.querySelector(".card"), "done: the new transcript", 8000);
  assert.ok(!p.$("save").disabled, "and saving is back");
});
