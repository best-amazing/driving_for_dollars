// integrations/street-view-verifier.ts
//
// Phase 2 — Google Street View Metadata verification
//
// Uses the Street View Static API *metadata* endpoint (not the image endpoint)
// so we only pay for the lookup, not image pixels.
//
// Metadata endpoint:
//   GET https://maps.googleapis.com/maps/api/streetview/metadata
//   ?location=<address>
//   &key=<GOOGLE_MAPS_API_KEY>
//
// Possible status values in the response:
//   "OK"           — Street View imagery exists at this location
//   "ZERO_RESULTS" — No imagery found (address gap, vacant lot, etc.)
//   "NOT_FOUND"    — Address couldn't be located
//   "UNKNOWN_ERROR"— Server-side error; worth retrying once
//
// Pricing note (as of 2025):
//   Metadata calls are FREE — $0 per request, no credit consumed.
//   Image calls ($7 per 1,000) are NOT made here.
//   https://developers.google.com/maps/documentation/streetview/usage-and-billing

import axios from "axios";
import { Parcel } from "./street-parcel-resolver";

// ── Types ─────────────────────────────────────────────────────────────────────

export type StreetViewStatus =
  | "ok"           // imagery confirmed
  | "zero_results" // no imagery — flag for manual review
  | "not_found"    // address unrecognised by Maps
  | "error";       // network / API error

export interface VerifiedParcel extends Parcel {
  streetViewStatus: StreetViewStatus;
  streetViewDate: string | null;   // e.g. "2023-06" — when Google last imaged it
  streetViewPanoId: string | null; // Google's internal panorama ID
  streetViewUrl: string | null;
  googleMapsUrl: string;
  needsReview: boolean;            // true when status !== "ok"
}

export interface VerificationSummary {
  total: number;
  confirmed: number;       // status === "ok"
  flagged: number;         // status !== "ok"
  flaggedAddresses: string[];
}

// ── Constants ─────────────────────────────────────────────────────────────────

const STREETVIEW_METADATA_URL =
  "https://maps.googleapis.com/maps/api/streetview/metadata";

// Stay well under Google's per-second rate limit.
// Free tier allows ~100 req/sec but be a good citizen.
const DELAY_BETWEEN_REQUESTS_MS = 100;

const REQUEST_TIMEOUT_MS = 10_000;

// ── Helpers ───────────────────────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function buildStreetViewUrl(parcel: Parcel, panoId: string | null): string | null {
  if (panoId) {
    const cbll = (parcel.lat !== null && parcel.lng !== null)
      ? `${parcel.lat},${parcel.lng}`
      : "0,0";
    return `https://www.google.com/maps?q=&layer=c&cbll=${cbll}&panoid=${panoId}`;
  }
  if (parcel.lat !== null && parcel.lng !== null) {
    return `https://maps.google.com/maps?q=&layer=c&cbll=${parcel.lat},${parcel.lng}`;
  }
  return null;
}

