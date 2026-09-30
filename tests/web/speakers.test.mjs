// The Speakers tab: headings follow the name, known people are picked, names are saved on the server, two labels
// of one person are merged, and a noise speaker is removed (with the second click).
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_OLD, WEEKLY, openPage, server, waitFor } from "./page.mjs";

const cards = p => [...p.doc.querySelectorAll(".card")];
const heading = card => card.querySelector(".label").textContent;
const cardOf = (p, label) => cards(p).find(c => c.querySelector(".side").dataset.label === label);
const selectStartingWith = (card, text) => [...card.querySelectorAll("select")].find(s => s.options[0].textContent.startsWith(text));

test("naming speakers: heading follows the fields, a known person fills them, save writes the server", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => cards(p).length === 3, "three speaker cards");
  const clean = () => p.$("dirty").classList.contains("clean");
  assert.ok(clean(), "nothing changed yet");
  assert.match(p.doc.querySelector(".langLine").textContent, /Jazyk přepisu: čeština/);
  assert.equal(cardOf(p, "SPEAKER_00").querySelector(".chip.lang").textContent, "čeština");

  const s0 = cardOf(p, "SPEAKER_00");
  const first = s0.querySelector('[data-key="first"]');
  p.input(first, "Jana");
  assert.equal(heading(s0), "Jana", "the heading is what is typed");
  p.input(first, "");
  assert.equal(heading(s0), "SPEAKER_00", "empty fields fall back to the label");

  const s1 = cardOf(p, "SPEAKER_01");
  p.change(selectStartingWith(s1, "vybrat"), "petr-svoboda");
  const fields = ["first", "last", "nick"].map(k => s1.querySelector(`[data-key="${k}"]`).value);
  assert.deepEqual(fields, ["Petr", "Svoboda", "Péťa"]);
  assert.match(s1.querySelector(".shown").textContent, /v zápisu: Péťa/);
  assert.ok(p.$("dirty").classList.contains("changed"), "unsaved changes are shown");

  p.change(selectStartingWith(s0, "stejná"), "SPEAKER_01");  // SPEAKER_00 is the same person
  assert.equal(heading(s0), "Petr Svoboda");

  p.$("save").click();
  await waitFor(clean, "the save to finish");
  const r = await server(`/api/recordings/${WEEKLY}`);
  const byLabel = Object.fromEntries(r.speakers.map(s => [s.label, s]));
  assert.equal(byLabel.SPEAKER_01.person?.id, "petr-svoboda");
  assert.equal(byLabel.SPEAKER_00.person?.id, "petr-svoboda");

  // two labels of one person: the page offers the merge, the server does it
  const merge = await waitFor(() => p.button("Sloučit podle osoby"), "the merge button");
  merge.click();
  await waitFor(() => cards(p).length === 2, "the merged card list");
  assert.match(p.$("status").textContent, /sloučeno 1/);
  const after = await server(`/api/recordings/${WEEKLY}`);
  assert.equal(after.speakers.filter(s => s.person?.id === "petr-svoboda").length, 1);
});

test("removing a speaker's lines needs a second click", async t => {
  // the other recording of the fixture: the test above already changed this one's speakers
  const p = await openPage(t, BOARD_OLD);
  const jana = await waitFor(() => cardOf(p, "Jana Nováková"), "Jana's card");
  const count = cards(p).length;
  const rm = p.button("✕ smazat repliky", jana);
  rm.click();
  assert.ok(rm.classList.contains("armed"), "the first click only arms the button");
  assert.equal((await server(`/api/recordings/${BOARD_OLD}`)).speakers.length, count);
  rm.click();
  await waitFor(() => cards(p).length === count - 1, "the card to go");
  const r = await server(`/api/recordings/${BOARD_OLD}`);
  assert.ok(!r.speakers.some(s => s.label === "Jana Nováková"), "the server dropped the lines");
});
