import Tesseract from "tesseract.js";
import * as fs from "fs";
import * as path from "path";
import { ingestListingsCsv }                             from "./listing-csv-ingestor";
import { processAddresses, AddressInput, StreetParcelResult } from "./street-parcel-resolver";
import { verifyStreetCompleteness }                      from "./street-view-verifier";
import { upsertParcels, COLUMN_ORDER }                      from "./sheets-writer";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface ExtractedAddress {
  streetName: string;
  city: string | null;
  state: string;
  confidence: "high" | "medium" | "low";
  sourceImage: string;
  notes: string | null;
}

// ── Config ────────────────────────────────────────────────────────────────────

function getConfig() {
  return {
    defaultCity:    process.env.DEFAULT_CITY  ?? "Columbus",
    defaultState:   process.env.DEFAULT_STATE ?? "OH",
    defaultCounty:  process.env.DEFAULT_COUNTY,   // optional — set for single-county sessions
    spreadsheetId:  process.env.SPREADSHEET_ID,
    googleMapsKey:  process.env.GOOGLE_MAPS_API_KEY,
  };
}

const DEFAULT_CITY  = process.env.DEFAULT_CITY  ?? "Columbus";
const DEFAULT_STATE = process.env.DEFAULT_STATE ?? "OH";

// ── Street name patterns ──────────────────────────────────────────────────────
const STREET_SUFFIXES = [
  "Street", "St", "Avenue", "Ave", "Boulevard", "Blvd", "Road", "Rd",
  "Drive", "Dr", "Lane", "Ln", "Court", "Ct", "Place", "Pl",
  "Parkway", "Pkwy", "Way", "Circle", "Cir", "Trail", "Trl",
  "Highway", "Hwy", "Pike",
];
const DIRECTION_SUFFIX = "(?:\\s+(?:NW|NE|SW|SE|N|S|E|W))?";
const SUFFIX_PATTERN = STREET_SUFFIXES.map((s) => s.replace(/\./g, "\\.")).join("|");
const STREET_REGEX = new RegExp(`\\b([A-Z][A-Za-z0-9 .'-]{2,}\\b(?:${SUFFIX_PATTERN})${DIRECTION_SUFFIX})`, "g");

async function ocrImage(imagePath: string): Promise<string> {
  const result = await Tesseract.recognize(imagePath, "eng", { logger: () => {} });
  return result.data.text;
}

function parseStreetsFromText(rawText: string, sourceImage: string): ExtractedAddress[] {
  const found = new Map<string, ExtractedAddress>();
  const lines = rawText.split(/[\n\r]+/).map((l) => l.trim()).filter((l) => l.length > 3);
  for (const line of lines) {
    let match: RegExpExecArray | null;
    STREET_REGEX.lastIndex = 0;
    while ((match = STREET_REGEX.exec(line)) !== null) {
      const raw = match[1].trim();
      if (raw.split(" ").length < 2) continue;
      const streetName = raw.replace(/\s{2,}/g, " ");
      const key = streetName.toLowerCase();
      if (!found.has(key)) {
        const isWholeLine = line.trim().toLowerCase() === key;
        found.set(key, { streetName, city: null, state: DEFAULT_STATE, confidence: isWholeLine ? "high" : "medium", sourceImage, notes: null });
      }
    }
  }
  return [...found.values()];
}

async function extractFromImage(imagePath: string): Promise<ExtractedAddress[]> {
  const filename = path.basename(imagePath);
  process.stdout.write(`[image-extractor] OCR: ${filename} … `);
  const rawText = await ocrImage(imagePath);
  const streets = parseStreetsFromText(rawText, filename);
  console.log(streets.length > 0 ? streets.map((s) => `"${s.streetName}"`).join(", ") : "(no streets found)");
  return streets;
}

export function toAddressInputs(extracted: ExtractedAddress[], startId = 1): AddressInput[] {
  const seen = new Set<string>();
  const inputs: AddressInput[] = [];
  for (const e of extracted) {
    const key = e.streetName.trim().toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    inputs.push({
      id: String(startId + inputs.length),
      street: `100 ${e.streetName}`,
      city: e.city ?? DEFAULT_CITY,
      state: e.state ?? DEFAULT_STATE,
      sourceImage: e.sourceImage,
    });
  }
  return inputs;
}

