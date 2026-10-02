// The job queue seen from a recording that waits in it: its place instead of the offer to transcribe it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_NEW, WEEKLY, openPage, server, waitFor } from "./page.mjs";

test("a recording waiting in the queue shows its place instead of the offer to transcribe it", async t => {
  const p = await openPage(t, BOARD_NEW);
  await waitFor(() => p.doc.querySelector("#main button.primary-btn"), "the offer to transcribe");
  const json = { method: "POST", headers: { "Content-Type": "application/json" } };
  await server(`/api/recordings/${WEEKLY}/process`, { ...json, body: JSON.stringify({ force: true }) });  // runs first
  await server(`/api/recordings/${BOARD_NEW}/process`, { ...json, body: "{}" });
  const main = p.doc.querySelector("#main");
  await waitFor(() => /1\. v pořadí/.test(main.textContent), "its place in the queue");
  assert.ok(!main.querySelector("button.primary-btn"), "no second offer to transcribe it");
  assert.match(main.textContent, new RegExp(`Teď běží ${WEEKLY}`));
  const option = stem => [...p.$("pick").options].find(o => o.value === stem).textContent;
  await waitFor(() => /⏳ ve frontě, 1\.\]/.test(option(BOARD_NEW)), "the list says it waits");
  assert.match(option(WEEKLY), /⏳ zpracovává se\]/, "and which one runs");
  const queued = [...p.doc.querySelectorAll("#states button")].find(b => b.textContent.startsWith("⏳ zpracovává se nebo ve frontě"));
  assert.ok(queued, "a state filter for them");
  assert.match(queued.textContent, /\(2\)$/);
  await waitFor(() => p.doc.querySelector(".card"), "its transcript once both jobs are done");
});

