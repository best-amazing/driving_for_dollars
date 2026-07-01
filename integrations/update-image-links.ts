import { google, sheets_v4 } from "googleapis";
import * as fs from "fs";
import * as path from "path";

// Load .env
require("dotenv").config({ path: path.join(__dirname, "../.env") });

const TAB_NAME = "Organic Driving for Dollars";

// Resolve the path to imagesLink.md
const IMAGES_LINK_PATH = path.join(__dirname, "../street-pictures/processed-images/imagesLink.md");

function getAuthClient() {
  const keyPath = process.env.GOOGLE_SERVICE_ACCOUNT_KEY_PATH;
  const keyJson = process.env.GOOGLE_SERVICE_ACCOUNT_KEY_JSON;

  if (!keyPath && !keyJson) {
    throw new Error(
      "No service account credentials found.\n" +
      "Set GOOGLE_SERVICE_ACCOUNT_KEY_PATH=/path/to/key.json\n" +
      "  or GOOGLE_SERVICE_ACCOUNT_KEY_JSON='{...}' in your .env file."
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

function colLetter(n: number): string {
  let s = "";
  while (n > 0) {
    n--;
    s = String.fromCharCode(65 + (n % 26)) + s;
    n = Math.floor(n / 26);
  }
  return s;
}

function loadImagesLinks(): Record<string, string> {
  if (!fs.existsSync(IMAGES_LINK_PATH)) {
    console.error(`Could not find ${IMAGES_LINK_PATH}`);
    return {};
  }
  
  const content = fs.readFileSync(IMAGES_LINK_PATH, "utf-8");
  const links: Record<string, string> = {};
  
  for (const line of content.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    
    // Line format: image copy.png, https://drive.google...
    // Notice there might be a trailing comma
    const splitIndex = trimmed.indexOf(",");
    if (splitIndex === -1) continue;
    
    const imageName = trimmed.substring(0, splitIndex).trim();
    let driveLink = trimmed.substring(splitIndex + 1).trim();
    
    // Remove any trailing commas from the drive link
    if (driveLink.endsWith(",")) {
      driveLink = driveLink.substring(0, driveLink.length - 1).trim();
    }
    
    links[imageName] = driveLink;
  }
  
  return links;
}

async function run() {
  const SPREADSHEET_ID = process.env.SPREADSHEET_ID;
  if (!SPREADSHEET_ID) {
    console.error("Set SPREADSHEET_ID in your .env file.");
    process.exit(1);
  }
  
  const IMAGE_LINKS = loadImagesLinks();
  if (Object.keys(IMAGE_LINKS).length === 0) {
    console.error("No image links found in the mappings file.");
    return;
  }

  const auth = getAuthClient();
  const sheets = google.sheets({ version: "v4", auth: auth as any });

  console.log(`[sheets] Fetching tab data...`);
  const range = `'${TAB_NAME}'!A:Z`;
  const resp = await sheets.spreadsheets.values.get({
    spreadsheetId: SPREADSHEET_ID,
    range,
  });

  const rows = resp.data.values;
  if (!rows || rows.length === 0) {
    console.log("No data found in the spreadsheet.");
    return;
  }

  const header = rows[0];
  const sourceListingColIndex = header.indexOf("Source Listing");
  
  if (sourceListingColIndex === -1) {
    console.error("Could not find 'Source Listing' column.");
    return;
  }

  const updateRequests: any[] = [];
  
  for (let i = 1; i < rows.length; i++) {
    const row = rows[i];
    const sourceListing = row[sourceListingColIndex] || "";
    
    // Check if the source listing matches any of our images
    for (const [imageName, driveLink] of Object.entries(IMAGE_LINKS)) {
      let searchName = imageName.replace(/\.[^/.]+$/, "").replace(/ /g, "_");
      
      // Handle the WhatsApp renaming you did in the CSV
      if (searchName.toLowerCase() === "whatsapp_image") searchName = "WhatsApp_main";
      if (searchName.toLowerCase() === "whatsapp_image(3)") searchName = "WhatsApp_3";
      if (searchName.toLowerCase() === "whatsapp_image_3") searchName = "WhatsApp_3"; // just in case
      
      // Extract the actual display text from the cell, which could be a HYPERLINK formula
      let displayValue = sourceListing;
      if (sourceListing.startsWith("=HYPERLINK")) {
        const parts = sourceListing.split('"');
        if (parts.length >= 4) {
           displayValue = parts[3];
        }
      }
      
      // Strip 'Image: ' from the beginning so searching for 'image' doesn't just match the prefix!
      const cleanDisplayValue = displayValue.replace(/^Image:\s*/i, "");
      
      // Use word-boundary regex so "image_copy" doesn't match "image_copy_2"
      // but it WILL match "image_copy / image"
      const regex = new RegExp(`\\b${searchName}\\b`, 'i');
      
      if (regex.test(cleanDisplayValue)) {
        // Use USER_ENTERED format with =HYPERLINK formula
        const formula = `=HYPERLINK("${driveLink}", "${displayValue.replace(/"/g, '""')}")`;
        
        const colStr = colLetter(sourceListingColIndex + 1);
        const cellRange = `'${TAB_NAME}'!${colStr}${i + 1}`;
        
        // Don't update if it's already the exact same formula
        if (sourceListing !== formula) {
          updateRequests.push({
            range: cellRange,
            values: [[formula]]
          });
        }
        break; 
      }
    }
  }

  if (updateRequests.length === 0) {
    console.log("No rows needed updating or they are already updated.");
    return;
  }

  console.log(`[sheets] Updating ${updateRequests.length} rows with Google Drive links...`);
  
  try {
    await sheets.spreadsheets.values.batchUpdate({
      spreadsheetId: SPREADSHEET_ID,
      requestBody: {
        valueInputOption: "USER_ENTERED", 
        data: updateRequests,
      },
    });
    console.log(`[sheets] ✓ Successfully added Google Drive links to spreadsheet.`);
  } catch (err: any) {
    console.error(`[sheets] ✗ Update failed: ${err?.message ?? String(err)}`);
  }
}

run().catch(console.error);
