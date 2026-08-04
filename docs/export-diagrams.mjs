/**
 * Renders the Mermaid diagrams in one HTML document to PNGs for the Word build.
 * Usage: node docs/export-diagrams.mjs [basename]   // default: architecture
 */
import { pathToFileURL, fileURLToPath } from "node:url";
import path from "node:path";
import puppeteer from "puppeteer";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const stem = path.parse(process.argv[2] ?? "architecture").name;
const fileUrl = pathToFileURL(path.join(__dirname, `${stem}.html`)).href;

const browser = await puppeteer.launch({
  headless: true,
  args: ["--no-sandbox", "--disable-setuid-sandbox"],
});

try {
  const page = await browser.newPage();
  // Scale factor 3 keeps the diagram legible after Word scales it to fit a page.
  await page.setViewport({ width: 1100, height: 1200, deviceScaleFactor: 3 });
  await page.goto(fileUrl, { waitUntil: "networkidle0", timeout: 120000 });
  await page.waitForFunction(
    () => document.body?.dataset?.diagramsReady === "true",
    { timeout: 120000 },
  );
  await new Promise((r) => setTimeout(r, 1500));

  // Drop the card chrome so the exported image is just the diagram.
  await page.$$eval(".diagram", (nodes) => {
    for (const n of nodes) {
      n.style.border = "none";
      n.style.background = "#ffffff";
      n.style.padding = "8px";
    }
  });

  const diagrams = await page.$$(".diagram");
  for (let i = 0; i < diagrams.length; i++) {
    const out = path.join(__dirname, `${stem}-diagram-${i + 1}.png`);
    await diagrams[i].screenshot({ path: out });
    console.log(`Wrote ${out}`);
  }
} finally {
  await browser.close();
}
