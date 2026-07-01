// integrations/sheets-writer.ts
//
// Phase 3 — Google Sheets API writer
//
// Writes / upserts VerifiedParcel rows into the "Organic Driving for Dollars"
// tab in your master Google Sheet, with dedup logic on (streetName + houseNumber).
//
// Auth: Service account JSON key file.
//   1. Create a service account in Google Cloud Console.
//   2. Download the JSON key, save it (never commit to git).
//   3. Share your Google Sheet with the service account email as Editor.
//   4. Set GOOGLE_SERVICE_ACCOUNT_KEY_PATH=/path/to/key.json in .env
//      OR set GOOGLE_SERVICE_ACCOUNT_KEY_JSON='{...}' with the raw JSON content.
//
// Sheet layout — columns are fixed; do not reorder them without updating
// COLUMN_ORDER below. Row 1 is always the header row.

import { google, sheets_v4 } from "googleapis";
import { VerifiedParcel } from "./street-view-verifier";

// ── Configuration ─────────────────────────────────────────────────────────────

// Tab name in the master sheet. Must match exactly (case-sensitive).
const TAB_NAME = "Organic Driving for Dollars";

// Dedup key — a row is considered a duplicate if this combination already exists.
const DEDUP_KEY = (p: VerifiedParcel) =>
  `${p.streetName.trim().toLowerCase()}|${p.houseNumber.trim()}`;

// Column order written to the sheet. Add / reorder columns here only.
// Each entry: [header label, value extractor]
export type ColumnDef = [string, (p: VerifiedParcel, now: string) => string];

export const COLUMN_ORDER: ColumnDef[] = [
  ["Full Address",          (p) => p.fullAddress],
  ["House Number",          (p) => p.houseNumber],
  ["Street Name",           (p) => p.streetName],
  ["Side",                  (p) => p.side],
  ["City",                  (p) => p.city],
  ["State",                 (p) => p.state],
  ["ZIP",                   (p) => p.zip],
  ["County",                (p) => p.county],
  ["Parcel ID",             (p) => p.parcelId],
  ["Owner Name",            (p) => p.ownerName ?? ""],
  ["Lat",                   (p) => p.lat?.toString() ?? ""],
  ["Lng",                   (p) => p.lng?.toString() ?? ""],
  ["Source Listing",        (p) => (p as any).sourceImage ? `Image: ${(p as any).sourceImage}` : p.sourceListingAddress],
  ["Street View Status",    (p) => p.streetViewStatus],
  ["Street View Date",      (p) => p.streetViewDate ?? ""],
  ["Street View Link",      (p) => p.streetViewUrl ?? ""],
  ["Google Maps Link",      (p) => p.googleMapsUrl ?? ""],
  ["Needs Review",          (p) => p.needsReview ? "YES" : ""],
  ["Skip Trace Status",     (p) => (p as any).skipTraceStatus ?? "pending"],
  ["Owner Phones",          (p) => ((p as any).ownerPhones ?? []).join(", ")],
  ["Owner Emails",          (p) => ((p as any).ownerEmails ?? []).join(", ")],
  ["Status",                (p) => (p as any).status ?? "pending"],
  ["Date Added",            (_p, now) => now],
];

const HEADER_ROW = COLUMN_ORDER.map(([header]) => header);

// ── Types ─────────────────────────────────────────────────────────────────────

export interface UpsertResult {
  inserted: number;
  updated: number;
  skipped: number;   // already existed (dedup)
  errors: string[];
}

// ── Auth ──────────────────────────────────────────────────────────────────────

function getAuthClient() {
  const keyPath = process.env.GOOGLE_SERVICE_ACCOUNT_KEY_PATH;
  const keyJson = process.env.GOOGLE_SERVICE_ACCOUNT_KEY_JSON;

  if (!keyPath && !keyJson) {
    throw new Error(
      "No service account credentials found.\n" +
      "Set GOOGLE_SERVICE_ACCOUNT_KEY_PATH=/path/to/key.json\n" +
      "  or GOOGLE_SERVICE_ACCOUNT_KEY_JSON='{...}' in your .env file.",
    );
  }

  let credentials: Record<string, unknown>;

  if (keyJson) {
    try {
      credentials = JSON.parse(keyJson);
    } catch {
      throw new Error("GOOGLE_SERVICE_ACCOUNT_KEY_JSON is not valid JSON.");
    }
  } else {
    // Load from file path
    const fs = require("fs");
    if (!fs.existsSync(keyPath!)) {
      throw new Error(`Service account key file not found: ${keyPath}`);
    }
    credentials = JSON.parse(fs.readFileSync(keyPath!, "utf-8"));
  }

  return new google.auth.GoogleAuth({
    credentials,
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });
}