function buildMapsUrl(parcel: Parcel): string {
  if (parcel.lat !== null && parcel.lng !== null) {
    return `https://www.google.com/maps/search/?api=1&query=${parcel.lat},${parcel.lng}`;
  }
  // Use clean structured components, not the raw ODNR fullAddress which may
  // contain trailing city/zip fragments like "FAIRGROUNDS RD MOUNT VERNON 43050"
  const parts = [parcel.houseNumber, parcel.streetName, parcel.city, parcel.state, parcel.zip]
    .map((p) => p?.trim())
    .filter(Boolean)
    .join(" ");
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(parts)}`;
}

function getApiKey(): string {
  const key = process.env.GOOGLE_MAPS_API_KEY;
  if (!key) {
    throw new Error(
      "GOOGLE_MAPS_API_KEY is not set. " +
      "Add it to your .env file — get it from " +
      "console.cloud.google.com → APIs & Services → Credentials.",
    );
  }
  return key;
}

// ── Core: single-address metadata lookup ──────────────────────────────────────

interface StreetViewMetadataResponse {
  status: string;
  date?: string;       // "YYYY-MM"
  location?: { lat: number; lng: number };
  pano_id?: string;
  copyright?: string;
}

async function fetchStreetViewMetadata(
  address: string,
  apiKey: string,
): Promise<{ status: StreetViewStatus; date: string | null; panoId: string | null }> {
  let result;

  try {
    result = await axios.get<StreetViewMetadataResponse>(STREETVIEW_METADATA_URL, {
      params: {
        location: address,
        key: apiKey,
        source: "outdoor", // prefer outdoor imagery (skip indoor Google Business photos)
      },
      timeout: REQUEST_TIMEOUT_MS,
      validateStatus: () => true,
    });
  } catch (err) {
    console.warn(`[street-view]   Network error for "${address}":`, err);
    return { status: "error", date: null, panoId: null };
  }

  if (result.status !== 200) {
    console.warn(`[street-view]   HTTP ${result.status} for "${address}"`);
    return { status: "error", date: null, panoId: null };
  }

  const body = result.data;
  const rawStatus = (body.status ?? "UNKNOWN_ERROR").toUpperCase();

  let status: StreetViewStatus;
  switch (rawStatus) {
    case "OK":
      status = "ok";
      break;
    case "ZERO_RESULTS":
      status = "zero_results";
      break;
    case "NOT_FOUND":
      status = "not_found";
      break;
    default:
      status = "error";
  }

  return {
    status,
    date: body.date ?? null,
    panoId: body.pano_id ?? null,
  };
}

// ── Public API ─────────────────────────────────────────────────────────────────
//
// verifyParcels()
//   Takes the Parcel[] output from Phase 1 (street-parcel-resolver) and
//   enriches each with a Street View status. Parcels with status !== "ok"
//   have needsReview = true so the ops team can check them manually.
//
// verifyStreetCompleteness()
//   Higher-level wrapper that calls verifyParcels() and returns a
//   VerificationSummary alongside the enriched list.

export async function verifyParcels(
  parcels: Parcel[],
  options: { verbose?: boolean } = {},
): Promise<VerifiedParcel[]> {
  if (parcels.length === 0) return [];

  const apiKey = getApiKey();
  const verified: VerifiedParcel[] = [];

  console.log(`[street-view] Verifying ${parcels.length} parcel(s) via Street View metadata...`);

  for (let i = 0; i < parcels.length; i++) {
    const parcel = parcels[i];

    // Use coordinates if available (more precise), otherwise fall back to address string
    const location =
      parcel.lat !== null && parcel.lng !== null
        ? `${parcel.lat},${parcel.lng}`
        : parcel.fullAddress;

    if (options.verbose) {
      process.stdout.write(
        `[street-view]   [${i + 1}/${parcels.length}] ${parcel.fullAddress} … `,
      );
    }

    const { status, date, panoId } = await fetchStreetViewMetadata(location, apiKey);

    if (options.verbose) {
      console.log(status.toUpperCase());
    }

    verified.push({
      ...parcel,
      streetViewStatus: status,
      streetViewDate: date,
      streetViewPanoId: panoId,
      streetViewUrl: buildStreetViewUrl(parcel, panoId),
      googleMapsUrl: buildMapsUrl(parcel),
      needsReview: status !== "ok",
    });

    // Throttle requests — don't hammer the API
    if (i < parcels.length - 1) {
      await sleep(DELAY_BETWEEN_REQUESTS_MS);
    }
  }

  const confirmed = verified.filter((v) => v.streetViewStatus === "ok").length;
  const flagged = verified.length - confirmed;
  console.log(
    `[street-view] Done — ${confirmed} confirmed, ${flagged} flagged for review`,
  );

  return verified;
}

export async function verifyStreetCompleteness(
  parcels: Parcel[],
  options: { verbose?: boolean } = {},
): Promise<{ verified: VerifiedParcel[]; summary: VerificationSummary }> {
  const verified = await verifyParcels(parcels, options);

  const flaggedAddresses = verified
    .filter((v) => v.needsReview)
    .map((v) => v.fullAddress);

  const summary: VerificationSummary = {
    total: verified.length,
    confirmed: verified.filter((v) => v.streetViewStatus === "ok").length,
    flagged: flaggedAddresses.length,
    flaggedAddresses,
  };

  if (flaggedAddresses.length > 0) {
    console.log("\n[street-view] ⚠ Addresses flagged for manual review:");
    flaggedAddresses.forEach((addr) => console.log(`    • ${addr}`));
  }

  return { verified, summary };
}

// ── Standalone test run ───────────────────────────────────────────────────────
//
// Run with:  npx ts-node src/integrations/street-view-verifier.ts
//
// Requires GOOGLE_MAPS_API_KEY in your .env.

if (require.main === module) {
  // dotenv support — loads .env if present
  try { require("dotenv").config(); } catch {}

  const TEST_PARCELS: Parcel[] = [
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
    },
    {
      parcelId: "010-999999",
      fullAddress: "9999 Nonexistent St, Columbus, OH 43215",
      houseNumber: "9999",
      streetName: "Nonexistent St",
      city: "Columbus",
      state: "OH",
      zip: "43215",
      side: "odd",
      lat: null,
      lng: null,
      ownerName: null,
      county: "Franklin",
      sourceListingAddress: "9999 Nonexistent St, Columbus, OH",
    },
  ];

  (async () => {
    console.log("=".repeat(60));
    console.log("Street View Verifier — Phase 2 Test Run");
    console.log("=".repeat(60));

    const { verified, summary } = await verifyStreetCompleteness(TEST_PARCELS, {
      verbose: true,
    });

    console.log("\nSummary:");
    console.log(`  Total:     ${summary.total}`);
    console.log(`  Confirmed: ${summary.confirmed}`);
    console.log(`  Flagged:   ${summary.flagged}`);

    console.log("\nDetailed results:");
    verified.forEach((v) => {
      console.log(
        `  ${v.fullAddress}\n` +
        `    status=${v.streetViewStatus}  date=${v.streetViewDate ?? "n/a"}  needsReview=${v.needsReview}`,
      );
    });

    console.log("\n" + "=".repeat(60));
    console.log("Test run complete");
  })();
}
