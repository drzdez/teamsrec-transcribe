// Uložit a přegenerovat zápis on a recording that has no transcript yet: it starts the whole processing.
// A file of its own: the other job tests already transcribe the fixture's only unprocessed recording.
import assert from "node:assert/strict";
import { test } from "node:test";
import { BOARD_NEW, openPage, waitFor } from "./page.mjs";

test("Uložit a přegenerovat zápis on a recording without a transcript starts the whole processing", async t => {
  const p = await openPage(t, BOARD_NEW);
  await waitFor(() => p.doc.querySelector("#main button.primary-btn"), "a recording without a transcript");
  p.$("saveSum").click();
  assert.match(p.$("status").textContent, /spustí se celé zpracování/, "the question says what will run");
  p.$("saveSum").click();
  await waitFor(() => p.requests.some(r => r === `POST /api/recordings/${BOARD_NEW}/process`), "the processing started");
  await waitFor(() => p.$("status").textContent === "hotovo: přepis, titulky i zápis", "processed");
  assert.ok(p.doc.querySelector(".card"), "the new speakers are shown");
});
