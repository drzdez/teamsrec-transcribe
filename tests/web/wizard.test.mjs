// The setup wizard of the installed suite: it opens by itself once (no marker yet), recommends by the GPU, checks
// the Hugging Face token, keeps the keys in the vault, writes the settings and the marker, and comes back only from
// Nastavení. A file of its own: it is the only test that starts without the marker.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { openPage, waitFor } from "./page.mjs";

const CONFIG = process.env.TEAMSREC_TEST_CONFIG;
const main = p => p.$("wizardMain");
const option = (p, name, value) => p.doc.querySelector(`#wizardMain input[name="${name}"][value="${value}"]`);
const pickOption = (p, name, value) => {
  const r = option(p, name, value);
  r.checked = true;
  r.dispatchEvent(new p.window.Event("change", { bubbles: true }));
};
const next = p => p.$("wizardNext").click();

test("setup wizard: once by itself, recommends by the GPU, checks the HF token, writes the settings", async t => {
  const p = await openPage(t);
  await waitFor(() => !p.$("wizard").hidden && main(p).querySelector("input"), "the wizard opens by itself");
  assert.match(p.$("wizardSteps").textContent, /1\. Vítejte/);
  const name = main(p).querySelector('.setRow:nth-of-type(2) input');
  p.input(name, "Eva Malá");

  next(p);
  assert.match(main(p).textContent, /RTX 4070 Laptop GPU, 8 GB: lokální přepis zvládne/, "the recommendation");
  const times = [...main(p).querySelectorAll(".wizTimes tr")].map(tr => tr.textContent);
  assert.equal(times.length, 3, "a header, 15 min and 1 h");
  assert.match(times[2], /^1 h~\d+ min~\d+ min$/, "this PC and the fast track for an hour");
  assert.match(main(p).textContent, /odhad podle grafické karty/, "no runs measured in the test folder yet");
  assert.ok(option(p, "tprov", "whisperx").checked, "local transcription for this card");
  if (!option(p, "diar", "on").checked) pickOption(p, "diar", "on");
  const token = main(p).querySelector('input[type="password"]');
  p.input(token, "hf_bad");
  [...main(p).querySelectorAll("button")].find(b => b.textContent === "Ověřit").click();
  await waitFor(() => /Token neplatí/.test(main(p).textContent), "the bad token is refused");
  p.input(main(p).querySelector('input[type="password"]'), "hf_good");
  [...main(p).querySelectorAll("button")].find(b => b.textContent === "Ověřit").click();
  await waitFor(() => main(p).querySelector(".wizHf.ok"), "the good token passes");

  next(p);
  assert.match(main(p).textContent, /Ollama tu zatím není/);
  pickOption(p, "sprov", "anthropic");
  assert.ok(main(p).textContent.includes("Klíč Anthropic"));

  next(p);
  next(p);
  assert.match(main(p).textContent, /Vaše jméno: Eva Malá/);
  assert.match(main(p).textContent, /Zápis: Claude \(cloud\)/);
  assert.match(p.$("wizardNext").textContent, /Uložit a začít/);
  next(p);
  await waitFor(() => p.$("wizard").hidden, "the wizard closes");
  const toml = readFileSync(CONFIG, "utf-8");
  assert.match(toml, /name = "Eva Malá"/);
  assert.match(toml, /provider = "anthropic"/);
  assert.match(toml, /batch_size = 8\b/, "the recommended batch for 8 GB");
  assert.ok(existsSync(join(dirname(CONFIG), "setup-done")), "the marker: not shown again");

  p.$("settingsBtn").click();
  await waitFor(() => !p.$("settings").hidden, "the settings");
  p.$("wizardOpen").click();
  await waitFor(() => !p.$("wizard").hidden && p.$("settings").hidden, "the wizard again from Nastavení");
  p.$("wizardSkip").click();
  await waitFor(() => p.$("wizard").hidden, "skipped");
});
