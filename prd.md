# Organic Driving for Dollars — PRD

## 1. Problem

The system processes scraped property listings provided via a CSV file. When a listing is parsed, the team wants every other address on that same street and owner contact info for every address on the street — all stored in one trackable spreadsheet with no duplicates. Today this process is done manually; this PRD scopes the automation to build the system from the ground up to ingest the CSV and automate the street-level property extraction and skip tracing.

## 2. Goals

- Given a list of shared listing addresses from Ohio (primarily Central Ohio/Columbus area) provided via a CSV file, return every parcel on the same street (both sides, from one end to the other) for each address.
- **Verify completeness** using Google Maps/Street View/Earth to ensure no addresses are missed.
- Skip trace the entire street list to get current owner contact info.
- Compile all properties into a spreadsheet, creating a dedicated "Organic Driving for Dollars" tab in the master Google Sheet, with zero duplicate rows.
- Maintain a clear system to ensure every address has been processed with no duplicates or omissions.

## 3. Out of scope (v1)

- Outreach itself (calling, mailing, texting owners) — this system stops at "data is ready to act on."
- States outside Ohio.
- Field scouts or field photo capture — the system relies entirely on the provided CSV data.

## 4. Users

- **Data Sources** — a CSV file containing scraped listings from various sites.
- **Acquisitions/ops team** — works the finished spreadsheet rows.

## 5. End-to-end workflow

1. System ingests a CSV file of scraped listing addresses.
2. System geocodes each address via the US Census Geocoder to confirm county + coordinates.
3. System queries that county's parcel GIS layer, filtered by street name + city, returning every parcel on the street with house numbers already attached.
4. System sorts each side (odd numbers / even numbers) ascending, giving an ordered list from one end of the street to the other.
5. System verifies completeness using Google Maps/Street View/Earth to ensure no addresses are missed.
6. Every address on the street list is sent to the skip tracing API; owner name + contact info comes back.
7. All records are compiled and upserted into the "Organic Driving for Dollars" tab in the master spreadsheet, deduped on street + house number.
8. A record is marked "complete" once it has a skip trace result, with a clear system to ensure no omissions.

## 6. Data model

| Field | Notes |
|---|---|
| `fullAddress` | e.g. "142 Main St, Columbus, OH" |
| `street`, `houseNumber`, `side` | side = odd/even, derived from house number |
| `county`, `parcelId` | from county GIS |
| `lat`, `lng` | from geocoder |
| `sourceListingAddress` | which scraped listing from the CSV triggered this street pull |
| `ownerName`, `ownerPhones[]`, `ownerEmails[]` | from skip trace |
| `skipTraceStatus` | pending / found / no-hit |
| `status` | pending / complete / needs-follow-up |
| `dateAdded` | for tracking/dedup |

## 7. Integrations

| Integration | Cost | Purpose |
|---|---|---|
| US Census Geocoder | Free | Address → coordinates + county |
| County parcel GIS (Franklin County first) | Free | Every parcel on a street, with house numbers |
| Google Maps/Street View/Earth | Varies | Verify completeness of addresses |
| Google Sheets API | Free tier | Write/dedup output rows |
| Skip tracing API (vendor TBD) | Paid, per-record | Owner contact info for every address |

## 8. Open questions

- **Skip trace vendor** — BatchData, PropStream, or REISkip — depends on expected volume/week.
- **CSV Format** — what are the exact column headers and structure of the inputted CSV file?
- **Re-submission behavior** — if a new listing comes in on a street already processed, do we skip already-traced addresses or re-check for ownership changes?
- **Skip trace budget** — how many streets/week, to estimate ongoing cost.

## 9. Build order & Timeline

**Expected Completion**: 1 week from 06-16-2026 (06-23-2026).

1. **Phase 1** — Ingest CSV file. Address in (from CSV) → geocode → Franklin County parcel query → sorted street list.
2. **Phase 2** — Integrate Google Maps/Street View verification.
3. **Phase 3** — Write that list into the Sheets tab ("Organic Driving for Dollars"), with dedup logic.
4. **Phase 4** — Wire up skip tracing for every address on the list.

## 10. Success metrics

- % of CSV listings fully processed (street pulled + verified + skip traced) within a defined turnaround time.
- Zero duplicate rows in the sheet.
- Skip trace spend per street stays within budget.