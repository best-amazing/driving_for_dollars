// pipeline.ts
//
// End-to-end orchestrator: CSV → Geocode → Parcels → Street View → Sheets
//
// Run with:  npx ts-node src/pipeline.ts path/to/listings.csv
//
// CSV format expected (first row is header):
//   id,street,city,state,zip
//   1,"142 S Champion Ave","Columbus","OH","43205"
//
// Required env vars (put in .env):
//   GOOGLE_MAPS_API_KEY                  — for Street View (Phase 2)
//   GOOGLE_SERVICE_ACCOUNT_KEY_PATH      — path to service account JSON (Phase 3)
//   SPREADSHEET_ID                       — your master Google Sheet ID (Phase 3)

import * as fs from "fs";
import * as path from "path";

import { AddressInput, processAddresses } from "./integrations/street-parcel-resolver";
import { verifyStreetCompleteness } from "./integrations/street-view-verifier";
import { upsertParcels, COLUMN_ORDER } from "./integrations/sheets-writer";
import { ingestListingsCsv } from "./integrations/listing-csv-ingestor";

try { require("dotenv").config(); } catch {}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  const csvPath = process.argv[2];
  if (!csvPath) {
    console.error("Usage: npx ts-node src/pipeline.ts <path-to-csv>");
    process.exit(1);
  }

  const resolvedPath = path.resolve(csvPath);
  if (!fs.existsSync(resolvedPath)) {
    console.error(`File not found: ${resolvedPath}`);
    process.exit(1);
  }

  console.log("=".repeat(60));
  console.log("Organic Driving for Dollars — Full Pipeline");
  console.log("=".repeat(60));

  // ── Phase 1: Geocode + parcel query ─────────────────────────────────────

  console.log("\n[Phase 1] Parsing CSV and resolving parcels...");
  const targetState = process.env.DEFAULT_STATE ?? "OH";
  const { resolvable } = await ingestListingsCsv(resolvedPath, { targetState });
  console.log(`[Phase 1] ${resolvable.length} address(es) loaded from CSV.`);

  const streetResults = await processAddresses(resolvable);

  const allParcels = streetResults.flatMap((r) => r.all);
  console.log(
    `[Phase 1] Complete — ${streetResults.length} street(s), ${allParcels.length} parcel(s) total.`,
  );

  if (allParcels.length === 0) {
    console.log("No parcels found. Exiting.");
    process.exit(0);
  }

  // ── Phase 2: Street View verification ───────────────────────────────────

  console.log("\n[Phase 2] Verifying addresses via Street View metadata...");
  const { verified, summary } = await verifyStreetCompleteness(allParcels, {
    verbose: true,
  });

  console.log(`[Phase 2] Complete:`);
  console.log(`  Confirmed: ${summary.confirmed}`);
  console.log(`  Flagged:   ${summary.flagged}`);
  if (summary.flaggedAddresses.length > 0) {
    console.log(`  Flagged addresses will still be written to the sheet with needsReview=YES`);
  }

  // ── Phase 3: Write to Google Sheets ─────────────────────────────────────

  const spreadsheetId = process.env.SPREADSHEET_ID;
  if (!spreadsheetId) {
    console.warn(
      "\n[Phase 3] SPREADSHEET_ID not set — skipping Sheets write.\n" +
      "Set it in .env and re-run to write results to your master sheet.",
    );
  } else {
    console.log("\n[Phase 3] Writing to Google Sheets...");
    const upsertResult = await upsertParcels(spreadsheetId, verified);
    console.log(`[Phase 3] Complete:`);
    console.log(`  Inserted: ${upsertResult.inserted}`);
    console.log(`  Skipped (duplicates): ${upsertResult.skipped}`);
    if (upsertResult.errors.length > 0) {
      console.error(`  Errors: ${upsertResult.errors.join(", ")}`);
    }
  }

  // ── Phase 4: Save local files for inspection ────────────────────────────

  console.log("\n[Phase 4] Saving results to local files...");
  const dir = path.resolve(process.cwd(), "results");
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
  const ext = path.extname(resolvedPath);
  const base = path.basename(resolvedPath, ext);

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

  console.log("\n" + "=".repeat(60));
  console.log("Pipeline complete.");
  console.log("=".repeat(60));
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

main().catch((err) => {
  console.error("Pipeline error:", err);
  process.exit(1);
});
