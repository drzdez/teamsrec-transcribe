// Transcript and minutes tabs, the Markdown renderer (only safe links), and the help over the page.
import assert from "node:assert/strict";
import { test } from "node:test";
import { WEEKLY, openPage, waitFor } from "./page.mjs";

test("the transcript is shown as text and the minutes as Markdown with safe links only", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "speaker cards");
  const dm = p.$("docMain");

  p.doc.querySelector('#subtabs button[data-sub="transcript"]').click();
  await waitFor(() => dm.textContent.includes("Rozpočet je hotový"), "the transcript");
  assert.ok(dm.innerHTML.includes("&lt;b&gt;programem"), "markup in what people said stays text");
  assert.equal(dm.querySelector("b"), null);
  assert.ok(p.$("main").hidden, "the speakers list is hidden while reading");

  p.doc.querySelector('#subtabs button[data-sub="summary"]').click();
  await waitFor(() => dm.querySelector("h2"), "the minutes");
  assert.deepEqual([...dm.querySelectorAll("h2")].map(h => h.textContent), ["Shrnutí", "Úkoly", "Mluvčí"]);
  assert.ok(dm.querySelector("strong") && dm.querySelector("code") && dm.querySelector(".stamp"));
  assert.equal(dm.querySelector("table tbody tr td").textContent, "Petr");
  const links = [...dm.querySelectorAll("a")].map(a => a.getAttribute("href"));
  assert.deepEqual(links, ["https://example.org/plan"], "javascript: and data: never become links");
  assert.ok(dm.textContent.includes("klikni sem") && dm.textContent.includes("data"), "their text stays readable");

  p.doc.querySelector('#subtabs button[data-sub="speakers"]').click();
  assert.ok(!p.$("main").hidden && dm.hidden);
});

test("help opens the guides from docs/, links between them stay inside, Esc closes", async t => {
  const p = await openPage(t, WEEKLY);
  const box = p.$("help"), main = p.$("helpMain");
  assert.ok(box.hidden);
  p.$("helpBtn").click();
  await waitFor(() => main.querySelector("h1"), "the user guide");
  assert.ok(!box.hidden);
  assert.equal(p.$("helpPick").value, "user-guide");
  const local = [...main.querySelectorAll("a")].find(a => /(install|privacy)\.md/.test(a.getAttribute("href") || ""));
  assert.ok(local, "the user guide links another guide");
  const target = local.getAttribute("href").match(/(install|privacy)\.md/)[1];
  const before = main.querySelector("h1").textContent;
  local.click();
  await waitFor(() => p.$("helpPick").value === target && main.querySelector("h1")?.textContent !== before,
                "the linked guide, shown in the help");
  for (const a of main.querySelectorAll('a[href^="http"]')) assert.equal(a.target, "_blank", "web links open outside");
  p.doc.dispatchEvent(new p.window.KeyboardEvent("keydown", { key: "Escape" }));
  assert.ok(box.hidden);
});

test("each sub-tab keeps its scroll position: back from the minutes to the speakers without scrolling up", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "the speakers");
  const sc = p.doc.scrollingElement || p.doc.documentElement;
  sc.scrollTop = 120;  // somewhere in the speakers
  p.doc.querySelector('#subtabs button[data-sub="summary"]').click();
  await waitFor(() => p.$("docMain").querySelector("h2"), "the minutes");
  assert.equal(sc.scrollTop, 0, "the minutes open at their top");
  sc.scrollTop = 900;  // far down in the minutes
  p.doc.querySelector('#subtabs button[data-sub="speakers"]').click();
  assert.equal(sc.scrollTop, 120, "back where the speakers were");
  p.doc.querySelector('#subtabs button[data-sub="summary"]').click();
  await waitFor(() => sc.scrollTop === 900, "and back down in the minutes");
  assert.ok(p.doc.documentElement.style.getPropertyValue("--hdr"), "the sticky tabs know the header's height");
});
