// integrations/truepeoplesearch-skiptrace.ts
//
// TruePeopleSearch skip-tracer using ZenRows proxy.
//
// FLOW (per address):
//   Step 1 — Search page: build /resultaddress URL, fetch via ZenRows,
//             find the card whose "Lives in" matches city/state/zip,
//             extract the detail page href.
//
//   Step 2 — Detail page: fetch matched profile, extract name, age,
//             up to 6 phone numbers with types, up to 2 emails.
//
//   Step 3 — Pagination: follow "Next" button until match found or
//             MAX_SEARCH_PAGES exhausted.
//
// Usage:
//   npx tsx integrations/truepeoplesearch-skiptrace.ts
//   npx tsx integrations/truepeoplesearch-skiptrace.ts "142 S Champion Ave,Columbus,OH,43205" "819 Nebraska Ave,Toledo,OH,43607"

import axios                   from "axios";
import * as fs                 from "fs";
import * as path               from "path";
import { load, CheerioAPI }    from "cheerio";

// ── Config ────────────────────────────────────────────────────────────────────

const ZENROWS_BASE_URL  = "https://api.zenrows.com/v1/";

const MAX_CONCURRENT    = 1;    // lowered — ZenRows premium proxy needs breathing room
const MAX_SEARCH_PAGES  = 5;
const MAX_PHONES        = 6;
const MAX_EMAILS        = 2;

const TPS_BASE          = "https://www.truepeoplesearch.com";
const TPS_SEARCH_PATH   = "/resultaddress";

// ZenRows params — split by page type:
//
//   SEARCH pages (/resultaddress):
//     js_render: false — server-rendered HTML, no JS needed, fast (~5-10s)
//     antibot:   true  — handles TLS fingerprinting without a full browser
//
//   DETAIL pages (/find/person/...):
//     js_render: true  — TPS profile pages have heavier bot protection that
//                        requires a real browser render to bypass. This is
//                        slower (~30-60s) but necessary for detail pages only.
//     antibot:   true
const ZENROWS_SEARCH_PARAMS = {
  js_render:     "true",
  antibot:       "true",
  premium_proxy: "true",
  proxy_country: "us",
  wait:          "4000",
};

const ZENROWS_DETAIL_PARAMS = {
  js_render:     "true",
  antibot:       "true",
  premium_proxy: "true",
  proxy_country: "us",
  wait:          "5000",
};

const SEARCH_TIMEOUT_MS = 150_000;  // search pages need full browser render now
const DETAIL_TIMEOUT_MS = 240_000;  // detail pages need full browser render
// ── Types ─────────────────────────────────────────────────────────────────────

export interface RawInput {
  header: Array<{ title: string }>;
  data:   Array<Array<string | number | null>>;
}

export interface AddressInput {
  Address:  string;
  City:     string;
  State:    string;
  Zipcode:  string;
}

export interface OwnerContact extends AddressInput {
  "First Name":     string;
  "Last Name":      string;
  Age1:             string;
  "Phone-1-Number": number | string;
  "Phone-1-Type":   string;
  "Phone-2-Number": number | string;
  "Phone-2-Type":   string;
  "Phone-3-Number": number | string;
  "Phone-3-Type":   string;
  "Phone-4-Number": number | string;
  "Phone-4-Type":   string;
  "Phone-5-Number": number | string;
  "Phone-5-Type":   string;
  "Phone-6-Number": number | string;
  "Phone-6-Type":   string;
  "Email1-1":       string;
  "Email1-2":       string;
}

// ── Input parser ──────────────────────────────────────────────────────────────

export function parseInput(raw: RawInput): AddressInput[] {
  const keys = raw.header.map(h => h.title.trim());
  const results: AddressInput[] = [];

  for (const row of raw.data) {
    if (!Array.isArray(row) || row.length !== keys.length) {
      console.warn(`[tps] Skipping malformed row: ${JSON.stringify(row)}`);
      continue;
    }

    const obj: Record<string, string> = {};
    for (let i = 0; i < keys.length; i++) {
      obj[keys[i]] = String(row[i] ?? "").trim();
    }

    if (!obj["Address"] || !obj["City"] || !obj["State"] || !obj["Zipcode"]) {
      console.warn(`[tps] Skipping row with missing required fields: ${JSON.stringify(obj)}`);
      continue;
    }

    results.push({
      Address: obj["Address"],
      City:    obj["City"],
      State:   obj["State"],
      Zipcode: obj["Zipcode"],
    });
  }

  return results;
}