export async function extractAddressesFromImages(imagePaths: string[]): Promise<{ extracted: ExtractedAddress[]; addressInputs: AddressInput[] }> {
  const allExtracted: ExtractedAddress[] = [];
  for (const imgPath of imagePaths) {
    if (!fs.existsSync(imgPath)) {
      console.warn(`[image-extractor] File not found, skipping: ${imgPath}`);
      continue;
    }
    const streets = await extractFromImage(imgPath);
    allExtracted.push(...streets);
  }
  const addressInputs = toAddressInputs(allExtracted);
  console.log(`\n[image-extractor] ${allExtracted.length} street(s) across all images → ${addressInputs.length} unique input(s) for pipeline.`);
  return { extracted: allExtracted, addressInputs };
}

// ── CSV pipeline entry ────────────────────────────────────────────────────────

export async function runCsvPipeline(csvPath: string): Promise<StreetParcelResult[]> {
  const cfg = getConfig();
  console.log("\n" + "=".repeat(60));
  console.log("CSV Pipeline");
  console.log("=".repeat(60));
  const { resolvable, countyOnly, dropped } = await ingestListingsCsv(csvPath, { targetState: cfg.defaultState });
  if (countyOnly.length > 0) {
    console.log(`\n[pipeline] ℹ ${countyOnly.length} listing(s) have county-only addresses:`);
    countyOnly.forEach((c) => console.log(`  • [${c.listing.source}] "${c.listing.rawAddress}" → county=${c.county ?? "unknown"}, score=${c.listing.dealScore}`));
    console.log("  These cannot be parcel-resolved. Consider contacting the seller for the address.");
  }
  if (resolvable.length === 0) {
    console.log("[pipeline] No resolvable addresses found in CSV.");
    return [];
  }
  console.log(`\n[pipeline] Resolving ${resolvable.length} address(es)...`);
  const parcelResults = await processAddresses(resolvable, { defaultState: cfg.defaultState, defaultCounty: cfg.defaultCounty });
  const totalParcels = parcelResults.reduce((n, r) => n + r.totalParcels, 0);
  console.log(`\n[pipeline] Phase 1 — ${totalParcels} parcel(s) found across ${parcelResults.filter((r) => !r.error).length} street(s).`);
  return parcelResults;
}

// ── Combined pipeline runner ──────────────────────────────────────────────────

export async function runFullPipeline(options: { imageParcels?: StreetParcelResult[]; csvPath?: string; }): Promise<void> {
  const cfg = getConfig();
  let allResults: StreetParcelResult[] = [...(options.imageParcels ?? [])];
  if (options.csvPath) {
    const csvResults = await runCsvPipeline(options.csvPath);
    allResults = [...allResults, ...csvResults];
  }
  if (allResults.length === 0) {
    console.log("[pipeline] No parcels to process.");
    return;
  }
  const allParcels = allResults.flatMap((r) => r.all);
  console.log(`\n[pipeline] Total parcels for Phase 2: ${allParcels.length}`);
  console.log("\n" + "=".repeat(60));
  console.log("Phase 2 — Street View Verification");
  console.log("=".repeat(60));
  const { verified, summary } = await verifyStreetCompleteness(allParcels, { verbose: true });
  console.log(`\n[pipeline] Phase 2 — ${summary.confirmed} confirmed, ${summary.flagged} flagged`);

  if (!cfg.spreadsheetId) {
    console.warn("\n[pipeline] SPREADSHEET_ID not set — skipping Google Sheets write.");
  } else {
    console.log("\n" + "=".repeat(60));
    console.log("Phase 3 — Google Sheets");
    console.log("=".repeat(60));
    const upsertResult = await upsertParcels(cfg.spreadsheetId, verified);
    console.log(`\n[pipeline] Phase 3 — inserted=${upsertResult.inserted}, skipped=${upsertResult.skipped}, errors=${upsertResult.errors.length}`);
  }

  // ── Phase 4: Save local files for inspection ────────────────────────────

  console.log("\n[Phase 4] Saving results to local files...");
  
  let dir = path.resolve(process.cwd(), "results");
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  let base = "extracted_output";

  if (options.csvPath) {
    const resolvedPath = path.resolve(options.csvPath);
    const ext = path.extname(resolvedPath);
    base = path.basename(resolvedPath, ext);
  }

  const jsonOutputPath = path.join(dir, `${base}.output.json`);
  const csvOutputPath = path.join(dir, `${base}.output.csv`);

  try {
    // Write JSON file
    fs.writeFileSync(jsonOutputPath, JSON.stringify(verified, null, 2), "utf-8");
    console.log(`  ✓ Saved JSON output to: ${jsonOutputPath}`);

    // Write CSV file
    const now = new Date().toISOString().slice(0, 10); // "YYYY-MM-DD"
    const headers = COLUMN_ORDER.map(([header]) => header);
    const rows = verified.map((parcel) =>
      COLUMN_ORDER.map(([, extractor]) => extractor(parcel, now))
    );
    const csvContent = convertToCsv(headers, rows);
    fs.writeFileSync(csvOutputPath, csvContent, "utf-8");
    console.log(`  ✓ Saved CSV output to:  ${csvOutputPath}`);
  } catch (err: any) {
    console.error(`  ✗ Failed to save local files: ${err?.message ?? err}`);
  }
}

