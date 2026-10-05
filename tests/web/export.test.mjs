// "Uložit jako…" in the Zápis tab: a copy of the minutes into a folder remembered by the meeting name.
import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { WEEKLY, openPage, waitFor } from "./page.mjs";

test("save a copy, and the next time the same folder is offered", async t => {
  const folder = mkdtempSync(join(tmpdir(), "teamsrec-export-"));
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "speakers");
  p.doc.querySelector('#subtabs [data-sub="summary"]').click();
  const exp = await waitFor(() => p.$("sumExport"), "the Uložit jako button");
  p.window.fetch = (orig => (url, init) => String(url).includes("pick-folder")  // no Windows dialog in a test
    ? Promise.resolve(new Response(JSON.stringify({ folder }), { headers: { "Content-Type": "application/json" } }))
    : orig(url, init))(p.window.fetch);
  exp.click();
  await waitFor(() => p.$("exportFolder") && p.$("exportFolder").value === folder, "first time: the folder dialog");
  p.button("Uložit", p.$("exportBox")).click();
  await waitFor(() => /kopie (zápisu )?uložena/.test(p.$("status").textContent), "saved").catch(e => { throw new Error(e.message + " – status: " + p.$("status").textContent); });
  assert.equal(readdirSync(folder).length, 1);
  assert.match(readdirSync(folder)[0], /^\d{4}-\d{2}-\d{2} \d{4} /, "date and time first, to sort");
  const again = await waitFor(() => p.$("sumExportAgain"), "Uložit jako minule");
  assert.match(again.title, new RegExp(folder.replace(/\\/g, "\\\\")));
  again.click();
  await waitFor(() => readdirSync(folder).length === 2, "one click: a second copy in the same folder");
  p.$("sumExport").click();
  await waitFor(() => p.$("exportFolder") && p.$("exportFolder").value === folder, "the remembered folder offered");
});
