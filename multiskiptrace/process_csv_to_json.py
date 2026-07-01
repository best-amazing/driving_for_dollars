import sys
import os
import csv
import json
import logging
import concurrent.futures
from app import SkipTracer, MAX_CONCURRENT_WORKERS

def main():
    input_csv = r"C:\Users\USERR\Work\AB-group\Driving-for-dollars\results\extracted_streets.output.csv"
    output_json = r"C:\Users\USERR\Work\AB-group\Driving-for-dollars\results\skiptraced_results.json"
    
    # Configure logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    logger = logging.getLogger(__name__)

    # Initialize skip tracer logic
    tracer = SkipTracer()
    all_results = []
    
    if not os.path.exists(input_csv):
        logger.error(f"Input file not found: {input_csv}")
        return

    logger.info(f"Starting to process {input_csv} with {MAX_CONCURRENT_WORKERS} concurrent workers")

    rows_to_process = []
    with open(input_csv, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            address = row.get('Full Address', '').strip()
            if address:
                rows_to_process.append((i, row))

    try:
        # Use ThreadPoolExecutor to run ZenRows scraping in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as executor:
            future_to_row = {}
            for i, row in rows_to_process:
                address = row.get('Full Address', '').strip()
                city = row.get('City', '').strip()
                state = row.get('State', '').strip()
                zip_code = row.get('ZIP', '').strip()
                county = row.get('County', '').strip()
                owner_name = row.get('Owner Name', '').strip()
                
                # Submit parsing task to worker pool
                future = executor.submit(
                    tracer.process_address,
                    row,
                    i,
                    "cli_export",  # dummy job ID since we are bypassing Supabase
                    address,
                    city,
                    state,
                    zip_code,
                    county,
                    owner_name,
                    set()
                )
                future_to_row[future] = (i, address)

            # Process as tasks complete
            for future in concurrent.futures.as_completed(future_to_row):
                i, address = future_to_row[future]
                try:
                    results = future.result()
                    if results:
                        all_results.extend(results)
                        logger.info(f"Row {i+1}: Found {len(results)} results for {address}")
                    else:
                        logger.info(f"Row {i+1}: No results found for {address}")
                except Exception as exc:
                    logger.error(f"Row {i+1}: {address} generated an exception: {exc}")

    except KeyboardInterrupt:
        logger.info("Process interrupted by user. Saving partial results...")

    # Export to JSON
    with open(output_json, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, indent=4)
        
    logger.info(f"Finished processing. Saved {len(all_results)} records to {output_json}")

if __name__ == '__main__':
    main()