// ── URL builder ───────────────────────────────────────────────────────────────

function buildSearchUrl(input: AddressInput): string {
  const address = input.Address.replace(/[\n\r,]/g, "").trim();
  const city    = input.City.replace(/,/g, "").trim();
  const state   = input.State.replace(/,/g, "").trim();
  const zip     = String(input.Zipcode).replace(/,/g, "").trim();

  const street = encodeURIComponent(address);
  const csz    = encodeURIComponent(`${city} ${state} ${zip}`);

  return `${TPS_BASE}${TPS_SEARCH_PATH}?streetaddress=${street}&citystatezip=${csz}`;
}

// ── Debug file helper ─────────────────────────────────────────────────────────

function saveDebug(filename: string, content: string): void {
  try {
    const dir = path.resolve("results/logs");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, filename), content, "utf-8");
    console.log(`[tps] Debug saved → results/logs/${filename}`);
  } catch {
    // non-fatal
  }
}

function slugify(input: AddressInput): string {
  return `${input.Address}_${input.City}_${input.State}`
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .slice(0, 60);
}

// ── ZenRows fetch ─────────────────────────────────────────────────────────────

function getApiKey(): string {
  // Prefer env var, fall back to hardcoded key from original Python
  return process.env.ZENROWS_API_KEY || "28bbe7ae1b3c6541a1b32891685ffb17f8a43df7";
}

async function zenrowsFetch(
  targetUrl:  string,
  params:     Record<string, string> = ZENROWS_SEARCH_PARAMS,
  timeoutMs:  number                 = SEARCH_TIMEOUT_MS,
): Promise<string | null> {
  const apiKey = getApiKey();

  console.log(`[tps] → ZenRows fetch (js_render=${params.js_render}): ${targetUrl}`);

  try {
    // Build the full URL manually so axios doesn't double-encode the
    // target URL (e.g. %20 → %2520).  The ZenRows `url` param must
    // be encoded exactly once.
    const qs = new URLSearchParams({
      apikey: apiKey,
      url:    targetUrl,
      ...params,
    }).toString();

    const response = await axios.get(`${ZENROWS_BASE_URL}?${qs}`, {
      timeout: timeoutMs,
    });

    const xRequestId = response.headers["x-request-id"];
    if (xRequestId) {
      console.log(`[tps] X-Request-ID: ${xRequestId}`);
    }

    // ZenRows returns error details in the response body with a non-200 status
    if (response.status !== 200) {
      console.warn(`[tps] ZenRows HTTP ${response.status} for ${targetUrl}`);
      console.warn(`[tps] ZenRows response: ${JSON.stringify(response.data)?.slice(0, 300)}`);
      return null;
    }

    const html = typeof response.data === "string"
      ? response.data
      : JSON.stringify(response.data);

    if (!html || html.length < 500) {
      console.warn(`[tps] Short/empty response (${html.length}ch) for ${targetUrl}`);
      return null;
    }

    console.log(`[tps] ✓ Got ${html.length}ch from ZenRows`);
    return html;

  } catch (err: any) {
    // Axios timeout surfaces as ECONNABORTED or ERR_CANCELED
    if (err.code === "ECONNABORTED" || err.code === "ERR_CANCELED") {
      console.error(`[tps] ZenRows timeout (${timeoutMs / 1000}s) for ${targetUrl}`);
    } else if (err.response) {
      // ZenRows returned an HTTP error with a body
      console.error(`[tps] ZenRows HTTP ${err.response.status}: ${JSON.stringify(err.response.data)?.slice(0, 300)}`);
    } else {
      console.error(`[tps] ZenRows network error for ${targetUrl}: ${err.message}`);
    }
    return null;
  }
}

