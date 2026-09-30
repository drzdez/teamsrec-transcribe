// The same page in the desktop window (?app=desktop, desktop/ Tauri shell): what differs, and nothing else.
import assert from "node:assert/strict";
import { test } from "node:test";
import { WEEKLY, openPage, waitFor } from "./page.mjs";

async function minutesLinks(p) {
  await waitFor(() => p.doc.querySelector(".card"), "the speakers");
  p.doc.querySelector('#subtabs button[data-sub="summary"]').click();
  await waitFor(() => p.$("docMain").querySelector("a"), "the minutes with a link");
  return p.$("docMain").querySelector('a[href^="https:"]');
}

test("in the browser: Zavřít is there, links in the minutes open a new tab", async t => {
  const p = await openPage(t, WEEKLY);
  assert.ok(!p.$("quit").hidden);
  assert.ok(!p.doc.documentElement.classList.contains("desktop"));
  const a = await minutesLinks(p);
  assert.equal(a.getAttribute("target"), "_blank");
});

test("in the desktop window: no Zavřít (closing the window stops the server), links are left to the window", async t => {
  const p = await openPage(t, WEEKLY, { desktop: true });
  assert.ok(p.doc.documentElement.classList.contains("desktop"));
  assert.ok(p.$("quit").hidden);
  const a = await minutesLinks(p);
  assert.equal(a.getAttribute("target"), null, "the window opens it in the default browser");
  assert.equal(p.$("pick").value, WEEKLY, "everything else is the same page: the recording opened by its hash");
});
