// integrations/listing-csv-ingestor.ts
//
// Parses the Craigslist/CreXi/InvestorLift CSV export and produces
// classified AddressInput[] ready for street-parcel-resolver.ts
//
// Three address tiers coming out of the CSV:
//   Tier 1 — Full street address  → resolvable, goes to county GIS
//   Tier 2 — City/county only     → county extracted, no parcel lookup possible
//   Tier 3 — Out-of-state / junk  → dropped with a warning
//
// Usage:
//   const { resolvable, countyOnly, dropped } = await ingestListingsCsv(filePath);
//   const results = await processAddresses(resolvable, { defaultState: "OH" });

import fs from "fs";
import path from "path";
import { AddressInput } from "./street-parcel-resolver";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface ListingRow {
  id: string;
  url: string;
  source: string;
  title: string;
  price: number | null;
  rawAddress: string;
  location: string;       // city field from CSV
  county: string;         // county field from CSV
  sourceImage: string;    // source_image field from CSV
  bedrooms: number | null;
  bathrooms: number | null;
  squareFeet: number | null;
  dealScore: string;
  equityEstimate: number | null;
}

export interface ClassifiedAddress {
  listing: ListingRow;
  tier: 1 | 2 | 3;
  addressInput: AddressInput | null;   // null for tier 2 & 3
  county: string | null;               // extracted even for tier 2
  state: string | null;
  reason: string;                      // why it was classified this way
}

export interface IngestResult {
  resolvable: AddressInput[];          // tier 1 — ready for processAddresses()
  countyOnly: ClassifiedAddress[];     // tier 2 — county known, no street
  dropped: ClassifiedAddress[];        // tier 3 — out-of-state or unparseable
  all: ClassifiedAddress[];
}

// ── Ohio county name → FIPS / registry key ───────────────────────────────────
// Used to validate county strings extracted from messy rawAddress fields.

const OHIO_COUNTIES = new Set([
  "adams","allen","ashland","ashtabula","athens","auglaize","belmont","brown",
  "butler","carroll","champaign","clark","clermont","clinton","columbiana",
  "coshocton","crawford","cuyahoga","darke","defiance","delaware","erie",
  "fairfield","fayette","franklin","fulton","gallia","geauga","greene",
  "guernsey","hamilton","hancock","hardin","harrison","henry","highland",
  "hocking","holmes","huron","jackson","jefferson","knox","lake","lawrence",
  "licking","logan","lorain","lucas","madison","mahoning","marion","medina",
  "meigs","mercer","miami","monroe","montgomery","morgan","morrow","muskingum",
  "noble","ottawa","paulding","perry","pickaway","pike","portage","preble",
  "putnam","richland","ross","sandusky","scioto","seneca","shelby","stark",
  "summit","trumbull","tuscarawas","union","van wert","vinton","warren",
  "washington","wayne","williams","wood","wyandot",
]);

// City → county mapping for common Ohio cities (avoids needing Census for tier-2)
const OHIO_CITY_TO_COUNTY: Record<string, string> = {
  "toledo":        "Lucas",
  "cleveland":     "Cuyahoga",
  "columbus":      "Franklin",
  "cincinnati":    "Hamilton",
  "akron":         "Summit",
  "dayton":        "Montgomery",
  "youngstown":    "Mahoning",
  "canton":        "Stark",
  "lorain":        "Lorain",
  "hamilton":      "Butler",
  "springfield":   "Clark",
  "kettering":     "Montgomery",
  "elyria":        "Lorain",
  "parma":         "Cuyahoga",
  "euclid":        "Cuyahoga",
  "middletown":    "Butler",
  "newark":        "Licking",
  "johnstown":     "Licking",
  "granville":     "Licking",
  "pataskala":     "Licking",
  "mansfield":     "Richland",
  "mentor":        "Lake",
  "cleveland heights": "Cuyahoga",
  "lakewood":      "Cuyahoga",
  "strongsville":  "Cuyahoga",
  "warren":        "Trumbull",
  "fairfield":     "Butler",
  "lima":          "Allen",
  "findlay":       "Hancock",
  "cuyahoga falls":"Summit",
  // Knox County cities
  "mount vernon":  "Knox",
  "mt. vernon":    "Knox",
  "mt vernon":     "Knox",
  "centerburg":    "Knox",
  "croton":        "Knox",
  "hartford":      "Knox",
  "fredericktown": "Knox",
  "gambier":       "Knox",
  "howard":        "Knox",
  "danville":      "Knox",
  "utica":         "Knox",
  "martinsburg":   "Knox",
  "ashtabula":     "Ashtabula",
  "grafton":       "Lorain",
};

// ── CSV parser ────────────────────────────────────────────────────────────────
// Handles quoted fields with embedded commas/newlines.

