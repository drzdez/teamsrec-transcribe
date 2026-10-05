// "Projít repliky" on a speaker card: every reply, each can move to another speaker or a new one (own file: it
// changes the transcript).
import assert from "node:assert/strict";
import { test } from "node:test";
import { WEEKLY, openPage, waitFor } from "./page.mjs";

test("a reply that someone else said moves to a new speaker; the rest stays", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelectorAll(".card").length === 3, "the speakers");
  const card = [...p.doc.querySelectorAll(".card")].find(c => c.querySelector(".side")?.dataset.label === "SPEAKER_00");
  const walk = card.querySelector("details.walk");
  assert.match(walk.querySelector("summary").textContent, /Projít repliky \(2\)/);
  walk.open = true;
  walk.dispatchEvent(new p.window.Event("toggle"));
  await waitFor(() => walk.querySelectorAll(".reply").length === 2, "its replies, loaded when opened");
  assert.ok(p.requests.some(r => r.endsWith("/speakers/SPEAKER_00/replies")));
  const sel = walk.querySelectorAll(".reply select")[1];
  assert.ok([...sel.options].some(o => o.textContent === "→ nový mluvčí"));
  p.change(sel, "@new");
  assert.ok(walk.querySelectorAll(".reply")[1].classList.contains("moved"));
  p.button("Uložit přesuny", walk).click();
  await waitFor(() => /přesunuto 1 replik/.test(p.$("status").textContent), "moved");
  await waitFor(() => p.doc.querySelectorAll(".card").length === 4, "a new speaker card");
});