// ── Sheet setup helpers ───────────────────────────────────────────────────────

async function getOrCreateTab(
  sheets: sheets_v4.Sheets,
  spreadsheetId: string,
): Promise<void> {
  const meta = await sheets.spreadsheets.get({ spreadsheetId });
  const tabs = meta.data.sheets ?? [];
  const exists = tabs.some(
    (s) => s.properties?.title === TAB_NAME,
  );

  if (!exists) {
    console.log(`[sheets] Tab "${TAB_NAME}" not found — creating it...`);
    await sheets.spreadsheets.batchUpdate({
      spreadsheetId,
      requestBody: {
        requests: [{ addSheet: { properties: { title: TAB_NAME } } }],
      },
    });
    console.log(`[sheets] Tab created.`);
  }
}

async function ensureHeaderRow(
  sheets: sheets_v4.Sheets,
  spreadsheetId: string,
): Promise<void> {
  const range = `'${TAB_NAME}'!A1:${colLetter(HEADER_ROW.length)}1`;
  const existing = await sheets.spreadsheets.values.get({
    spreadsheetId,
    range,
  });

  const currentHeader = existing.data.values?.[0] ?? [];
  if (currentHeader.join("|") !== HEADER_ROW.join("|")) {
    console.log("[sheets] Writing header row...");
    await sheets.spreadsheets.values.update({
      spreadsheetId,
      range,
      valueInputOption: "RAW",
      requestBody: { values: [HEADER_ROW] },
    });
  }
}

// ── Dedup logic ───────────────────────────────────────────────────────────────

async function fetchExistingDedupeKeys(
  sheets: sheets_v4.Sheets,
  spreadsheetId: string,
): Promise<Map<string, number>> {
  // Read house number (col B) + street name (col C) from all data rows
  const range = `'${TAB_NAME}'!B2:C`;
  const resp = await sheets.spreadsheets.values.get({
    spreadsheetId,
    range,
  });

  const keys = new Map<string, number>();
  let rowIndex = 2;
  for (const row of resp.data.values ?? []) {
    const houseNum = String(row[0] ?? "").trim();
    const streetName = String(row[1] ?? "").trim().toLowerCase();
    if (houseNum && streetName) {
      keys.set(`${streetName}|${houseNum}`, rowIndex);
    }
    rowIndex++;
  }

  return keys;
}

// ── Column letter helper ──────────────────────────────────────────────────────

function colLetter(n: number): string {
  // Converts 1-based column index to A1-notation letter(s)
  // 1→A, 26→Z, 27→AA, etc.
  let s = "";
  while (n > 0) {
    n--;
    s = String.fromCharCode(65 + (n % 26)) + s;
    n = Math.floor(n / 26);
  }
  return s;
}

// ── Public API ─────────────────────────────────────────────────────────────────
//
// upsertParcels()
//   Takes VerifiedParcel[] and writes new rows to the sheet, skipping
//   any parcel whose (streetName + houseNumber) already exists.
//
// Parameters:
//   spreadsheetId — the ID from your Sheet URL:
//     https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit
//   parcels — output of verifyParcels() / verifyStreetCompleteness()