function parseCsvLine(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let inQuotes = false;

  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (inQuotes && line[i + 1] === '"') {
        current += '"';
        i++;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (ch === "," && !inQuotes) {
      fields.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  fields.push(current);
  return fields;
}

function parseCsv(content: string): Record<string, string>[] {
  // Split on newlines but respect quoted fields that contain newlines
  const rows: string[][] = [];
  let currentRow = "";
  let inQuotes = false;

  for (let i = 0; i < content.length; i++) {
    const ch = content[i];
    if (ch === '"') {
      if (inQuotes && content[i + 1] === '"') {
        currentRow += '""';
        i++;
      } else {
        inQuotes = !inQuotes;
        currentRow += ch;
      }
    } else if ((ch === "\n" || ch === "\r") && !inQuotes) {
      if (currentRow.trim()) rows.push(parseCsvLine(currentRow));
      currentRow = "";
      if (ch === "\r" && content[i + 1] === "\n") i++; // skip \r\n pair
    } else {
      currentRow += ch;
    }
  }
  if (currentRow.trim()) rows.push(parseCsvLine(currentRow));

  if (rows.length === 0) return [];
  const headers = rows[0].map((h) => h.trim().replace(/^"|"$/g, ""));
  return rows.slice(1).map((row) => {
    const obj: Record<string, string> = {};
    headers.forEach((h, i) => {
      obj[h] = (row[i] ?? "").trim().replace(/^"|"$/g, "");
    });
    return obj;
  });
}

// ── Address classifier ────────────────────────────────────────────────────────

interface ParsedAddress {
  houseNumber: string | null;
  streetName: string | null;
  city: string | null;
  state: string | null;
  zip: string | null;
  county: string | null;
}

function parseRawAddress(raw: string, locationCity: string): ParsedAddress {
  // Normalise: strip emoji, collapse whitespace
  const cleaned = raw
    .replace(/[\u{1F000}-\u{1FFFF}]/gu, "")
    .replace(/\s+/g, " ")
    .trim();

  // Pattern: "576 E 152nd St, Cleveland, Cuyahoga County, OH 44110"
  // or:      "819 Nebraska Ave"  (no city in field)
  // or:      "Toledo, Lucas County, OH, 43623"  (no street number)
  // or:      "Lot 1 Hillside Drive, Wisconsin Dells"

  // Try to extract state (2-letter code), ignoring directionals like NW/SE
  const stateMatches = [...cleaned.matchAll(/\b([A-Z]{2})\b/g)].map(m => m[1]);
  const possibleStates = stateMatches.filter(s => !["NW", "NE", "SW", "SE"].includes(s));
  // Pick the last valid 2-letter word (since state is usually at the end)
  const state = possibleStates.length > 0 ? possibleStates[possibleStates.length - 1] : null;

  // Try to extract ZIP
  const zipMatch = cleaned.match(/\b(\d{5})(?:-\d{4})?\b/);
  const zip = zipMatch ? zipMatch[1] : null;

  // Try to extract county
  const countyMatch = cleaned.match(/\b([A-Za-z\s]+)\s+County\b/i);
  const county = countyMatch ? countyMatch[1].trim() : null;

  // Try to detect if there's a house number at the start
  const houseNumMatch = cleaned.match(/^(\d+[A-Za-z]?)\s+/);
  const hasHouseNumber = !!houseNumMatch;
  const houseNumber = houseNumMatch ? houseNumMatch[1] : null;

  // Extract street name — everything after house number up to first comma
  let streetName: string | null = null;
  if (hasHouseNumber) {
    const afterNum = cleaned.slice(houseNumMatch![0].length);
    const upToComma = afterNum.split(",")[0].trim();
    streetName = upToComma || null;
  }

  // City: use locationCity field if present, otherwise try to parse from address
  let city = locationCity?.trim() || null;
  if (!city && hasHouseNumber) {
    const parts = cleaned.split(",");
    if (parts.length >= 2) city = parts[1].trim();
  }
  if (!city && !hasHouseNumber) {
    // "Toledo, Lucas County, OH" — city is the first segment
    city = cleaned.split(",")[0].trim();
  }

  return { houseNumber, streetName, city, state, zip, county };
}

function classifyAddress(
  listing: ListingRow,
  targetState: string = "OH",
): ClassifiedAddress {
  const raw = listing.rawAddress?.trim() ?? "";

  if (!raw) {
    return {
      listing,
      tier: 3,
      addressInput: null,
      county: null,
      state: null,
      reason: "Empty rawAddress field",
    };
  }

  const parsed = parseRawAddress(raw, listing.location);

  // Tier 3: out-of-state
  if (parsed.state && parsed.state !== targetState) {
    return {
      listing,
      tier: 3,
      addressInput: null,
      county: parsed.county,
      state: parsed.state,
      reason: `Out-of-state: ${parsed.state}`,
    };
  }

  // Resolve county — use explicit CSV county first, then address string, then city lookup
  let resolvedCounty = (listing.county?.trim() || null) ?? parsed.county;
  if (!resolvedCounty && parsed.city) {
    resolvedCounty = OHIO_CITY_TO_COUNTY[parsed.city.toLowerCase()] ?? null;
  }

  // Tier 2: no house number — county-only listing (InvestorLift style)
  if (!parsed.houseNumber || !parsed.streetName) {
    return {
      listing,
      tier: 2,
      addressInput: null,
      county: resolvedCounty,
      state: parsed.state ?? targetState,
      reason: "No street number — county/city only address",
    };
  }

  // Tier 1: full address — build AddressInput
  const street = `${parsed.houseNumber} ${parsed.streetName}`;
  const city = parsed.city ?? listing.location ?? "Unknown";

  return {
    listing,
    tier: 1,
    addressInput: {
      id:          listing.id,
      street,
      city,
      state:       parsed.state ?? targetState,
      zip:         parsed.zip ?? undefined,
      county:      resolvedCounty ?? undefined,
      sourceImage: listing.sourceImage || undefined,
    },
    county: resolvedCounty,
    state:  parsed.state ?? targetState,
    reason: "Full street address",
  };
}

// ── Public API ─────────────────────────────────────────────────────────────────

export async function ingestListingsCsv(
  filePath: string,
  options: { targetState?: string } = {},
): Promise<IngestResult> {
  const targetState = options.targetState ?? "OH";

  console.log(`[ingestor] Reading CSV: ${path.basename(filePath)}`);
  const content = fs.readFileSync(filePath, "utf-8");
  const rows = parseCsv(content);
  console.log(`[ingestor] ${rows.length} row(s) parsed`);

  const listings: ListingRow[] = rows.map((r) => ({
    id:             r.id ?? r.propertyId ?? String(Math.random()),
    url:            r.url ?? "",
    source:         r.source ?? "",
    title:          r.title ?? "",
    price:          r.price ? Number(r.price) : null,
    rawAddress:     r.rawAddress ?? "",
    location:       r.location ?? "",
    county:         r.county ?? "",
    sourceImage:    r.source_image ?? "",
    bedrooms:       r.bedrooms ? Number(r.bedrooms) : null,
    bathrooms:      r.bathrooms ? Number(r.bathrooms) : null,
    squareFeet:     r.squareFeet ? Number(r.squareFeet) : null,
    dealScore:      r.dealScore ?? "",
    equityEstimate: r.equityEstimate ? Number(r.equityEstimate) : null,
  }));

  const all: ClassifiedAddress[] = listings.map((l) =>
    classifyAddress(l, targetState),
  );

  const resolvable  = all.filter((c) => c.tier === 1).map((c) => c.addressInput!);
  const countyOnly  = all.filter((c) => c.tier === 2);
  const dropped     = all.filter((c) => c.tier === 3);

  console.log(`[ingestor] Tier 1 (resolvable):  ${resolvable.length}`);
  console.log(`[ingestor] Tier 2 (county-only): ${countyOnly.length}`);
  console.log(`[ingestor] Tier 3 (dropped):     ${dropped.length}`);

  if (dropped.length > 0) {
    dropped.forEach((d) =>
      console.log(`[ingestor]   ✗ dropped "${d.listing.rawAddress}" — ${d.reason}`),
    );
  }
  if (countyOnly.length > 0) {
    countyOnly.forEach((c) =>
      console.log(`[ingestor]   ~ county-only "${c.listing.rawAddress}" → county=${c.county ?? "unknown"}`),
    );
  }

  return { resolvable, countyOnly, dropped, all };
}

// ── Standalone test ───────────────────────────────────────────────────────────

if (require.main === module) {
  try { require("dotenv").config(); } catch {}

  const csvPath = process.argv[2];
  if (!csvPath) {
    console.error("Usage: npx tsx integrations/listing-csv-ingestor.ts <path-to-csv>");
    process.exit(1);
  }

  (async () => {
    const result = await ingestListingsCsv(csvPath, { targetState: "OH" });

    console.log("\n── Resolvable addresses ──────────────────────────────");
    result.resolvable.forEach((a) =>
      console.log(`  [${a.id}] ${a.street}, ${a.city}, ${a.state} | county=${a.county ?? "tbd"}`),
    );

    console.log("\n── County-only (no parcel lookup possible) ───────────");
    result.countyOnly.forEach((c) =>
      console.log(`  [${c.listing.id}] ${c.listing.rawAddress} | county=${c.county ?? "unknown"}`),
    );

    console.log("\n── Dropped ───────────────────────────────────────────");
    result.dropped.forEach((d) =>
      console.log(`  [${d.listing.id}] ${d.listing.rawAddress} — ${d.reason}`),
    );
  })();
}