async function zenrowsFetchWithRetry(
  targetUrl: string,
  params: Record<string, string>,
  timeoutMs: number,
  retries = 2,
): Promise<string | null> {
  // Fresh session_id each attempt to get a clean residential IP
  for (let attempt = 1; attempt <= retries; attempt++) {
    const attemptParams = {
      ...params,
      session_id: String(Math.floor(Math.random() * 9000) + 1),
    };
    const result = await zenrowsFetch(targetUrl, attemptParams, timeoutMs);
    if (result) return result;
    if (attempt < retries) {
      console.log(`[tps] ZenRows retry ${attempt}/${retries - 1} for ${targetUrl}`);
      await new Promise(r => setTimeout(r, 3000));
    }
  }

  return null;
}

// ── HTML parsers ──────────────────────────────────────────────────────────────

const AGE_REGEX   = /Age\s+\d+\s+\([A-Za-z]{3}\s+\d+\)/;
const EMAIL_REGEX = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/;

function findDetailHref($: CheerioAPI, input: AddressInput): string | null {
  let matchedHref: string | null = null;
  const cityLower  = input.City.toLowerCase();
  const stateLower = input.State.toLowerCase();

  $(".card-summary").each((_, card) => {
    const $card = $(card);

    // Skip hidden cards (TPS uses d-none for expanded duplicate cards
    // that have stale/invalid hrefs)
    if ($card.hasClass("d-none")) return;

    // Get the full card text for city/state matching.
    // TPS renders location in two ways:
    //   1. "Lives in" label + content-value (older format)
    //   2. Inline after age: "71 • Toledo, OH" (current format)
    // Matching the full text covers both.
    const cardText = $card.text().toLowerCase();

    if (cardText.includes(cityLower) && cardText.includes(stateLower)) {
      const href = $card.find("a.detail-link").attr("href");
      if (href) {
        console.log(`[tps] Matched card → ${$card.find(".content-header").text().trim()} | href: ${href}`);
        matchedHref = href;
        return false; // break .each
      }
    }
  });

  if (matchedHref) return matchedHref;

  // Fallback: first visible green CTA button (skip d-none parents)
  $("a.btn.btn-success.btn-lg.detail-link").each((_, el) => {
    const $el = $(el);
    if ($el.closest(".d-none").length === 0) {
      matchedHref = $el.attr("href") ?? null;
      return false;
    }
  });

  return matchedHref;
}

function parseDetailPage($: CheerioAPI): Omit<OwnerContact, keyof AddressInput> {
  // Name
  const fullName  = $("h1.oh1").first().text().trim();
  const nameParts = fullName.split(/\s+/);
  const firstName = nameParts[0] ?? "";
  const lastName  = nameParts[nameParts.length - 1] ?? "";

  // Age
  let age = "Age Unknown";
  $("span").each((_, el) => {
    const t = $(el).text().trim();
    if (AGE_REGEX.test(t)) {
      age = t;
      return false;
    }
  });

  // Phones
  const seenPhones = new Set<string>();
  const phones: Array<{ number: number | string; type: string }> = [];

  $("div.row div.col-12").each((_, col) => {
    if (phones.length >= MAX_PHONES) return false;

    const $col      = $(col);
    const phoneSpan = $col.find('a[data-link-to-more="phone"] span[itemprop="telephone"]');
    const typeSpan  = $col.find("span.smaller");

    if (!phoneSpan.length) return;

    const phoneRaw  = phoneSpan.text().trim();
    const phoneType = typeSpan.text().trim();
    const phoneInfo = `${phoneRaw} - ${phoneType}`;

    if (!phoneInfo || seenPhones.has(phoneInfo)) return;
    seenPhones.add(phoneInfo);

    const digits = phoneRaw.replace(/\D/g, "");
    phones.push({
      number: digits.length >= 10 ? parseInt(digits, 10) : digits,
      type:   phoneType || "Unknown",
    });
  });

  // Emails
  const seenEmails = new Set<string>();
  const emails: string[] = [];

  $(".col > div:last-child").each((_, el) => {
    if (emails.length >= MAX_EMAILS) return false;
    const text  = $(el).text().trim();
    const match = text.match(EMAIL_REGEX);
    if (
      match &&
      match[0] !== "support@truepeoplesearch.com" &&
      !seenEmails.has(match[0])
    ) {
      seenEmails.add(match[0]);
      emails.push(match[0]);
    }
  });

  const empty = { number: "" as number | string, type: "" };
  const p = (i: number) => phones[i] ?? empty;

  return {
    "First Name":     firstName,
    "Last Name":      lastName,
    Age1:             age,
    "Phone-1-Number": p(0).number, "Phone-1-Type": p(0).type,
    "Phone-2-Number": p(1).number, "Phone-2-Type": p(1).type,
    "Phone-3-Number": p(2).number, "Phone-3-Type": p(2).type,
    "Phone-4-Number": p(3).number, "Phone-4-Type": p(3).type,
    "Phone-5-Number": p(4).number, "Phone-5-Type": p(4).type,
    "Phone-6-Number": p(5).number, "Phone-6-Type": p(5).type,
    "Email1-1":       emails[0] ?? "",
    "Email1-2":       emails[1] ?? "",
  };
}

