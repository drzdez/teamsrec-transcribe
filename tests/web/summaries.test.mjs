// Zápis: a tab per summary named by its model, generating with another model adds a tab, tabs move and close.
import assert from "node:assert/strict";
import { test } from "node:test";
import { WEEKLY, openPage, server, waitFor } from "./page.mjs";

const tabs = p => [...p.doc.querySelectorAll("#sumTabs .sumTab")].map(t => t.querySelector("span").textContent);
const tab = (p, label) => [...p.doc.querySelectorAll("#sumTabs .sumTab")].find(t => t.querySelector("span").textContent === label);

async function openSummaries(p) {
  await waitFor(() => p.doc.querySelector(".card"), "the speakers");
  p.doc.querySelector('#subtabs button[data-sub="summary"]').click();
  await waitFor(() => p.doc.querySelector("#sumTabs .sumTab"), "the summary tabs");
}

test("summaries: tabs by model, generate another, reorder by dragging, close = delete after a second click", async t => {
  const p = await openPage(t, WEEKLY);
  await openSummaries(p);
  assert.ok(!p.$("sumBar").hidden && p.$("docPick").hidden, "tabs replace the dropdown");
  assert.deepEqual(tabs(p), ["test"], "the tab is named after the model in the file's stamp");
  assert.ok(tab(p, "test").querySelector(".main"), "the configured model's summary is marked as the main one");
  await waitFor(() => p.$("sumModel").value === "ollama:gemma4:31b", "the configured model as the default");
  assert.deepEqual([...p.$("sumModel").options].map(o => o.value), ["ollama:gemma4:31b", "__other__"],
                   "at first only what is known without asking anybody");
  p.$("sumModel").dispatchEvent(new p.window.Event("focus"));  // opening the dropdown asks for the current lists
  await waitFor(() => p.$("sumModel").options.length === 3, "the live lists");
  assert.deepEqual([...p.$("sumModel").options].map(o => o.value), ["ollama:gemma4:31b", "anthropic:claude-opus-5-5", "__other__"],
                   "every model is listed although one is selected");
  assert.equal(p.$("sumModel").value, "ollama:gemma4:31b", "the selection is kept");
  assert.deepEqual([...p.$("sumModel").querySelectorAll("optgroup")].map(g => g.label),
                   ["Ollama – v tomto počítači", "Claude – cloud (text přepisu jde ven)"]);

  p.change(p.$("sumModel"), "anthropic:claude-opus-5-5");
  p.$("sumGen").click(); p.$("sumGen").click();  // generating asks for a second click
  await waitFor(() => p.$("quit").disabled, "the job running");
  await waitFor(() => tabs(p).length === 2 && !p.$("quit").disabled, "the new summary's tab");
  assert.equal(p.$("status").textContent, "zápis claude-opus-5-5 hotový");
  assert.deepEqual(tabs(p), ["test", "claude-opus-5-5"]);
  assert.ok(tab(p, "claude-opus-5-5").classList.contains("active"), "the new one is shown");
  await waitFor(() => p.$("docMain").textContent.includes("Zápis od claude-opus-5-5"), "its text");
  assert.match(p.$("sumAgain").textContent, /Přegenerovat „claude-opus-5-5“/);

  const drop = new p.window.Event("drop", { bubbles: true, cancelable: true });
  Object.defineProperty(drop, "dataTransfer", { value: { getData: () => tab(p, "claude-opus-5-5").dataset.file } });
  tab(p, "test").dispatchEvent(drop);
  assert.deepEqual(tabs(p), ["claude-opus-5-5", "test"], "dragged in front");

  const x = tab(p, "claude-opus-5-5").querySelector(".x");
  x.click();
  assert.equal(x.textContent, "smazat?", "the first click only asks");
  assert.match(p.$("status").textContent, /se smaže i se souborem/);
  assert.equal(tabs(p).length, 2);
  x.click();
  await waitFor(() => tabs(p).length === 1, "the tab and its summary gone");
  assert.deepEqual(tabs(p), ["test"]);
  const docs = (await server(`/api/recordings/${WEEKLY}`)).docs.summaries.map(d => d.model);
  assert.deepEqual(docs, ["test"], "the file is deleted on the server");
  assert.match(p.$("status").textContent, /claude-opus-5-5.* smazán/, "the page's message or the server's event");
});

test("summaries: the order of the tabs is stored for the recording", async t => {
  const p = await openPage(t, WEEKLY);
  await openSummaries(p);
  await waitFor(() => p.$("sumModel").value === "ollama:gemma4:31b", "the models");
  p.$("sumModel").dispatchEvent(new p.window.Event("focus"));
  await waitFor(() => p.$("sumModel").options.length === 3, "the live lists");
  p.change(p.$("sumModel"), "anthropic:claude-opus-5-5");
  p.$("sumGen").click(); p.$("sumGen").click();  // generating asks for a second click
  await waitFor(() => tabs(p).length === 2 && !p.$("quit").disabled, "a second summary");
  const drop = new p.window.Event("drop", { bubbles: true, cancelable: true });
  Object.defineProperty(drop, "dataTransfer", { value: { getData: () => tab(p, "claude-opus-5-5").dataset.file } });
  tab(p, "test").dispatchEvent(drop);
  const saved = JSON.parse(p.window.localStorage.getItem(`teamsrec.sumtabs.${WEEKLY}`));
  assert.deepEqual(saved.order.map(f => f.split(".summary")[1]), [".claude-opus-5-5.md", ".md"], "the order is stored");
  p.doc.querySelector('#subtabs button[data-sub="speakers"]').click();
  p.doc.querySelector('#subtabs button[data-sub="summary"]').click();
  await waitFor(() => tabs(p).length === 2, "the tabs again");
  assert.deepEqual(tabs(p), ["claude-opus-5-5", "test"], "the stored order is used");
});

test("summaries: an unconfirmed delete goes back to x and its question leaves the status line", async t => {
  const p = await openPage(t, WEEKLY);
  await openSummaries(p);
  const x = tab(p, "test").querySelector(".x");
  x.click();
  assert.match(p.$("status").textContent, /se smaže/);
  p.doc.querySelector('#subtabs button[data-sub="speakers"]').click();
  assert.equal(p.$("status").textContent, "", "leaving the tab drops the question");
  p.doc.querySelector('#subtabs button[data-sub="summary"]').click();
  await waitFor(() => p.doc.querySelector("#sumTabs .sumTab"), "the tabs");
  tab(p, "test").querySelector(".x").click();
  await waitFor(() => p.$("status").textContent === "" && tab(p, "test").querySelector(".x").textContent === "✕",
                "the question gone after 4 s", 6000);
  assert.ok(tabs(p).includes("test"), "nothing deleted");
});

test("minutes being written: said in the Zápis tab, not above the tabs", async t => {
  const p = await openPage(t, WEEKLY);
  await waitFor(() => p.doc.querySelector(".card"), "speakers");
  p.doc.querySelector('#subtabs [data-sub="summary"]').click();
  await waitFor(() => p.doc.querySelector("#docMain") && !p.$("docMain").hidden, "the Zápis tab");
  p.$("saveSum").click(); p.$("saveSum").click();  // regenerate the minutes (asks first)
  await waitFor(() => p.doc.querySelector("#docMain [data-minutes-run]"), "the note in the Zápis tab");
  assert.match(p.$("docMain").textContent, /zápis se právě generuje/i);
  assert.ok(!p.doc.getElementById("queueBanner"), "no line above the tabs for minutes");
  await waitFor(() => !p.doc.querySelector("#docMain [data-minutes-run]"), "gone when done", 8000);
});
