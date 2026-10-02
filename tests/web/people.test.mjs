// The People tab: the list of known people, a person's detail and the way back.
import assert from "node:assert/strict";
import { test } from "node:test";
import { openPage, waitFor } from "./page.mjs";

test("people list, detail and back", async t => {
  const p = await openPage(t);
  p.$("tabPeople").click();
  const pm = p.$("peopleMain");
  await waitFor(() => pm.querySelectorAll("tbody tr").length === 2, "two people");
  assert.ok(pm.querySelector(".people-help"));
  const petrRow = [...pm.querySelectorAll("tbody tr")].find(r => [...r.querySelectorAll("input")].some(i => i.value === "Petr"));
  assert.ok(petrRow, "Petr is listed");
  assert.ok([...pm.querySelectorAll("tbody tr input")].some(i => i.value === "Jana"), "Jana is listed");
  const detail = [...petrRow.querySelectorAll("button.link")].find(b => b.textContent.includes("detail"));
  assert.ok(detail, "each person has a detail link");
  detail.click();
  await waitFor(() => pm.querySelector(".person-h"), "Petr's detail");
  assert.match(pm.querySelector(".person-h").textContent, /Petr|Péťa/);
  const back = [...pm.querySelectorAll("button.link")].find(b => b.textContent.startsWith("←"));
  back.click();
  await waitFor(() => pm.querySelectorAll("tbody tr").length === 2 && !pm.querySelector(".person-h"), "the list again");
});

test("the recording filters are not on the People tab, and come back with Schůzka", async t => {
  const p = await openPage(t);
  p.$("tabPeople").click();
  await waitFor(() => !p.$("peopleMain").hidden, "the people list");
  assert.ok(p.$("filterRows").hidden && p.$("filter").hidden, "no filters for the shared people list");
  p.$("tabSpeakers").click();
  assert.ok(!p.$("filterRows").hidden && !p.$("filter").hidden);
  assert.ok(p.$("peopleMain").hidden);
});
