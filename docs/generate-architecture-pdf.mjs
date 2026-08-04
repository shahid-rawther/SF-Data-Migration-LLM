/**
 * Renders one of the HTML documents in this folder to a PDF via Puppeteer.
 * Usage: node docs/generate-architecture-pdf.mjs [basename]   // default: architecture
 */
import { pathToFileURL } from "node:url";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const stem = path.parse(process.argv[2] ?? "architecture").name;
const htmlPath = path.join(__dirname, `${stem}.html`);
const pdfPath = path.join(__dirname, `${stem.toUpperCase()}.pdf`);
const fileUrl = pathToFileURL(htmlPath).href;

// Prefer Puppeteer's own Chrome, but fall back to an installed Chrome or Edge so
// the build works without `npx puppeteer browsers install chrome`.
async function launch() {
  const base = { headless: true, args: ["--no-sandbox", "--disable-setuid-sandbox"] };
  const attempts = [base, { ...base, channel: "chrome" }, { ...base, channel: "msedge" }];
  let lastErr;
  for (const options of attempts) {
    try {
      return await puppeteer.launch(options);
    } catch (err) {
      lastErr = err;
    }
  }
  throw new Error(
    `Could not launch a browser. Install one with \`npx puppeteer browsers install chrome\`.\n${lastErr}`,
  );
}

const browser = await launch();

try {
  const page = await browser.newPage();
  await page.goto(fileUrl, { waitUntil: "networkidle0", timeout: 120000 });

  // Only documents with Mermaid blocks set the ready flag, so don't wait on
  // one that has no diagrams.
  const hasDiagrams = await page.$(".mermaid");
  if (hasDiagrams) {
    await page.waitForFunction(
      () => document.body?.dataset?.diagramsReady === "true",
      { timeout: 120000 },
    );
    // Extra beat for SVG layout
    await new Promise((r) => setTimeout(r, 1500));
  }

  const pdfOptions = {
    format: "A4",
    printBackground: true,
    margin: { top: "16mm", bottom: "16mm", left: "12mm", right: "12mm" },
  };

  let target = pdfPath;
  try {
    await page.pdf({ ...pdfOptions, path: target });
  } catch (err) {
    // The file is locked while open in a PDF viewer; write beside it instead.
    if (err?.code !== "EBUSY") throw err;
    target = path.join(__dirname, `${stem.toUpperCase()}-latest.pdf`);
    await page.pdf({ ...pdfOptions, path: target });
  }
  console.log(`Wrote ${target}`);
} finally {
  await browser.close();
}
