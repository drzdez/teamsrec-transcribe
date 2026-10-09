// The settings panel: fields of the shared teamsrec.toml by section, saving only what changed, a warning when a
// cloud service has no key, and API keys that go into the vault and never come back to the page.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { openPage, server, waitFor } from "./page.mjs";

const CONFIG = process.env.TEAMSREC_TEST_CONFIG;  // the throw-away teamsrec.toml of this run
const field = (p, key) => p.doc.querySelector(`#settingsMain [data-type][data-key="${key}"]`);
const section = (p, id) => p.doc.querySelector(`#settingsMain [data-section="${id}"]`);
const keyRow = (p, name) => p.doc.querySelector(`#settingsMain [data-secret="${name}"]`);

async function openSettings(p) {
  p.$("settingsBtn").click();
  await waitFor(() => field(p, "transcribe.provider"), "the settings");
}

test("settings: sections, values from the file, only changes are saved, comments stay", async t => {
  const p = await openPage(t);
  await openSettings(p);
  assert.ok(!p.$("settings").hidden);
  const titles = [...p.doc.querySelectorAll("#settingsMain .setSec h2")].map(h => h.textContent);
  assert.deepEqual(titles, ["Kde se co zpracovává", "Klíče API", "Obecné", "Nahrávání", "Přepis", "Video oken Teams",
                            "Zápis", "Hlasové otisky", "Uchování"]);
  const places = [...p.doc.querySelectorAll(".places tr")].map(tr => tr.textContent);
  assert.ok(places.some(t => /Přepis řeči.*lokálně/.test(t)), "where the transcription runs");
  assert.ok(places.some(t => /Rychle přes cloud.*cloud/.test(t)), "the fast track is always listed, as cloud");
  assert.equal(field(p, "user.name").value, "Jan Novák");
  assert.equal(field(p, "transcribe.glossary").value, "WFMS\nNOTAM");
  assert.equal(field(p, "transcribe.diarize").checked, true, "a default when the file does not say");
  assert.ok(field(p, "capture.onsite_mic").classList.contains("combo"), "the mic field is a dropdown of the inputs");
  assert.ok(p.$("settingsSave").disabled, "nothing to save yet");

  field(p, "transcribe.diarize").checked = false;
  field(p, "transcribe.diarize").dispatchEvent(new p.window.Event("change"));
  p.input(field(p, "transcribe.glossary"), "WFMS\nNOTAM\nBPMN");
  p.input(field(p, "voiceprints.threshold"), "0,6");
  assert.ok(!p.$("settingsSave").disabled);
  assert.equal(p.doc.querySelectorAll("#settingsMain .setRow.changed").length, 3);

  p.$("settingsSave").click();
  // the save answers with the whole settings again (models, devices): on GitHub's runner that took over 4 s
  // three times (2026-10-06, -08, -09), so this one waits longer
  await waitFor(() => /nastavení uloženo \(3\)/.test(p.$("status").textContent), "the save", 15000);
  assert.equal(p.doc.querySelectorAll("#settingsMain .setRow.changed").length, 0);
  const text = readFileSync(CONFIG, "utf8");
  assert.match(text, /glossary = \["WFMS", "NOTAM", "BPMN"\]/);
  assert.match(text, /diarize = false/);
  assert.match(text, /threshold = 0\.6/);
  assert.match(text, /name = "Jan Novák"  # your name/, "untouched lines keep their comments");
});

test("settings: a bad value is refused with a message and nothing is written", async t => {
  const p = await openPage(t);
  await openSettings(p);
  const before = readFileSync(CONFIG, "utf8");
  p.input(field(p, "transcribe.batch_size"), "osm");
  p.$("settingsSave").click();
  await waitFor(() => p.$("status").classList.contains("err"), "the error");
  assert.match(p.$("status").textContent, /není číslo/);
  assert.equal(readFileSync(CONFIG, "utf8"), before);
  assert.ok(p.doc.querySelector('#settingsMain .setRow.changed[data-key="transcribe.batch_size"]'), "the change stays to fix");
});

test("settings: a cloud service without a key is flagged; a stored key is never shown again", async t => {
  const p = await openPage(t);
  await openSettings(p);
  const warn = () => section(p, "transcribe").querySelector("p.setWarn[data-needs]");
  assert.ok(warn().hidden);
  p.change(field(p, "transcribe.provider"), "openai");
  assert.ok(!warn().hidden && /openai potřebuje klíč OpenAI/.test(warn().textContent));

  assert.match(keyRow(p, "openai").querySelector(".keyState").textContent, /chybí/);
  const inp = keyRow(p, "openai").querySelector("input[type=password]");
  p.input(inp, "sk-test-OPENAI-123");
  p.button("Uložit klíč", keyRow(p, "openai")).click();
  await waitFor(() => /uložen ve Správci/.test(keyRow(p, "openai").querySelector(".keyState").textContent), "the key stored");
  assert.ok(warn().hidden, "the warning goes once the key is there");
  assert.equal(field(p, "transcribe.provider").value, "openai", "an unsaved field change survives storing a key");
  const everything = JSON.stringify(await server("/api/settings")) + p.doc.documentElement.outerHTML;
  assert.ok(!everything.includes("sk-test-OPENAI-123"), "the key value never comes back");

  const del = p.button("Smazat", keyRow(p, "openai"));
  del.click();
  assert.equal((await server("/api/settings")).secrets.find(s => s.name === "openai").state, "vault", "one click only arms");
  p.button("Opravdu smazat", keyRow(p, "openai")).click();
  await waitFor(() => /chybí/.test(keyRow(p, "openai").querySelector(".keyState").textContent), "the key removed");
});

test("settings: closing with unsaved changes needs a second click; Esc closes", async t => {
  const p = await openPage(t);
  await openSettings(p);
  p.input(field(p, "user.name"), "Někdo jiný");
  p.$("settingsClose").click();
  assert.ok(!p.$("settings").hidden, "the first close only warns");
  assert.match(p.$("status").textContent, /neuložené změny/);
  p.$("settingsClose").click();
  assert.ok(p.$("settings").hidden);
  await openSettings(p);
  assert.equal(field(p, "user.name").value, "Jan Novák", "the dropped change was not saved");
  p.doc.dispatchEvent(new p.window.KeyboardEvent("keydown", { key: "Escape" }));
  assert.ok(p.$("settings").hidden);
});

test("settings: model fields are editable dropdowns that say what is local and what downloads", async t => {
  const p = await openPage(t);
  await openSettings(p);
  const model = field(p, "summarize.model");
  assert.equal(model.tagName, "SELECT", "a real dropdown: it always lists everything, whatever is selected");
  assert.match(model.options[0].textContent, /seznam zatím nenačten/, "the settings open without asking Ollama or Claude");
  model.dispatchEvent(new p.window.Event("focus"));  // opening the dropdown asks for the current lists
  await waitFor(() => /modely načteny/.test(p.$("status").textContent), "the live model lists");
  const opts = () => [...model.options].map(o => o.textContent);
  assert.deepEqual(opts(), ["gemma4:31b   —   ✓ v počítači (Ollama)", "jiný…"]);
  p.change(field(p, "summarize.provider"), "anthropic");
  assert.deepEqual(opts(), ["claude-opus-5-5   —   cloud (Claude API)", "jiný…"], "the list follows the service");
  p.change(model, "claude-opus-5-5");
  assert.equal(model.closest(".setRow").querySelector(".modelNote").textContent, "cloud (Claude API)");
  p.change(field(p, "summarize.provider"), "ollama");
  p.change(model, "__other__");
  const other = model.closest(".setRow").querySelector("input.comboOther");
  assert.ok(!other.hidden, "jiný… opens a text box");
  p.input(other, "qwen9:70b");
  assert.match(model.closest(".setRow").querySelector(".modelNote").textContent, /není stažený – stáhněte ho: ollama pull qwen9:70b/);
  assert.ok(model.closest(".setRow").classList.contains("changed"));
  const adder = field(p, "summarize.compare").closest(".setRow").querySelector("select.adder");
  p.change(adder, "anthropic:claude-opus-5-5");
  assert.equal(field(p, "summarize.compare").value, "anthropic:claude-opus-5-5", "picking adds a line");
});

test("settings: the tray's Settings… opens them directly; choices read as sentences; the Windows sound button", async t => {
  const p = await openPage(t, "", { query: "open=settings" });
  await waitFor(() => field(p, "capture.device_missing"), "the settings opened by the link");
  assert.ok(!p.$("settings").hidden);
  const texts = [...field(p, "capture.device_missing").options].map(o => o.textContent);
  assert.deepEqual(texts, ["Zeptat se, čím nahrávat (nabídne dostupná zařízení)", "Nenahrávat a nahlásit chybu",
                           "Tiše použít výchozí vstup Windows"]);
  assert.equal(field(p, "capture.device_missing").value, "ask", "the values stay the same");
  assert.ok(field(p, "capture.tray_open"), "what the tray icon opens is set here too");
  const sound = p.$("soundSettings");
  assert.ok(sound && section(p, "capture").contains(sound), "the sound button sits under Nahrávání");
  assert.match(section(p, "capture").textContent, /Test mikrofonu: pravým tlačítkem na ikonu/);
});