export async function upsertParcels(
  spreadsheetId: string,
  parcels: VerifiedParcel[],
): Promise<UpsertResult> {
  // Only write addresses that were successfully verified by Google Street View
  const validParcels = parcels.filter((p) => p.streetViewStatus === "ok");

  if (validParcels.length === 0) {
    return { inserted: 0, updated: 0, skipped: 0, errors: [] };
  }

  console.log(`[sheets] Connecting to Google Sheets API...`);
  const auth = getAuthClient();
  const sheets = google.sheets({ version: "v4", auth: auth as any });

  // 1. Ensure tab exists and header row is present
  await getOrCreateTab(sheets, spreadsheetId);
  await ensureHeaderRow(sheets, spreadsheetId);

  // 2. Load existing dedup keys so we don't write duplicates
  console.log(`[sheets] Loading existing rows for dedup check...`);
  const existingKeys = await fetchExistingDedupeKeys(sheets, spreadsheetId);
  console.log(`[sheets] ${existingKeys.size} existing row(s) in sheet.`);

  // 3. Filter to only new parcels and queue updates for existing
  const now = new Date().toISOString().slice(0, 10); // "YYYY-MM-DD"
  const newRows: string[][] = [];
  const updateRequests: any[] = [];
  let skipped = 0;
  let updatedCount = 0;

  for (const parcel of validParcels) {
    const key = DEDUP_KEY(parcel);
    if (existingKeys.has(key)) {
      // It's a duplicate. Check if we need to update city/zip and if it's not a within-batch duplicate
      const rowIndex = existingKeys.get(key)!;
      let hasUpdate = false;
      
      if (rowIndex > 0) {
        if (parcel.city) {
          updateRequests.push({
            range: `'${TAB_NAME}'!E${rowIndex}`,
            values: [[parcel.city]]
          });
          hasUpdate = true;
        }
        if (parcel.zip) {
          updateRequests.push({
            range: `'${TAB_NAME}'!G${rowIndex}`,
            values: [[parcel.zip]]
          });
          hasUpdate = true;
        }
      }
      
      if (hasUpdate) updatedCount++;
      skipped++;
      continue;
    }

    const row = COLUMN_ORDER.map(([, extractor]) => extractor(parcel, now));
    newRows.push(row);
    existingKeys.set(key, -1); // prevent duplicates within this batch too
  }

  console.log(
    `[sheets] ${newRows.length} new row(s) to insert, ${skipped} duplicate(s) skipped (${updatedCount} will have city/zip updated).`,
  );

  // 4. Update existing rows in a single API call if needed
  if (updateRequests.length > 0) {
    try {
      await sheets.spreadsheets.values.batchUpdate({
        spreadsheetId,
        requestBody: {
          valueInputOption: "RAW",
          data: updateRequests,
        },
      });
      console.log(`[sheets] ✓ Updated city/zip for ${updatedCount} existing row(s).`);
    } catch (err: any) {
      const msg = err?.message ?? String(err);
      console.error(`[sheets] ✗ Update failed: ${msg}`);
    }
  }

  if (newRows.length === 0) {
    return { inserted: 0, updated: updatedCount, skipped, errors: [] };
  }

  // 5. Append new rows in a single API call (efficient — no row-by-row writes)
  try {
    await sheets.spreadsheets.values.append({
      spreadsheetId,
      range: `'${TAB_NAME}'!A:A`,
      valueInputOption: "RAW",
      insertDataOption: "INSERT_ROWS",
      requestBody: { values: newRows },
    });

    console.log(`[sheets] ✓ Inserted ${newRows.length} row(s).`);
    return { inserted: newRows.length, updated: updatedCount, skipped, errors: [] };
  } catch (err: any) {
    const msg = err?.message ?? String(err);
    console.error(`[sheets] ✗ Append failed: ${msg}`);
    return { inserted: 0, updated: updatedCount, skipped, errors: [msg] };
  }
}

// ── Standalone test run ───────────────────────────────────────────────────────
//
// Run with:  npx ts-node src/integrations/sheets-writer.ts
//
// Set in .env:
//   GOOGLE_SERVICE_ACCOUNT_KEY_PATH=/path/to/service-account.json
//   SPREADSHEET_ID=your_sheet_id_here

if (require.main === module) {
  try { require("dotenv").config(); } catch {}

  const SPREADSHEET_ID = process.env.SPREADSHEET_ID;
  if (!SPREADSHEET_ID) {
    console.error("Set SPREADSHEET_ID in your .env file.");
    process.exit(1);
  }

  const TEST_PARCELS: VerifiedParcel[] = [
    {
      parcelId: "010-123456",
      fullAddress: "142 S Champion Ave, Columbus, OH 43205",
      houseNumber: "142",
      streetName: "S Champion Ave",
      city: "Columbus",
      state: "OH",
      zip: "43205",
      side: "even",
      lat: 39.9612,
      lng: -82.9871,
      ownerName: "SMITH JOHN",
      county: "Franklin",
      sourceListingAddress: "142 S Champion Ave, Columbus, OH",
      streetViewStatus: "ok",
      streetViewDate: "2023-08",
      streetViewPanoId: "abc123",
      streetViewUrl: "https://maps.googleapis.com/maps/api/streetview?pano=abc123",
      googleMapsUrl: "https://www.google.com/maps/search/?api=1&query=39.9612,-82.9871",
      needsReview: false,
    },
  ];

  (async () => {
    console.log("=".repeat(60));
    console.log("Sheets Writer — Phase 3 Test Run");
    console.log("=".repeat(60));

    const result = await upsertParcels(SPREADSHEET_ID!, TEST_PARCELS);
    console.log("\nResult:", result);
    console.log("=".repeat(60));
  })();
}