// ── Helpers for local CSV generation ──────────────────────────────────────────

function escapeCsvValue(val: string): string {
  const str = val ?? "";
  if (str.includes(",") || str.includes('"') || str.includes("\n") || str.includes("\r")) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

function convertToCsv(headers: string[], rows: string[][]): string {
  const headerLine = headers.map(escapeCsvValue).join(",");
  const dataLines = rows.map((row) => row.map(escapeCsvValue).join(","));
  return [headerLine, ...dataLines].join("\n");
}

// ── CLI entry ─────────────────────────────────────────────────────────────────

if (require.main === module) {
  try { require("dotenv").config(); } catch {}

  const args = process.argv.slice(2);
  const csvFlag = args.indexOf("--csv");
  const csvPath = csvFlag !== -1 ? args[csvFlag + 1] : undefined;
  const hasPipeline = args.includes("--run-pipeline");
  const imagePaths = args.filter((a, i) => !a.startsWith("--") && i !== csvFlag + 1 && a !== csvPath);

  if (!hasPipeline && !csvPath) {
    console.error("Usage:");
    console.error("  npx tsx integrations/image-address-extractor.ts --run-pipeline [images...]");
    console.error("  npx tsx integrations/image-address-extractor.ts --csv listings.csv");
    console.error("  npx tsx integrations/image-address-extractor.ts --run-pipeline [images...] --csv listings.csv");
    process.exit(1);
  }

  (async () => {
    if (!hasPipeline && csvPath) {
      const results = await runCsvPipeline(csvPath);
      console.log(`\nPhase 1 — ${results.reduce((n, r) => n + r.totalParcels, 0)} parcel(s) found.\nDone.`);
      return;
    }

    let imageParcels: StreetParcelResult[] = [];

    if (imagePaths.length > 0) {
      console.log("=".repeat(60));
      console.log("Image Address Extractor (OCR)");
      console.log("=".repeat(60) + "\n");
      const { extracted, addressInputs } = await extractAddressesFromImages(imagePaths);
      
      if (extracted.length > 0) {
        const outDir = path.resolve(process.cwd(), "results");
        if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
        const outCsv = path.resolve(outDir, "extracted_addresses.csv");
        const header = "Street Name,City,State,Confidence,Source Image\n";
        const rows = extracted.map((e) => `"${e.streetName}","${e.city ?? "" }","${e.state}","${e.confidence}","${e.sourceImage}"`).join("\n");
        fs.writeFileSync(outCsv, header + rows, "utf-8");
        console.log(`\n✅ Saved extracted addresses to: ${outCsv}`);
      }

      if (addressInputs.length > 0) {
        const cfg = getConfig();
        imageParcels = await processAddresses(addressInputs, { defaultState: cfg.defaultState, defaultCounty: cfg.defaultCounty });
      }
    } else if (hasPipeline && !csvPath) {
       console.log("No images provided for OCR.");
    }

    await runFullPipeline({ imageParcels, csvPath });
  })();
}