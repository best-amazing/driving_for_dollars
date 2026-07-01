// integrations/street-parcel-resolver.ts

import axios from "axios";
import FormData from "form-data";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface AddressInput {
  id: string;
  street: string;
  city: string;
  state: string;
  zip?: string;
  county?: string;
  sourceImage?: string;
}

export interface GeocodedAddress {
  id: string;
  inputAddress: string;
  matchedAddress: string | null;
  lat: number | null;
  lng: number | null;
  county: string | null;
  countyFips: string | null;
  state: string | null;
  streetName: string | null;
  geocodeStatus: "match" | "no-match" | "tie" | "error" | "skipped";
  sourceImage?: string;
  city?: string | null;
  zip?: string | null;
}

export interface Parcel {
  parcelId: string;
  fullAddress: string;
  houseNumber: string;
  streetName: string;
  city: string;
  state: string;
  zip: string;
  side: "odd" | "even" | "unknown";
  lat: number | null;
  lng: number | null;
  ownerName: string | null;
  county: string;
  sourceListingAddress: string;
  sourceImage?: string;
}

export interface StreetParcelResult {
  sourceAddress: string;
  streetName: string;
  county: string;
  totalParcels: number;
  oddSide: Parcel[];
  evenSide: Parcel[];
  all: Parcel[];
  error?: string;
  skipped?: boolean;
}

// ── County GIS Registry ───────────────────────────────────────────────────────

interface CountyGISConfig {
  name: string;
  fips: string;
  queryUrl: string;
  fields: {
    parcelId: string;
    siteAddress: string;
    houseNum: string;
    streetName: string;
    city: string;
    state: string;
    zip: string;
    ownerName: string;
    centroidX: string;
    centroidY: string;
  };
  buildWhereClause: (streetName: string) => string;
}

const ODNR_URL = "https://services2.arcgis.com/MlJ0G8iWUyC7jAmu/arcgis/rest/services/OhioStatewidePacels_full_view/FeatureServer/0/query";

// NOTE: We intentionally leave MailCity and MailZip out of ODNR_FIELDS.
// The ODNR statewide layer stores the owner's *mailing* address in those
// fields, which is often a completely different city/state/zip from the
// physical parcel location. We derive city and zip from the Census geocoder
// result (authoritative) or the original input instead.
const ODNR_FIELDS: CountyGISConfig["fields"] = {
  parcelId:    "LocalParcelID",
  siteAddress: "SitusAddressAll",
  houseNum:    "",
  streetName:  "",
  city:        "",         // intentionally blank — use inputCity from Census
  state:       "",
  zip:         "",         // intentionally blank — use inputZip from Census
  ownerName:   "",
  centroidX:   "",
  centroidY:   "",
};

function odnrWhere(countyName: string) {
  return (street: string) =>
    `County='${countyName}' AND UPPER(SitusAddressAll) LIKE UPPER('%${street.replace(/'/g, "''")}%')`;
}

