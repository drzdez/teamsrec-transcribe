// The title's own Uložit: renames the meeting only; unsaved names on the cards stay unsaved (own file: it renames).
import assert from "node:assert/strict";
import { test } from "node:test";
import { WEEKLY, openPage, waitFor } from "./page.mjs";

test("the button next to the title saves only the title", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelectorAll(".card").length === 3, "the speakers");
  const btn = p.$("saveTitle");
  assert.ok(btn.disabled, "nothing to save yet");
  p.input(p.doc.querySelector('.side [data-key="nick"]'), "Kája");  // an unsaved name
  p.input(p.$("title"), "Týdenní sync týmu");
  assert.ok(!btn.disabled);
  btn.click();
  await waitFor(() => /název uložen/.test(p.$("status").textContent), "the title saved");
  assert.ok(p.requests.some(r => /^PUT \/api\/recordings\/[^/]+\/title$/.test(r)), "its own request");
  assert.ok(!p.requests.some(r => r.includes("/names")), "the names were not saved");
  assert.ok(btn.disabled, "saved: nothing more to save");
  assert.match(p.$("pick").selectedOptions[0].textContent, /Týdenní sync týmu/, "the list has the new title");
  assert.equal(p.doc.querySelector('.side [data-key="nick"]').value, "Kája", "the card keeps what was typed");
  assert.ok(p.$("dirty").classList.contains("changed"), "the name is still unsaved");
  assert.match(p.window.location.hash, /tydenni-sync-tymu/, "the address follows the renamed recording");
});
