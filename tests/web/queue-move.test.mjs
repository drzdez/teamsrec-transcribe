// Moving a waiting recording forward in the job queue: "jako další" and "hned" (own file: the server's
// recordings change state as jobs finish).
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_NEW, BOARD_OLD, WEEKLY, openPage, server, waitFor } from "./page.mjs";

test("a waiting recording can go next, or right now (asks first: the running job starts over)", async t => {
  const p = await openPage(t, BOARD_NEW);
  const json = { method: "POST", headers: { "Content-Type": "application/json" } };
  await server(`/api/recordings/${WEEKLY}/process`, { ...json, body: JSON.stringify({ force: true }) });   // runs (2 s)
  await server(`/api/recordings/${BOARD_OLD}/process`, { ...json, body: JSON.stringify({ force: true }) }); // waits, 1st
  await server(`/api/recordings/${BOARD_NEW}/process`, { ...json, body: "{}" });                           // waits, 2nd
  const main = p.doc.querySelector("#main");
  await waitFor(() => /2\. v pořadí/.test(main.textContent), "second in the queue");
  const now = p.button("Zpracovat hned", main);
  assert.ok(now, "it can jump the queue");
  p.button("Zpracovat jako další", main).click();
  await waitFor(() => /1\. v pořadí/.test(main.textContent), "first after the move");
  assert.ok(p.requests.some(r => /^POST \/api\/jobs\/\d+\/next$/.test(r)));
  assert.ok(!p.button("Zpracovat jako další", main), "already next");
  const now2 = p.button("Zpracovat hned", main);
  now2.click();
  assert.ok(!p.requests.some(r => r.endsWith("/now")), "the first click only asks");
  await waitFor(() => p.doc.querySelector(".card"), "its transcript in the end");
});