const COUNTY_GIS_REGISTRY: Record<string, CountyGISConfig> = {
  franklin: {
    name: "Franklin",
    fips: "39049",
    queryUrl: "https://gis.franklincountyohio.gov/hosting/rest/services/ParcelFeatures/Parcel_Features/MapServer/0/query",
    fields: {
      parcelId:    "PARCELID",
      siteAddress: "SITEADDRESS",
      houseNum:    "",
      streetName:  "",
      city:        "",
      state:       "",
      zip:         "ZIPCD",
      ownerName:   "OWNERNME1",
      centroidX:   "",
      centroidY:   "",
    },
    buildWhereClause: (street) =>
      `UPPER(SITEADDRESS) LIKE UPPER('%${street.replace(/'/g, "''")}%')`,
  },
  knox:      { name: "Knox",      fips: "39083", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Knox")      },
  licking:   { name: "Licking",   fips: "39089", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Licking")   },
  lucas:     { name: "Lucas",     fips: "39095", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Lucas")     },
  cuyahoga:  { name: "Cuyahoga",  fips: "39035", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Cuyahoga")  },
  summit:    { name: "Summit",    fips: "39153", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Summit")    },
  ashtabula: { name: "Ashtabula", fips: "39007", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Ashtabula") },
  clark:     { name: "Clark",     fips: "39023", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Clark")     },
  lorain:    { name: "Lorain",    fips: "39093", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Lorain")    },
  mahoning:  { name: "Mahoning",  fips: "39099", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Mahoning")  },
  delaware:  { name: "Delaware",  fips: "39041", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Delaware")  },
  fairfield: { name: "Fairfield", fips: "39045", queryUrl: ODNR_URL, fields: ODNR_FIELDS, buildWhereClause: odnrWhere("Fairfield") },
};

const FIPS_TO_KEY: Record<string, string> = Object.fromEntries(
  Object.entries(COUNTY_GIS_REGISTRY).map(([k, v]) => [v.fips, k]),
);

const COUNTY_FIPS_MAP: Record<string, string> = Object.fromEntries(
  Object.entries(COUNTY_GIS_REGISTRY).map(([, v]) => [v.fips, v.name]),
);

// ── Constants ─────────────────────────────────────────────────────────────────

const CENSUS_HOST        = "geocoding.geo.census.gov";
const CENSUS_BATCH_PATH  = "/geocoder/geographies/addressbatch";
const CENSUS_BENCHMARK   = "Public_AR_Current";
const CENSUS_VINTAGE     = "Current_Current";
const REQUEST_TIMEOUT_MS = 90_000;

const MAX_PARCEL_RESULT_THRESHOLD = 300;
const MIN_QUERY_TOKEN_LENGTH = 2;

// Ohio ZIP code range: 43001–45999
const OH_ZIP_MIN = 43001;
const OH_ZIP_MAX = 45999;

// ── City → ZIP fallback ───────────────────────────────────────────────────────
// Used when Census geocoding fails (e.g. dummy house numbers) and ODNR doesn't
// embed a ZIP in SitusAddressAll. Keys are lowercase, trimmed city names.
const CITY_ZIP_LOOKUP: Record<string, string> = {
  // Knox County
  "centerburg":     "43011",
  "croton":         "43013",
  "hartford":       "43013",
  "mount vernon":   "43050",
  "mt. vernon":     "43050",
  "mt vernon":      "43050",
  "fredericktown":  "43019",
  "gambier":        "43022",
  "howard":         "43028",
  "danville":       "43014",
  "utica":          "43080",
  "martinsburg":    "43037",
  // Licking County
  "johnstown":      "43031",
  "newark":         "43055",
  "granville":      "43023",
  "pataskala":      "43062",
  "heath":          "43056",
};

const OCR_NOISE_PATTERNS: RegExp[] = [
  /\b(MPH|BRE|paolo)\b/i,          // corrupted words
  /\b[IVX]{2,}\b/,                  // roman numerals (II, III, IV...)
  /\bane\b/i,                        // "Nichols ane" — OCR for "Lane"
  /\bturges\b/i,                     // "JS turges" — OCR for "Sturges"
  /\bownship\b/i,                    // "MPH ownship" — OCR for "Township"
  // Bare 1-2 letter prefix pair with NOTHING else after — e.g. "JS" alone.
  // Does NOT fire on "R E Houck" or "T J Evans" because those have a
  // real word following the initials.
  /^[A-Z]{1,2}\s+[A-Z]{1,2}$/i,    // entire name is just two short tokens
];

const COMMON_STREET_NAMES = new Set([
  "main", "church", "mill", "hollow", "spring", "oak", "maple",
  "elm", "center", "broadway", "washington", "lincoln", "jefferson",
  "cleveland", "park", "lake", "river", "ridge", "hill", "valley",
  "school", "high", "cherry", "walnut", "chestnut", "pine", "cedar",
]);

// Known Ohio city name aliases / abbreviations that appear in ODNR raw addresses.
// Used to strip trailing city fragments from the parsed street name.
// All entries lowercase; the regex built from this is applied case-insensitively.
const OHIO_CITY_FRAGMENTS = [
  "centerburg", "mount vernon", "mt\\. vernon", "mt vernon",
  "columbus", "gambier", "howard", "danville", "fredericktown",
  "utica", "marengo", "sunbury", "johnstown", "newark", "akron",
  "cleveland", "toledo", "cincinnati", "dayton", "youngstown",
];

// Pre-built regex that matches a trailing " CITYNAME OPTIONALZIP" at end of string
const TRAILING_CITY_ZIP_RE = new RegExp(
  `\\s+(${OHIO_CITY_FRAGMENTS.join("|")})(\\s+\\d{5})?\\s*$`,
  "i",
);

// ── Helpers ───────────────────────────────────────────────────────────────────

function extractStreetName(fullStreet: string): string {
  return fullStreet
    .replace(/^\d+[A-Za-z]?\s+/, "")
    .replace(/\bSt\.?\b/gi,   "Street")
    .replace(/\bAve\.?\b/gi,  "Avenue")
    .replace(/\bBlvd\.?\b/gi, "Boulevard")
    .replace(/\bDr\.?\b/gi,   "Drive")
    .replace(/\bRd\.?\b/gi,   "Road")
    .replace(/\bLn\.?\b/gi,   "Lane")
    .replace(/\bCt\.?\b/gi,   "Court")
    .replace(/\bPl\.?\b/gi,   "Place")
    .replace(/\bPkwy\.?\b/gi, "Parkway")
    .replace(/\bTrl\.?\b/gi,  "Trail")
    .replace(/[-']/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/**
 * Strips trailing city name and/or ZIP that ODNR sometimes appends to
 * SitusAddressAll when the address has no comma-separated components.
 *
 * Examples:
 *   "CROTON RD  CENTERBURG 43011"  → "CROTON RD"
 *   "CONCORD ST S  MT. VERNON,"    → "CONCORD ST S"
 *   "PROSPECT ST EXT   MOUNT VERNON 43050" → "PROSPECT ST EXT"
 *   "CHAPEL RD  DANVILLE 43014"    → "CHAPEL RD"
 */
function stripTrailingCityZip(streetFragment: string): string {
  // 1. Remove trailing punctuation/commas
  let s = streetFragment.replace(/[,;]+\s*$/, "").trim();

  // 2. Strip known city + optional zip at the end
  s = s.replace(TRAILING_CITY_ZIP_RE, "").trim();

  // 3. Catch any orphaned 5-digit zip that remains at the end
  s = s.replace(/\s+\d{5}(?:-\d{4})?\s*$/, "").trim();

  // 4. Collapse multiple internal spaces left by the removal
  s = s.replace(/\s{2,}/g, " ").trim();

  return s;
}

/**
 * Validates that a string looks like a valid Ohio ZIP code.
 * Returns the ZIP if valid, empty string otherwise.
 */
function validateOhioZip(raw: string): string {
  const match = raw.match(/\b(\d{5})\b/);
  if (!match) return "";
  const n = parseInt(match[1], 10);
  return n >= OH_ZIP_MIN && n <= OH_ZIP_MAX ? match[1] : "";
}

function extractCityZipFromODNR(rawStreet: string): { city: string; zip: string } {
  const match = rawStreet.match(
    new RegExp(
      `\\s+(${OHIO_CITY_FRAGMENTS.join("|")})(\\s+(\\d{5}))?\\s*$`,
      "i"
    )
  );
  return {
    city: match ? match[1].trim() : "",
    zip:  match ? validateOhioZip(match[3] ?? "") : "",
  };
}

/**
 * Builds a clean Google Maps search URL from structured components
 * rather than the raw fullAddress string (which may contain dirty ODNR data).
 */
function buildGoogleMapsUrl(
  houseNumber: string,
  streetName: string,
  city: string,
  state: string,
  zip: string,
): string {
  const parts = [houseNumber, streetName, city, state, zip]
    .map((p) => p.trim())
    .filter(Boolean)
    .join(" ");
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(parts)}`;
}

function registryKey(county: string): string {
  return county.toLowerCase().replace(/\s+county$/i, "").trim();
}

function ocrNoiseReason(streetName: string): string | null {
  for (const pattern of OCR_NOISE_PATTERNS) {
    if (pattern.test(streetName)) {
      return `matches noise pattern ${pattern}`;
    }
  }
  const words = streetName.trim().split(/\s+/).filter((w) => w.length >= 2);
  if (words.length > 4) {
    return `too many tokens (${words.length}) — likely merged OCR`;
  }
  return null;
}

/**
 * Builds the LIKE-query string sent to the county GIS.
 *
 * Key insight: ODNR's SitusAddressAll stores the *full* road-type suffix
 * (e.g. "REIGN WAY", "WINDY HOLLOW RD", "DERRINGER CT").  Stripping the
 * suffix before querying causes misses for short or ambiguous names —
 * "%REIGN%" may match nothing while "%REIGN WAY%" matches exactly.
 *
 * Strategy:
 *  1. Strip house number and fix punctuation/dashes
 *  2. Normalise road-type abbreviations to their canonical short form
 *     (so "Rd" and "Road" both become "RD" in the query)
 *  3. For COMMON names (Main, Church…) keep the directional to stay specific
 *  4. For all other names strip directionals — the core+suffix is specific enough
 *  5. Normalise ordinal suffixes (152nd → 152)
 */
function buildGisQueryString(streetName: string): string {
  // Step 1: strip house number and fix punctuation
  let s = streetName
    .replace(/^\d+[A-Za-z]?\s+/, "")
    .replace(/[-'.]/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim();

  // Step 2: normalise road-type suffixes to short canonical forms.
  // We KEEP the suffix in the query — ODNR stores it and it helps precision.
  s = s
    .replace(/\bStreet\b/gi,    "ST")
    .replace(/\bAvenue\b/gi,    "AVE")
    .replace(/\bBoulevard\b/gi, "BLVD")
    .replace(/\bDrive\b/gi,     "DR")
    .replace(/\bRoad\b/gi,      "RD")
    .replace(/\bLane\b/gi,      "LN")
    .replace(/\bCourt\b/gi,     "CT")
    .replace(/\bPlace\b/gi,     "PL")
    .replace(/\bParkway\b/gi,   "PKWY")
    .replace(/\bTrail\b/gi,     "TRL")
    .replace(/\bWay\b/gi,       "WAY")
    .replace(/\bCircle\b/gi,    "CIR")
    .replace(/\s{2,}/g, " ")
    .trim();

  // Step 3: check if core name (without suffix + directionals) is common
  const coreOnly = s
    .replace(/\b(ST|AVE|BLVD|DR|RD|LN|CT|PL|PKWY|TRL|WAY|CIR)\b/gi, "")
    .replace(/\b(NW|NE|SW|SE|N|S|E|W)\b/gi, "")
    .replace(/\s{2,}/g, " ")
    .trim()
    .toLowerCase();

  const isCommon = COMMON_STREET_NAMES.has(coreOnly);

  // Step 4: strip directionals only for non-common names
  if (!isCommon) {
    s = s
      .replace(/\b(NW|NE|SW|SE|N|S|E|W)\b/gi, "")
      .replace(/\s{2,}/g, " ")
      .trim();
  }

  // Step 5: strip ordinal suffixes (152nd → 152)
  s = s.replace(/\b(\d+)(st|nd|rd|th)\b/gi, "$1").trim();

  return s;
}

// ── Census batch geocode ──────────────────────────────────────────────────────

export async function batchGeocodeAddresses(
  addresses: AddressInput[],
): Promise<GeocodedAddress[]> {
  if (addresses.length === 0) return [];

  const csvRows = addresses
    .map((a) => {
      const zip    = a.zip ?? "";
      const street = `"${a.street.replace(/"/g, '""')}"`;
      const city   = `"${a.city.replace(/"/g, '""')}"`;
      return `${a.id},${street},${city},${a.state},${zip}`;
    })
    .join("\r\n");

  console.log(`[resolver] Census batch geocode: ${addresses.length} address(es)`);

  const form = new FormData();
  form.append("benchmark", CENSUS_BENCHMARK);
  form.append("vintage",   CENSUS_VINTAGE);
  form.append("format",    "csv");
  form.append("addressFile", Buffer.from(csvRows, "utf-8"), {
    filename: "addresses.csv",
    contentType: "text/plain",
  });

  let result: any;
  try {
    result = await axios.post(`https://${CENSUS_HOST}${CENSUS_BATCH_PATH}`, form, {
      headers: form.getHeaders(),
      timeout: REQUEST_TIMEOUT_MS,
      validateStatus: () => true,
      family: 4,
    });
  } catch (err) {
    console.error("[resolver] Census network error:", (err as any).message);
  }

  const errorRow = (a: AddressInput): GeocodedAddress => ({
    id: a.id,
    inputAddress: `${a.street}, ${a.city}, ${a.state}`,
    matchedAddress: null, lat: null, lng: null,
    county: null, countyFips: null, state: null, streetName: null,
    geocodeStatus: "error",
  });

  if (!result) return addresses.map(errorRow);

  const bodyText = typeof result.data === "string" ? result.data : String(result.data);
  console.log(`[resolver] Census: HTTP ${result.status}  body=${bodyText.length}ch`);

  if (result.status !== 200) return addresses.map(errorRow);

  const lines    = bodyText.trim().split("\n").filter(Boolean);
  const geocoded: GeocodedAddress[] = [];

  for (const line of lines) {
    const cols = line.split(",").map((c: string) => c.trim().replace(/^"|"$/g, ""));
    const [id, inputAddress, matchStatus, , matchedAddress, coordinates, , , stateCode, countyCode] = cols;

    const status: GeocodedAddress["geocodeStatus"] =
      matchStatus?.toLowerCase() === "match" ? "match"
      : matchStatus?.toLowerCase() === "tie"  ? "tie"
      : "no-match";

    let lat: number | null = null, lng: number | null = null;
    if (coordinates?.includes(",")) {
      const [lngStr, latStr] = coordinates.split(",");
      lng = parseFloat(lngStr); lat = parseFloat(latStr);
    }

    const countyFips = stateCode && countyCode ? `${stateCode}${countyCode}` : null;
    const county     = countyFips ? (COUNTY_FIPS_MAP[countyFips] ?? `FIPS-${countyFips}`) : null;

    let streetName: string | null = null;
    let city: string | null = null;
    let zip: string | null = null;
    if (matchedAddress) {
      const parts = matchedAddress.split(",").map((p: string) => p.trim());
      if (parts[0]) streetName = extractStreetName(parts[0]);
      if (parts[1]) city = parts[1];

      // Census format: "123 MAIN ST, CITY, OH 43011" — state+zip are in parts[2], NOT parts[3]
      if (parts[2]) {
        const stateZipMatch = parts[2].match(/([A-Z]{2})\s+(\d{5})/);
        if (stateZipMatch) {
          zip = validateOhioZip(stateZipMatch[2]) || null;
        }
      }
      // Fallback for older Census format: "123 MAIN ST, CITY, STATE, ZIP"
      if (!zip && parts[3]) {
        zip = validateOhioZip(parts[3]) || null;
      }

      console.log(`[census-parse] id=${id} matched="${matchedAddress}" → city=${city} zip=${zip}`);
    } else {
      console.log(`[census-parse] id=${id} status=${status} — no matched address`);
    }

    const inputA = addresses.find((a) => a.id === id);
    geocoded.push({
      id,
      inputAddress: inputAddress ?? (inputA ? `${inputA.street}, ${inputA.city}, ${inputA.state}` : ""),
      matchedAddress: matchedAddress || null,
      lat, lng, county, countyFips,
      state: stateCode ?? null,
      streetName,
      geocodeStatus: status,
      sourceImage: inputA?.sourceImage,
      city: city || (inputA?.city ?? null),
      zip: zip || (inputA?.zip ? validateOhioZip(inputA.zip) : null) || null,
    });
  }

  const matched   = geocoded.filter((g) => g.geocodeStatus === "match" || g.geocodeStatus === "tie").length;
  const withZip    = geocoded.filter((g) => g.zip).length;
  const withCity   = geocoded.filter((g) => g.city).length;
  console.log(`[resolver] Census: ${matched}/${geocoded.length} matched | ${withZip} have zip | ${withCity} have city`);
  return geocoded;
}

// ── County GIS parcel query ───────────────────────────────────────────────────

export async function queryCountyParcels(
  streetName: string,
  county: string,
  sourceAddress: string,
  sourceImage?: string,
  inputCity?: string | null,
  inputZip?: string | null,
): Promise<StreetParcelResult> {
  const key    = registryKey(county);
  const config = COUNTY_GIS_REGISTRY[key];

  const errorResult = (msg: string): StreetParcelResult => ({
    sourceAddress, streetName, county,
    totalParcels: 0, oddSide: [], evenSide: [], all: [], error: msg,
  });

  const skipResult = (msg: string): StreetParcelResult => ({
    sourceAddress, streetName, county,
    totalParcels: 0, oddSide: [], evenSide: [], all: [],
    skipped: true, error: msg,
  });

  if (!config) {
    return errorResult(`No GIS config for county "${county}". Add it to COUNTY_GIS_REGISTRY.`);
  }

  const noiseReason = ocrNoiseReason(streetName);
  if (noiseReason) {
    console.warn(`[resolver] ⚠ Skipping "${streetName}" — ${noiseReason}`);
    return skipResult(`Skipped: ${noiseReason}`);
  }

  const isAddressParsed = config.queryUrl.includes("OhioStatewidePacels")
    || config.queryUrl.includes("Parcel_Features");

  const normalisedForGIS = isAddressParsed
    ? buildGisQueryString(streetName)
    : extractStreetName(streetName);

  if (!normalisedForGIS || normalisedForGIS.length < MIN_QUERY_TOKEN_LENGTH) {
    console.warn(`[resolver] ⚠ Skipping "${streetName}" — query too short after normalisation: "${normalisedForGIS}"`);
    return skipResult(`Query too short after normalisation: "${normalisedForGIS}"`);
  }

  const where = config.buildWhereClause(normalisedForGIS);
  const f     = config.fields;

  console.log(`[resolver] ${config.name} County GIS: street="${normalisedForGIS}"`);

  let result: any;
  try {
    result = await axios.get(config.queryUrl, {
      params: {
        where,
        outFields: Object.values(f).filter(Boolean).join(","),
        returnGeometry: "false",
        f: "json",
        resultRecordCount: "2000",
      },
      headers: {
        Accept: "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; ODoD-pipeline/1.0)",
      },
      timeout: REQUEST_TIMEOUT_MS,
      validateStatus: () => true,
      family: 4,
    });
  } catch (err) {
    return errorResult(`${config.name} County GIS: network failure`);
  }

  if (!result || result.status !== 200) {
    return errorResult(`${config.name} County GIS: HTTP ${result?.status ?? "unknown"}`);
  }

  const json = typeof result.data === "string" ? JSON.parse(result.data) : result.data;
  if (json.error) {
    return errorResult(`${config.name} GIS error: ${json.error.message ?? JSON.stringify(json.error)}`);
  }

  const features: any[] = json.features ?? [];
  if (features.length === 0) {
    return errorResult(`No parcels found for "${normalisedForGIS}" in ${config.name} County`);
  }

  console.log(`[resolver] ${config.name}: ${features.length} parcel(s) returned`);

  if (features.length >= MAX_PARCEL_RESULT_THRESHOLD) {
    console.warn(
      `[resolver] ⚠ "${normalisedForGIS}" returned ${features.length} parcels — ` +
      `exceeds threshold of ${MAX_PARCEL_RESULT_THRESHOLD}. ` +
      `Too broad to be useful; skipping. Try a more specific query.`,
    );
    return skipResult(
      `Too many results (${features.length} ≥ ${MAX_PARCEL_RESULT_THRESHOLD}) — query too broad`,
    );
  }

  // Validated Census/input city and zip — these are authoritative over ODNR mail fields
  const authoritativeCity = inputCity?.trim() || "";
  const authoritativeZip  = validateOhioZip(inputZip ?? "");

  console.log(
    `[parcel-map] street="${streetName}" county=${config.name} ` +
    `inputCity=${inputCity ?? "—"} inputZip=${inputZip ?? "—"} ` +
    `→ authoritativeCity="${authoritativeCity}" authoritativeZip="${authoritativeZip}"`
  );

  const parcels: Parcel[] = features
    .map((feat: any): Parcel | null => {
      const a        = feat.attributes ?? {};
      const fullAddr = String(a[f.siteAddress] ?? "").trim();
      const parcelId = String(a[f.parcelId]   ?? "").trim();

      if (!fullAddr || !parcelId) return null;

      const addrMatch = fullAddr.match(/^(\d+)\s+(.+)$/);
      const houseNum  = addrMatch ? addrMatch[1].trim() : "";
      // rawStreet is what comes after the house number in SitusAddressAll —
      // it may contain trailing " CENTERBURG 43011" or " MT. VERNON," etc.
      const rawStreet = addrMatch ? addrMatch[2].trim() : fullAddr;

      if (!houseNum || houseNum === "0") return null;

      const houseInt = parseInt(houseNum, 10);
      const side: Parcel["side"] = isNaN(houseInt) ? "unknown"
        : houseInt % 2 === 0 ? "even" : "odd";

      // ── Clean the street name from the raw ODNR situs address ──────────────
      // SitusAddressAll for ODNR often looks like:
      //   "2106  CROTON RD  CENTERBURG 43011"
      //   "1  CONCORD ST S  MT. VERNON,"
      // After splitting off the house number, rawStreet still contains the
      // trailing city/zip fragment. Strip it.
      const cleanStreet = stripTrailingCityZip(rawStreet);
      const zipFromODNR = validateOhioZip(rawStreet);
      const { city: odnrCity, zip: odnrZip } = extractCityZipFromODNR(rawStreet);

      // ── ZIP: authoritative source order ────────────────────────────────────
      // 1. Census-geocoded zip (passed as inputZip) — most accurate
      // 2. Extracted from raw ODNR address (often contains the actual situs zip)
      // 3. Franklin County GIS ZIPCD field — accurate for Franklin
      // 4. City→ZIP static lookup (for when Census fails on dummy addresses)
      // 5. Nothing (leave blank rather than store a wrong state's zip)
      let finalZip = authoritativeZip || zipFromODNR || odnrZip;
      if (!finalZip && f.zip) {
        finalZip = validateOhioZip(String(a[f.zip] ?? ""));
      }

      // ── City: authoritative source order ───────────────────────────────────
      // 1. ODNR situs city (extracted from raw SitusAddressAll) — actual physical location
      // 2. Census-geocoded city (only useful when Census matched, which won't happen with dummy addr)
      // 3. Do NOT fall back to ODNR MailCity — it's the owner's mailing city, not the property city
      const finalCity = odnrCity || authoritativeCity;

      // Step 4: city→zip static lookup — last resort when all other sources fail
      if (!finalZip && finalCity) {
        finalZip = CITY_ZIP_LOOKUP[finalCity.toLowerCase().trim()] ?? "";
      }

      // Log zip resolution for parcels that still have no ZIP
      if (!finalZip) {
        console.log(
          `[zip-miss] ${houseNum} ${cleanStreet} — authoritativeZip="${authoritativeZip}" ` +
          `zipFromODNR="${zipFromODNR}" odnrZip="${odnrZip}" finalCity="${finalCity}" rawStreet="${rawStreet}"`
        );
      }

      return {
        parcelId,
        fullAddress:          fullAddr,
        houseNumber:          houseNum,
        streetName:           cleanStreet,
        city:                 finalCity,
        state:                "OH",
        zip:                  finalZip,
        side,
        lat:                  null,
        lng:                  null,
        ownerName:            f.ownerName ? String(a[f.ownerName] ?? "").trim() || null : null,
        county:               config.name,
        sourceListingAddress: sourceAddress,
        sourceImage,
      };
    })
    .filter((p): p is Parcel => p !== null);

  const byNum = (a: Parcel, b: Parcel) =>
    parseInt(a.houseNumber, 10) - parseInt(b.houseNumber, 10);

  const oddSide  = parcels.filter((p) => p.side === "odd").sort(byNum);
  const evenSide = parcels.filter((p) => p.side === "even").sort(byNum);
  const all      = [...parcels].sort(byNum);

  console.log(`[resolver] Parcels: ${oddSide.length} odd / ${evenSide.length} even`);

  return {
    sourceAddress, streetName: normalisedForGIS, county: config.name,
    totalParcels: parcels.length, oddSide, evenSide, all,
  };
}

// ── Backwards compat shim ─────────────────────────────────────────────────────

export async function queryFranklinCountyParcels(
  geocoded: GeocodedAddress,
): Promise<StreetParcelResult> {
  if (
    geocoded.geocodeStatus !== "match" &&
    geocoded.geocodeStatus !== "tie" &&
    geocoded.geocodeStatus !== "skipped"
  ) {
    return {
      sourceAddress: geocoded.inputAddress,
      streetName: geocoded.streetName ?? "",
      county: geocoded.county ?? "Franklin",
      totalParcels: 0, oddSide: [], evenSide: [], all: [],
      error: `Skipped — geocode status: ${geocoded.geocodeStatus}`,
    };
  }
  return queryCountyParcels(
    geocoded.streetName ?? "",
    geocoded.county ?? "Franklin",
    geocoded.inputAddress,
    geocoded.sourceImage,
    geocoded.city,
    geocoded.zip
  );
}

// ── Orchestrator ──────────────────────────────────────────────────────────────

export async function processAddresses(
  addresses: AddressInput[],
  options: { defaultCounty?: string; defaultState?: string } = {},
): Promise<StreetParcelResult[]> {
  if (addresses.length === 0) return [];

  // Geocode every address individually — Census batch is free and this is the
  // only reliable way to get accurate per-address city+zip for streets that
  // span multiple postal areas (e.g. Fairgrounds Rd: Hartford 43013 vs Mount Vernon 43050)
  console.log(`[resolver] Geocoding ${addresses.length} address(es) via Census...`);
  const censusResults = await batchGeocodeAddresses(addresses);

  // Build id→result lookup for O(1) access
  const censusById = new Map<string, GeocodedAddress>();
  for (const g of censusResults) censusById.set(g.id, g);

  const geocoded: GeocodedAddress[] = addresses.map((original) => {
    const g = censusById.get(original.id);
    const isMatch = g && (g.geocodeStatus === "match" || g.geocodeStatus === "tie");

    const finalCity = isMatch ? (g.city ?? original.city) : original.city;
    const finalZip  = isMatch ? (g.zip  ?? original.zip ?? null) : (original.zip ?? null);

    console.log(
      `[geocode-map] id=${original.id} street="${original.street}" ` +
      `→ isMatch=${isMatch} censusCity=${g?.city ?? "—"} censusZip=${g?.zip ?? "—"} ` +
      `→ finalCity=${finalCity ?? "—"} finalZip=${finalZip ?? "—"}`
    );

    const streetName = isMatch
      ? (g.streetName ?? extractStreetName(original.street))
      : extractStreetName(original.street);

    return {
      id:             original.id,
      inputAddress:   g?.inputAddress ?? `${original.street}, ${original.city}, ${original.state}`,
      matchedAddress: g?.matchedAddress ?? null,
      lat:            g?.lat ?? null,
      lng:            g?.lng ?? null,
      county:         g?.county ?? original.county ?? options.defaultCounty ?? null,
      countyFips:     g?.countyFips ?? null,
      state:          g?.state ?? original.state,
      streetName,
      geocodeStatus:  isMatch ? g!.geocodeStatus : "skipped" as const,
      sourceImage:    original.sourceImage,
      city:           finalCity ?? null,
      zip:            finalZip  ? validateOhioZip(finalZip) || null : null,
    };
  });

  // Deduplicate streets for GIS queries — prefer whichever geocoded entry has a zip
  const seenStreets = new Map<string, GeocodedAddress>();
  for (const g of geocoded) {
    if (!g.streetName || !g.county) continue;
    const k = `${g.streetName.toLowerCase()}|${g.county.toLowerCase()}`;
    const existing = seenStreets.get(k);
    // Prefer an entry with a zip over one without
    if (!existing || (!existing.zip && g.zip)) {
      seenStreets.set(k, g);
    }
  }

  console.log(
    `[resolver] ${seenStreets.size} unique street(s) to query (from ${addresses.length} input(s))`,
  );

  const results: StreetParcelResult[] = [];
  for (const [, geo] of seenStreets) {
    if (!geo.streetName?.trim()) {
      console.warn(`[resolver] Skipping empty street name (inputAddress="${geo.inputAddress}")`);
      continue;
    }
    const r = await queryCountyParcels(
      geo.streetName!,
      geo.county!,
      geo.inputAddress,
      geo.sourceImage,
      geo.city,
      geo.zip
    );
    results.push(r);
  }

  const ok      = results.filter((r) => !r.error && !r.skipped).length;
  const skipped = results.filter((r) => r.skipped).length;
  const failed  = results.filter((r) => r.error && !r.skipped).length;

  console.log(
    `[resolver] Done — ${ok} resolved / ${skipped} skipped (noise/broad) / ${failed} failed`,
  );

  return results;
}

// ── Standalone test ───────────────────────────────────────────────────────────

if (require.main === module) {
  try { require("dotenv").config(); } catch {}

  const TEST: AddressInput[] = [
    { id: "1", street: "819 Nebraska Ave",    city: "Toledo",    state: "OH", county: "Lucas"     },
    { id: "2", street: "1014 Evesham Ave",    city: "Toledo",    state: "OH", county: "Lucas"     },
    { id: "3", street: "5031 Crystal Street", city: "Ashtabula", state: "OH", county: "Ashtabula" },
    { id: "4", street: "576 E 152nd St",      city: "Cleveland", state: "OH", county: "Cuyahoga"  },
    { id: "5", street: "1009 Pardee Avenue",  city: "Akron",     state: "OH", county: "Summit"    },
    { id: "6", street: "142 S Champion Ave",  city: "Columbus",  state: "OH", zip: "43205", county: "Franklin" },
  ];

  (async () => {
    console.log("=".repeat(60));
    const results = await processAddresses(TEST);
    for (const r of results) {
      console.log(`\n${r.county} | ${r.streetName}`);
      if (r.skipped) { console.log(`  SKIPPED: ${r.error}`); continue; }
      if (r.error)   { console.log(`  ERROR: ${r.error}`);   continue; }
      console.log(`  ${r.totalParcels} parcels  (${r.oddSide.length} odd / ${r.evenSide.length} even)`);
      r.oddSide.slice(0, 3).forEach((p) =>
        console.log(`  ${p.houseNumber} ${p.streetName} — ${p.ownerName ?? "unknown"} [${p.city}, ${p.zip}]`),
      );
    }
    console.log("\n" + "=".repeat(60));
  })();
}