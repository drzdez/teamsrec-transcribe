// A person's guessed groups ("Ke kontrole"): going to a meeting and back, and a confirmation that must not put the
// open meeting's cards under the Lidé tab (2026-10-09). A file of its own: it confirms an assignment.
import assert from "node:assert/strict";
import { test } from "node:test";
import { WEEKLY, openPage, waitFor } from "./page.mjs";

async function openJana(p) {
  p.$("tabPeople").click();
  await waitFor(() => [...p.doc.querySelectorAll("#peopleMain button.link")].some(b => b.textContent === "detail"), "the people");
  const row = [...p.doc.querySelectorAll("#peopleMain tr")].find(tr => [...tr.querySelectorAll("input")].some(i => i.value === "Nováková"));
  [...row.querySelectorAll("button.link")].find(b => b.textContent === "detail").click();
  await waitFor(() => p.doc.querySelector("#peopleMain .person-h3"), "Jana's page");
  await waitFor(() => p.doc.querySelector("#peopleMain .personTabs"), "Jana's page");
  [...p.doc.querySelectorAll("#peopleMain .personTabs button")].find(b => b.textContent.startsWith("Ve schůzkách")).click();
  await waitFor(() => p.doc.querySelector("#peopleMain table.assignments"), "Jana's guessed groups");
}

test("person page: to the meeting at the speaker's card and back; a confirmation keeps the Lidé view", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the open meeting");
  await openJana(p);
  assert.match(p.doc.querySelector("#peopleMain").textContent, /· 2 ke kontrole/);

  // the meeting that is already open: a plain #stem link did nothing here
  const row = [...p.doc.querySelectorAll("table.candidates tr")].find(tr => tr.textContent.includes("Týdenní"));
  row.querySelector("a.openRec").click();
  await waitFor(() => !p.$("pick").hidden && p.$("tabSpeakers").classList.contains("active"), "the Schůzka tab");
  assert.ok(p.$("peopleMain").hidden && !p.$("main").hidden, "the meeting, not the people");
  assert.ok(p.doc.querySelector('.card.flash .side[data-label="Jana Nováková"]'), "Jana's card is pointed at");
  assert.match(p.$("backToPerson").textContent, /zpět na Jana Nováková/);
  p.$("backToPerson").click();
  await waitFor(() => p.doc.querySelector("#peopleMain table.assignments"), "back on Jana's page (the same view)");
  assert.ok(p.$("backToPerson").hidden && p.$("main").hidden);

  // ✓ Potvrdit on the meeting that is open under Schůzka: the server announces it changed; the page stays here
  const weekly = [...p.doc.querySelectorAll("table.candidates tr")].find(tr => tr.textContent.includes("Týdenní"));
  weekly.querySelector("button.candYes").click();
  await waitFor(() => /potvrzeno/.test(p.$("status").textContent), "the confirmation");
  await waitFor(() => /· 1 ke kontrole/.test(p.doc.querySelector("#peopleMain").textContent), "one left to review");
  await new Promise(r => setTimeout(r, 300));  // the server's event arrives
  assert.ok(p.$("main").hidden && p.$("pick").hidden, "no meeting cards under the Lidé tab");
  p.$("tabSpeakers").click();
  await waitFor(() => !p.$("main").hidden && p.doc.querySelector(".card"), "the meeting, reloaded on the way back");
});
