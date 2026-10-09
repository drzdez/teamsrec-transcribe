// Lidé → osoba → Všechny repliky: nothing is computed until the user confirms the GPU job; then every reply of the
// person is listed, playable, sortable. A file of its own: it writes the replies' voices.
import assert from "node:assert/strict";
import { test } from "node:test";
import { openPage, waitFor } from "./page.mjs";

test("all replies: the GPU job only after a confirmation, then every reply of the person", async t => {
  const p = await openPage(t);
  p.$("tabPeople").click();
  await waitFor(() => [...p.doc.querySelectorAll("#peopleMain button.link")].some(b => b.textContent === "detail"), "the people");
  const row = [...p.doc.querySelectorAll("#peopleMain tr")].find(tr => [...tr.querySelectorAll("input")].some(i => i.value === "Nováková"));
  [...row.querySelectorAll("button.link")].find(b => b.textContent === "detail").click();
  await waitFor(() => p.doc.querySelector("#peopleMain .personTabs"), "Jana's page");
  assert.match(p.doc.querySelector("#peopleMain .personTabs").textContent, /Vzorky pro rozpoznávání \(0\/20\)/);
  [...p.doc.querySelectorAll("#peopleMain .personTabs button")].find(b => b.textContent === "Všechny repliky").click();
  const go = await waitFor(() => [...p.doc.querySelectorAll("#peopleMain button")].find(b => b.textContent.startsWith("Vyhodnotit repliky")), "the offer");
  assert.match(p.doc.querySelector("#peopleMain .repliesBox").textContent, /Vyhodnoceno 0 z 2 schůzek/);
  go.click();
  assert.match(go.textContent, /Potvrdit: vytížit grafickou kartu/, "asks first: it loads the GPU");
  go.click();
  await waitFor(() => p.doc.querySelector("#peopleMain table.repliesTable"), "the replies, after the job");
  const rows = [...p.doc.querySelectorAll("table.repliesTable tbody tr")];
  assert.equal(rows.length, 2, "Jana's replies in both meetings");
  assert.ok(rows.every(r => r.querySelector("button.play")), "each one can be played");
  assert.match(p.doc.querySelector("#peopleMain .repliesBox").textContent, /Vyhodnoceno 2 z 2 schůzek/);
});
