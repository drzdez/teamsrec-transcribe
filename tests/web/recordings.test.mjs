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
  const todo = [...p.doc.querySelectorAll("#states button")].find(b => b.textContent.startsWith("◐ nezpracované"));
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

test("presets for repeating meetings by their newest recording; processing states and one-offs in a second row", async t => {
  const p = await openPage(t);
  const meetings = [...p.doc.querySelectorAll("#titles button")].map(b => b.textContent);
  // an earlier test renamed recordings; whatever repeats is a preset, newest meeting first
  assert.ok(meetings.every(m => /\(\d+\)$/.test(m) && !/\(1\)$/.test(m)), meetings.join(" | "));
  assert.equal(p.$("titles").hidden, meetings.length === 0, "no repeating meeting: no presets row");
  const states = [...p.doc.querySelectorAll("#states button")].map(b => b.textContent.replace(/ \(\d+\)$/, ""));
  assert.deepEqual(states.filter(x => x !== "? nepojmenované"), ["◐ nezpracované", "○ bez přepisu", "1× neopakující se"]);
  const pickText = () => p.$("pick").selectedOptions[0].textContent;
  [...p.doc.querySelectorAll("#states button")].find(b => b.textContent.startsWith("○ bez přepisu")).click();
  await waitFor(() => /bez přepisu/.test(pickText()), "a recording without a transcript");
  assert.ok([...p.$("pick").options].every(o => /bez přepisu|mimo filtr/.test(o.textContent)));
  [...p.doc.querySelectorAll("#states button")].find(b => b.textContent.startsWith("1×")).click();
  const counts = [...p.$("pick").options].filter(o => !o.textContent.includes("mimo filtr")).map(o => o.textContent);
  assert.ok(counts.length >= 1, "one-off meetings listed");
  assert.equal(p.$("states").querySelectorAll("button.on").length, 1, "one state at a time");
  assert.equal(p.window.getComputedStyle(p.$("titles")).flexWrap, "nowrap", "one row that scrolls sideways");
});

test("the text filter finds dates as the list shows them, in Czech, and as a regexp", async t => {
  const p = await openPage(t);
  const filter = p.$("filter");
  for (const q of ["2026-09-03", "2026-09-03 09:00", "3.9.2026", "2026-09-0[3]"]) {
    p.input(filter, q);
    assert.equal(p.$("filterCount").textContent, "1 z 3", q);
  }
  p.input(filter, "2026-09-0[34]");
  assert.equal(p.$("filterCount").textContent, "2 z 3");
  p.input(filter, "");
});

test("a third row filters by when: ranges fill od/do, typed dates work, a second click clears", async t => {
  const p = await openPage(t);
  const btn = key => p.doc.querySelector(`#dateBtns button[data-key="${key}"]`);
  assert.deepEqual([...p.doc.querySelectorAll("#dateBtns button")].map(b => b.dataset.key),
                   ["today", "yesterday", "week", "lastweek", "month", "lastmonth", "older"]);
  assert.match(btn("lastweek").title, /^minulý týden: \d{4}-\d{2}-\d{2} – \d{4}-\d{2}-\d{2}/, "the full name in the tooltip");
  btn("today").click();
  assert.equal(p.$("dateFrom").value, p.$("dateTo").value, "today is one day");
  assert.ok(btn("today").classList.contains("on"));
  btn("today").click();
  assert.equal(p.$("dateFrom").value, "", "a second click clears it");
  assert.equal(p.$("filterCount").textContent, "");
  p.change(p.$("dateFrom"), "2026-09-03");
  p.change(p.$("dateTo"), "2026-09-04");
  assert.equal(p.$("filterCount").textContent, "2 z 3", "od–do inclusive");
  assert.ok(![...p.doc.querySelectorAll("#dateBtns button")].some(b => b.classList.contains("on")), "typed: no button");
  p.change(p.$("dateFrom"), "");
  p.change(p.$("dateTo"), "");
  assert.equal(p.$("filterCount").textContent, "");
});
