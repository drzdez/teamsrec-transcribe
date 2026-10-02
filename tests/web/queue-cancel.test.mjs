// Cancelling processing from the page (own file: the server's recordings change state as jobs finish).
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_NEW, WEEKLY, openPage, server, waitFor } from "./page.mjs";

test("cancelling: a waiting recording leaves the queue at once, the running one asks first", async t => {
  const p = await openPage(t, BOARD_NEW);
  const json = { method: "POST", headers: { "Content-Type": "application/json" } };
  await server(`/api/recordings/${WEEKLY}/process`, { ...json, body: JSON.stringify({ force: true }) });   // runs (2 s)
  await server(`/api/recordings/${BOARD_NEW}/process`, { ...json, body: "{}" });
  const main = p.doc.querySelector("#main");
  await waitFor(() => /1\. v pořadí/.test(main.textContent), "in the queue");
  p.button("Zrušit zpracování", main).click();
  await waitFor(() => main.querySelector("button.primary-btn"), "out of the queue: the offer to transcribe it again");
  assert.ok(p.requests.some(r => /^POST \/api\/jobs\/\d+\/cancel$/.test(r)), "no confirmation for a waiting one");
  assert.match(p.$("status").textContent, /vyřazeno z fronty/);
});
