// The recordings dropdown: "date time – title [state]", the text/regexp filter and the clickable presets.
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_NEW, BOARD_OLD, WEEKLY, openPage, waitFor } from "./page.mjs";

const options = p => [...p.$("pick").options].map(o => o.textContent);
const matched = p => options(p).filter(t => !t.includes("mimo filtr"));  // the open recording stays, marked
const chip = (p, name) => [...p.doc.querySelectorAll("#titles button")].find(b => b.textContent.startsWith(name));

test("each recording says how far it got", async t => {
  const p = await openPage(t);
  const texts = options(p);
  assert.equal(texts.length, 3);
  assert.match(texts[0], /^2026-09-04 14:00 – Týdenní sync \[.*nepojmenovan.*\]$/);
  assert.match(texts[1], /^2026-09-03 09:00 – Archi board \[○ bez přepisu\]$/);
  assert.equal(p.$("pick").value, WEEKLY, "the newest one opens");
});

test("the filter takes text or a regular expression, folds case and diacritics, marks a broken one", async t => {
  const p = await openPage(t);
  const filter = p.$("filter");
  p.input(filter, "archi");
  assert.equal(matched(p).length, 2);
  assert.equal(p.$("filterCount").textContent, "2 z 3");
  p.input(filter, "^tydenni");  // a regexp, without the diacritics of "Týdenní"
  assert.equal(matched(p).length, 1);
  assert.ok(matched(p)[0].includes("Týdenní sync"));
  p.input(filter, "(");
  assert.ok(filter.classList.contains("bad"), "a broken expression is visible");
  p.input(filter, "");
  assert.equal(options(p).length, 3);
});

test("a preset shows one meeting and opens its newest recording; a second click clears it", async t => {
  const p = await openPage(t);
  assert.ok(p.doc.querySelector("#titles .presetLabel"), "the row says these are presets");
  const board = chip(p, "Archi board");
  assert.equal(board.textContent, "Archi board (2)");
  board.click();
  await waitFor(() => p.$("pick").value === BOARD_NEW, "the newest board recording");
  assert.equal(matched(p).length, 2);
  assert.ok(!options(p).some(o => o.includes("mimo filtr")), "nothing stays behind from the old selection");
  assert.ok(chip(p, "Archi board").classList.contains("on"));
  await waitFor(() => p.doc.querySelector("#main button.primary-btn"), "the offer to transcribe it");
  chip(p, "Archi board").click();
  assert.equal(p.$("filter").value, "");
  assert.equal(options(p).length, 3);
  assert.ok([...p.$("pick").options].some(o => o.value === BOARD_OLD));
});

test("the unfinished preset counts and picks what still needs work", async t => {
  const p = await openPage(t);
  const todo = [...p.doc.querySelectorAll("#titles button")].find(b => b.textContent.startsWith("◐ nezpracované"));
  assert.ok(todo, "the preset exists");
  assert.equal(todo.textContent, "◐ nezpracované (3)", "unnamed speakers and a missing transcript both count");
  todo.click();
  assert.equal(p.$("filterCount").textContent, "3 z 3");
});

test("a recording without a transcript can be renamed right after recording", async t => {
  const p = await openPage(t, BOARD_NEW);
  await waitFor(() => p.doc.querySelector("#main button.primary-btn"), "the offer to transcribe");
  p.input(p.$("title"), "Plánování sprintu");
  assert.match(p.$("dirty").textContent, /neuložený název/);
  p.$("save").click();
  await waitFor(() => /název uložen: Plánování sprintu/.test(p.$("status").textContent), "the rename");
  assert.equal(p.$("pick").value, "2026-09-03_0900_planovani-sprintu", "the folder and files follow the title");
  assert.match([...p.$("pick").options].find(o => o.selected).textContent, /Plánování sprintu \[○ bez přepisu\]/);
  assert.equal(p.$("dirty").textContent, "");
  assert.ok(p.doc.querySelector("#main button.primary-btn"), "it can still be transcribed");
});

test("a new #stem from outside (a click on the Saved balloon) switches the open page to that recording", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the first recording");
  p.window.location.hash = BOARD_OLD;
  p.window.dispatchEvent(new p.window.HashChangeEvent("hashchange"));
  await waitFor(() => p.$("pick").value === BOARD_OLD && p.$("title").value === "Archi board", "the other recording");
});

test("the list looks again when the window comes back, and a lost server is said out loud", async t => {
  const p = await openPage(t);
  const lists = () => p.requests.filter(r => r.startsWith("GET /api/recordings?")).length;
  const before = lists();
  Object.defineProperty(p.doc, "visibilityState", { value: "visible", configurable: true });
  p.doc.dispatchEvent(new p.window.Event("visibilitychange"));
  await waitFor(() => lists() > before, "a fresh recordings list");

  const offline = p.$("offline");
  assert.ok(offline.hidden);
  p.window.setOnline(false);
  assert.ok(offline.hidden, "one missed answer is not yet an outage");
  p.window.setOnline(false);
  assert.ok(!offline.hidden, "two are");
  assert.match(offline.textContent, /přerušené/);
  const now = lists();
  p.window.setOnline(true);
  assert.ok(offline.hidden);
  await waitFor(() => lists() > now, "back online: what was missed comes in");
});
