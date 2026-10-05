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

test("presets: 1× first (one-off meetings), then repeating meetings by their newest recording; states below", async t => {
  const p = await openPage(t);
  const buttons = [...p.doc.querySelectorAll("#titles button")];
  const one = buttons[0];
  assert.equal(one.id, "singleBtn", "1× comes first");
  assert.match(one.textContent, /^1× \(\d+\)$/);
  assert.match(one.title, /jen jednu nahrávku/);
  // an earlier test renamed recordings; whatever repeats is a preset, never with a count of 1
  const meetings = buttons.slice(1).map(b => b.textContent);
  assert.ok(meetings.every(m => /\(\d+\)$/.test(m) && !/\(1\)$/.test(m)), meetings.join(" | "));
  const states = [...p.doc.querySelectorAll("#states button")].map(b => b.textContent.replace(/ \(\d+\)$/, ""));
  assert.deepEqual(states.filter(x => x !== "? nepojmenované"), ["◐ nezpracované", "○ bez přepisu"]);
  const pickText = () => p.$("pick").selectedOptions[0].textContent;
  [...p.doc.querySelectorAll("#states button")].find(b => b.textContent.startsWith("○ bez přepisu")).click();
  await waitFor(() => /bez přepisu/.test(pickText()), "a recording without a transcript");
  assert.ok([...p.$("pick").options].every(o => /bez přepisu|mimo filtr/.test(o.textContent)));
  p.$("singleBtn").click();
  assert.ok(p.$("singleBtn").classList.contains("on"), "1× combines with a state");
  assert.equal(p.$("states").querySelectorAll("button.on").length, 1);
  p.$("singleBtn").click();
  assert.ok(!p.$("singleBtn").classList.contains("on"), "a second click clears it");
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

test("one button next to the count clears every filter, the open recording stays", async t => {
  const p = await openPage(t);
  const clear = p.$("clearFilters");
  assert.ok(clear.hidden, "nothing to clear");
  p.input(p.$("filter"), "archi");
  [...p.doc.querySelectorAll("#states button")].find(b => b.textContent.startsWith("◐ nezpracované")).click();
  p.change(p.$("dateFrom"), "2026-09-01");
  assert.ok(!clear.hidden);
  const open = p.$("pick").value;
  clear.click();
  assert.equal(p.$("filter").value, "");
  assert.equal(p.$("dateFrom").value, "");
  assert.equal(p.doc.querySelectorAll("#filterRows button.on").length, 0, "no preset, state or time left on");
  assert.equal(p.$("filterCount").textContent, "");
  assert.ok(clear.hidden);
  assert.equal(p.$("pick").value, open, "what is open stays open");
});

test("typing a filter opens its first match", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the weekly recording");
  p.input(p.$("filter"), "archi");
  await waitFor(() => p.$("pick").value !== WEEKLY && /archi/i.test(p.$("pick").selectedOptions[0].textContent),
                "the first archi recording selected");
  const stem = p.$("pick").value;
  await waitFor(() => p.window.location.hash === "#" + stem, "and opened");
  assert.ok(!options(p).some(o => o.includes("mimo filtr")), "nothing left behind outside the filter");
  p.input(p.$("filter"), "");
});

test("a recording without a transcript offers the cloud-only run next to the local one", async t => {
  const p = await openPage(t, BOARD_NEW);
  const fast = await waitFor(() => p.doc.querySelector("#main button.cloud-btn"), "the cloud button");
  assert.match(fast.textContent, /co nejrychleji \(cloud\)/);
  assert.ok(fast.disabled ? /chybí klíč/.test(fast.title) : /do cloudu/.test(fast.title), "says why or what leaves the PC");
});