function getNextPageUrl($: CheerioAPI): string | null {
  const href = $("#btnNextPage").attr("href");
  return href ? `${TPS_BASE}${href}` : null;
}

// ── Empty contact template ────────────────────────────────────────────────────

function emptyContact(input: AddressInput): OwnerContact {
  return {
    ...input,
    "First Name": "", "Last Name": "", Age1: "",
    "Phone-1-Number": "", "Phone-1-Type": "",
    "Phone-2-Number": "", "Phone-2-Type": "",
    "Phone-3-Number": "", "Phone-3-Type": "",
    "Phone-4-Number": "", "Phone-4-Type": "",
    "Phone-5-Number": "", "Phone-5-Type": "",
    "Phone-6-Number": "", "Phone-6-Type": "",
    "Email1-1": "", "Email1-2": "",
  };
}

// ── Core scrape (single address) ──────────────────────────────────────────────

async function scrapeAddress(input: AddressInput): Promise<OwnerContact[]> {
  const results: OwnerContact[] = [];
  const addressSlug = slugify(input);
  let nextPageUrl: string | null = buildSearchUrl(input);
  let pageNum = 0;

  const sessionId = String(Math.floor(Math.random() * 9000) + 1); // 1–9999, within ZenRows limit

  const searchParams = {
    ...ZENROWS_SEARCH_PARAMS,
    session_id: sessionId,
  };

  const detailParams = {
    ...ZENROWS_DETAIL_PARAMS,
    session_id: sessionId, // same IP as the search request
  };

  while (nextPageUrl && pageNum < MAX_SEARCH_PAGES) {
    pageNum++;
    console.log(`[tps] ${input.Address}, ${input.City} — search page ${pageNum}`);

    const html = await zenrowsFetchWithRetry(nextPageUrl, searchParams, SEARCH_TIMEOUT_MS);
    if (!html) {
      console.warn(`[tps] No response on search page ${pageNum} for ${input.Address}`);
      break;
    }

    if (pageNum === 1) {
      saveDebug(`tps_search_${addressSlug}.html`, html);
    }

    const $ = load(html);

    // Log card count for debugging
    const cardCount = $(".card-summary").length;
    console.log(`[tps] Found ${cardCount} result card(s) on page ${pageNum}`);

    const detailHref = findDetailHref($, input);

    if (!detailHref) {
      console.log(`[tps] No matching card on page ${pageNum} — trying next page`);
      nextPageUrl = getNextPageUrl($);
      continue;
    }

    // ── Detail page ───────────────────────────────────────────────────────

    try {
      const detailUrl = detailHref.startsWith("http")
        ? detailHref
        : `${TPS_BASE}${detailHref}`;

      console.log(`[tps] Detail → ${detailUrl}`);
      const detailHtml = await zenrowsFetchWithRetry(detailUrl, detailParams, DETAIL_TIMEOUT_MS);

      if (!detailHtml) {
        console.warn(`[tps] No response from detail page for ${input.Address}`);
      } else {
        saveDebug(`tps_detail_${addressSlug}.html`, detailHtml);

        const $d      = load(detailHtml);
        const contact = parseDetailPage($d);

        const hasPhone = !!contact["Phone-1-Number"];
        if (hasPhone) {
          results.push({ ...emptyContact(input), ...contact });
          console.log(
            `[tps] ✓ ${contact["First Name"]} ${contact["Last Name"]} ` +
            `(${contact.Age1}) @ ${input.Address}`
          );
        } else {
          console.warn(`[tps] No phones found for ${input.Address} — skipping`);
        }
      }
    } catch (err: any) {
      console.error(`[tps] Detail page error for ${input.Address}: ${err.message}`);
    }

    nextPageUrl = getNextPageUrl($);
  }

  return results;
}

// ── Concurrency runner ────────────────────────────────────────────────────────

async function runQueue(
  addresses:   AddressInput[],
  concurrency: number,
): Promise<OwnerContact[]> {
  const queue  = [...addresses];
  const output: OwnerContact[] = [];

  async function worker(): Promise<void> {
    while (queue.length > 0) {
      const item = queue.shift();
      if (!item) break;
      try {
        const result = await scrapeAddress(item);
        output.push(...result);
      } catch (err: any) {
        console.error(`[tps] Error scraping ${item.Address}: ${err.message}`);
      }
    }
  }

  await Promise.all(
    Array.from({ length: concurrency }, () => worker())
  );

  return output;
}

// ── Public entrypoint ─────────────────────────────────────────────────────────

export async function runSkipTrace(raw: RawInput): Promise<{
  Success: boolean;
  data?:   OwnerContact[];
  error?:  string;
}> {
  try {
    const addresses = parseInput(raw);
    console.log(`[tps] Skip trace: ${addresses.length} address(es)`);

    const results = await runQueue(addresses, MAX_CONCURRENT);
    console.log(`[tps] Done — ${results.length} owner(s) found`);

    return { Success: true, data: results };
  } catch (err: any) {
    console.error(`[tps] Fatal error: ${err.message}`);
    console.error(err.stack);
    return { Success: false, error: err.message };
  }
}

// ── Standalone CLI ────────────────────────────────────────────────────────────

if (require.main === module) {
  try { require("dotenv").config(); } catch {}

  const args = process.argv.slice(2);
  let input: RawInput;

  if (args.length > 0) {
    const data = args.map(arg => {
      const parts = arg.split(",").map(s => s.trim());
      if (parts.length < 4) {
        console.error(`[tps] Invalid format: "${arg}". Expected: "Address,City,State,Zip"`);
        process.exit(1);
      }
      return parts.slice(0, 4);
    });

    input = {
      header: [
        { title: "Address" },
        { title: "City" },
        { title: "State" },
        { title: "Zipcode" },
      ],
      data,
    };
  } else {
    input = {
      header: [
        { title: "Address" },
        { title: "City" },
        { title: "State" },
        { title: "Zipcode" },
      ],
      data: [
        ["142 S Champion Ave", "Columbus", "OH", "43205"],
        ["819 Nebraska Ave",   "Toledo",   "OH", "43607"],
      ],
    };
  }

  (async () => {
    console.log("=".repeat(60));
    console.log("TruePeopleSearch Skip-Tracer (ZenRows)");
    console.log("=".repeat(60));

    console.log(`[tps] Using API key: ${getApiKey().slice(0, 8)}...`);

    const result = await runSkipTrace(input);

    console.log("\n--- Final Results ---");
    console.log(JSON.stringify(result.data ?? [], null, 2));

    const outDir = path.resolve("results");
    if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
    const outPath = path.resolve(outDir, "skiptrace_results.json");
    fs.writeFileSync(outPath, JSON.stringify(result.data ?? [], null, 2), "utf-8");
    console.log(`\n✅ Results saved to: ${outPath}`);
  })();
}