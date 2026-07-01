# app.py
import os
import re
import csv
import time
import requests
from flask import Flask, render_template, request, send_file, jsonify, Response
from bs4 import BeautifulSoup
import urllib.parse
import io
import logging
from datetime import datetime, timezone
import json
from typing import List, Dict, Any, Optional, Tuple
import threading
import random
from dotenv import load_dotenv
from collections import deque
import concurrent.futures
import hashlib
from supabase import create_client, Client

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Configuration (make wait configurable)
ZENROWS_API_KEY = os.getenv("ZENROWS_API_KEY")
ZENROWS_BASE_URL = "https://api.zenrows.com/v1/"
TRUE_PEOPLE_SEARCH_BASE = "https://www.truepeoplesearch.com"
ZENROWS_WAIT_MS = int(os.getenv("ZENROWS_WAIT_MS", "1000"))  # default 1000 (1s)
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "15.0"))

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Global dictionary to track the state of multiple processing jobs (in-memory)
processing_jobs: Dict[str, Dict[str, Any]] = {}
RESULTS_FOLDER = 'results'
os.makedirs(RESULTS_FOLDER, exist_ok=True)


class SkipTracer:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        self.request_count = 0
        self.last_request_time = time.time()
        self.request_semaphore = threading.Semaphore(4)  # limit concurrent requests

    def expand_address(self, address: str) -> List[str]:
        addresses = []

        if '#' in address:
            parts = address.split('#')
            base_address = parts[0].strip()
            number_part = parts[1].strip()
            base_match = re.search(r'(\d+)', base_address)
            if base_match and number_part.isdigit():
                base_num = int(base_match.group(1))
                end_num = int(number_part)
                if end_num > base_num:
                    for num in range(base_num, end_num + 1):
                        expanded = re.sub(r'\d+', str(num), base_address, count=1)
                        addresses.append(expanded)
                    return addresses

        hyphen_match = re.search(r'(\d+)-(\d+)\s+', address)
        if hyphen_match:
            start = int(hyphen_match.group(1))
            end = int(hyphen_match.group(2))
            if end < start and len(str(end)) < len(str(start)):
                prefix = str(start)[:len(str(start)) - len(str(end))]
                end = int(prefix + str(end))
            if end >= start:
                for num in range(start, end + 1):
                    expanded = re.sub(r'\d+-\d+', str(num), address, count=1)
                    addresses.append(expanded)
                return addresses

        addresses.append(address)
        return addresses

    def make_zenrows_request(self, url: str, max_retries: int = 3) -> Optional[requests.Response]:
        """
        Make a request to Zenrows with retry logic and defensive error handling.

        Defensive improvements:
        - Rate-limits to avoid 429s
        - Exponential backoff with jitter on retry
        - Catches RecursionError (observed in SSLContext recursion), SSLError, and RequestException
        - Returns None on unrecoverable errors so callers can continue gracefully
        """
        with self.request_semaphore:
            # Rate limiting to avoid 429 errors
            current_time = time.time()
            time_since_last_request = current_time - self.last_request_time

            # Ensure at least 0.5 second between requests to avoid rate limiting
            if time_since_last_request < 0.5:
                time.sleep(0.5 - time_since_last_request)

            self.last_request_time = time.time()

        for attempt in range(max_retries):
            response = None
            try:
                # Add a small delay between retries (exponential backoff + jitter)
                if attempt > 0:
                    wait_time = (2 ** attempt) + random.uniform(0, 1)
                    logger.info(f"Waiting {wait_time:.2f} seconds before retry (attempt {attempt + 1})...")
                    time.sleep(wait_time)

                params = {
                    'url': url,
                    'apikey': self.api_key,
                    'js_render': 'true',
                    'premium_proxy': 'true',
                    'proxy_country': 'us',
                    'wait': str(ZENROWS_WAIT_MS),  # configurable via env var
                    'block_resources': 'image,media,font',
                }

                # Perform the request; wrap in try/except to catch SSL/recursion issues
                try:
                    response = self.session.get(
                        ZENROWS_BASE_URL,
                        params=params,
                        timeout=30  # request-level timeout (tune as needed)
                    )
                except RecursionError as rec_err:
                    # Observed when SSLContext property recurses (often gevent/monkeypatch issue).
                    logger.exception(
                        "RecursionError while creating SSLContext (likely SSL/monkeypatch/compatibility issue). "
                        "Switch to gthread or pin Python/OpenSSL if you see this repeatedly."
                    )
                    # Do not re-raise; return None so caller treats as failed fetch
                    return None
                except requests.exceptions.SSLError as ssle:
                    logger.exception(f"SSL error while calling Zenrows: {ssle}")
                    # treat as transient and let outer retry loop continue (response is None)
                    response = None
                except requests.exceptions.RequestException as req_e:
                    logger.error(f"Attempt {attempt + 1}: Request error: {req_e}")
                    response = None

                # If we got a response, handle status codes
                if response is not None:
                    if response.status_code == 200:
                        self.request_count += 1
                        return response
                    elif response.status_code == 429:
                        logger.error(f"Attempt {attempt + 1}: Rate limited (429). Waiting longer...")
                        # Longer wait for rate limiting
                        time.sleep(10 * (attempt + 1))
                        continue
                    else:
                        logger.error(f"Attempt {attempt + 1}: Failed to fetch data: {response.status_code}")
                        if response.text:
                            logger.debug(f"Response content (truncated): {response.text[:500]}")
                        # For non-429 HTTP errors we retry a limited number of times
                        continue

            except Exception as e:
                # Catch-all for anything unexpected during the attempt loop (keeps worker alive)
                logger.exception(f"Unexpected error during Zenrows request attempt {attempt + 1}: {e}")
                # small jitter before next retry
                time.sleep(random.uniform(0.5, 1.5))
                continue

        # If all retries failed, return None
        logger.error(f"All {max_retries} attempts to fetch {url} failed.")
        return None

    def search_address(self, address: str, city: str, state: str, zip_code: str, page: int = 1) -> Optional[BeautifulSoup]:
        search_url = f"{TRUE_PEOPLE_SEARCH_BASE}/results"
        query_params = {
            'streetaddress': address,
            'citystatezip': f"{city}, {state} {zip_code}"
        }
        if page > 1:
            query_params['page'] = page

        job_id = threading.current_thread().name
        if job_id in processing_jobs:
            processing_jobs[job_id]['results'].append({
                'type': 'status',
                'message': f"Searching for: {address}, {city}, {state} {zip_code}",
                'url': f"{search_url}?{urllib.parse.urlencode(query_params)}"
            })

        target_url = f"{search_url}?{urllib.parse.urlencode(query_params)}"
        logger.info(f"Requesting URL: {target_url}")

        response = self.make_zenrows_request(target_url)
        if response:
            if "Please enable JavaScript to view the page content" in response.text:
                logger.error("JavaScript rendering failed - page requires JavaScript")
                if job_id in processing_jobs:
                    processing_jobs[job_id]['results'].append({
                        'type': 'status',
                        'message': "JavaScript rendering failed - try increasing wait time"
                    })
                return None

            soup = BeautifulSoup(response.content, 'html.parser')

            record_count_elem = soup.find('div', class_='h2')
            if job_id in processing_jobs:
                if record_count_elem and "No Results Found" in record_count_elem.text:
                    processing_jobs[job_id]['results'].append({
                        'type': 'status',
                        'message': "No results found for this address"
                    })
                    logger.info("No results found for this address")
                    return soup
                elif record_count_elem:
                    result_text = record_count_elem.text.strip()
                    processing_jobs[job_id]['results'].append({
                        'type': 'status',
                        'message': f"Found results: {result_text}"
                    })
                    logger.info(f"Found: {result_text}")

            return soup
        else:
            if job_id in processing_jobs:
                processing_jobs[job_id]['results'].append({
                    'type': 'status',
                    'message': f"Failed to search for: {address}, {city}, {state} {zip_code}"
                })
        return None

    def parse_search_results(self, soup: BeautifulSoup, job_id: str = None) -> List[Dict[str, Any]]:
        results = []

        no_results = soup.find('div', class_='h2', string=lambda text: text and 'No Results Found' in text)
        if no_results:
            logger.info("No results found on this page")
            return results

        person_cards = soup.find_all('div', class_='card-summary')

        for card in person_cards:
            try:
                name_elem = card.find('div', class_='h4')
                if not name_elem:
                    name_elem = card.find('h2')

                if name_elem:
                    name_parts = name_elem.text.strip().split()
                    first_name = name_parts[0] if name_parts else ""
                    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
                else:
                    first_name, last_name = "", ""

                detail_link = card.find('a')
                detail_url = detail_link['href'] if detail_link and detail_link.has_attr('href') else ""

                age_location = card.find_all('span', class_='content-value')
                age = age_location[0].text.strip() if len(age_location) > 0 else ""
                location = age_location[1].text.strip() if len(age_location) > 1 else ""

                results.append({
                    'first_name': first_name,
                    'last_name': last_name,
                    'age': age,
                    'location': location,
                    'detail_url': detail_url
                })
            except Exception as e:
                logger.error(f"Error parsing person card: {str(e)}")
                continue

        return results

    def get_person_details(self, detail_url: str, searched_address: str = "") -> Dict[str, Any]:
        details = {
            'first_name': '',
            'last_name': '',
            'phones': [],
            'emails': [],
            'addresses': [],
            'url': '',
            'error': None
        }

        try:
            full_url = f"{TRUE_PEOPLE_SEARCH_BASE}{detail_url}"
            details['url'] = full_url
            logger.info(f"Getting details for: {full_url}")

            job_id = threading.current_thread().name
            if job_id in processing_jobs:
                processing_jobs[job_id]['results'].append({
                    'type': 'status',
                    'message': f"Fetching details for: {full_url}",
                    'url': full_url
                })

            response = self.make_zenrows_request(full_url)

            if not response or response.status_code != 200:
                error_msg = f"HTTP Error: {response.status_code}" if response else "Failed to fetch details"
                details['error'] = error_msg
                if job_id in processing_jobs:
                    processing_jobs[job_id]['results'].append({'type': 'status', 'message': error_msg})
                return details

            soup = BeautifulSoup(response.content, 'html.parser')

            person_card = soup.find('div', id='personDetails')
            if not person_card:
                details['error'] = "Could not find personDetails card."
                if job_id in processing_jobs:
                    processing_jobs[job_id]['results'].append({'type': 'status', 'message': details['error']})
                return details

            details['first_name'] = person_card.get('data-fn', '')
            details['last_name'] = person_card.get('data-ln', '')

            if not details['first_name'] and not details['last_name']:
                name_elem = soup.find('h1', class_='oh1')
                if name_elem:
                    name_parts = name_elem.text.strip().split()
                    details['first_name'] = name_parts[0] if name_parts else ""
                    details['last_name'] = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

            phone_section = soup.find('h5', string='Phone Numbers')
            if phone_section:
                parent_div = phone_section.find_parent('div', class_='row')
                if parent_div:
                    phone_spans = parent_div.find_all('span', itemprop='telephone')
                    for span in phone_spans:
                        phone = span.text.strip()
                        if phone and phone not in details['phones']:
                            details['phones'].append(phone)

            bio_phones = person_card.select('a[href*="/find/phone/"]')
            for phone_link in bio_phones:
                phone_text = phone_link.text.strip()
                if phone_text and re.match(r'\(\d{3}\) \d{3}-\d{4}', phone_text) and phone_text not in details['phones']:
                    details['phones'].append(phone_text)

            email_section = soup.find('h5', string='Email Addresses')
            if email_section:
                parent_div = email_section.find_parent('div', class_='row')
                if parent_div:
                    email_div = parent_div.find('div', class_=lambda c: c and 'col' in c and '@' in parent_div.text)
                    if email_div:
                        email_text = email_div.text.strip()
                        found_emails = re.findall(r'[\w\.-]+@[\w\.-]+', email_text)
                        for email in found_emails:
                            if email not in details['emails']:
                                details['emails'].append(email)

            bio_email_span = person_card.find('span', class_='bio-hl', string=re.compile(r'\S+@\S+'))
            if bio_email_span:
                email = bio_email_span.text.strip()
                if email not in details['emails']:
                    details['emails'].append(email)

            # Find the current address link
            address_link = soup.find('a', attrs={'data-link-to-more': 'address'})
            if address_link:
                street = address_link.find('span', itemprop='streetAddress')
                locality = address_link.find('span', itemprop='addressLocality')
                region = address_link.find('span', itemprop='addressRegion')
                postal_code = address_link.find('span', itemprop='postalCode')

                if all([street, locality, region, postal_code]):
                    full_address = f"{street.text.strip()}, {locality.text.strip()}, {region.text.strip()} {postal_code.text.strip()}"
                    if full_address not in details['addresses']:
                        details['addresses'].append(full_address)

                    # Check if current address matches searched address
                    if searched_address:
                        # Normalize both addresses for comparison
                        current_normalized = re.sub(r'\s+', ' ', full_address.lower().strip())
                        searched_normalized = re.sub(r'\s+', ' ', searched_address.lower().strip())

                        if current_normalized != searched_normalized:
                            details['skip'] = True
                            details['skip_reason'] = f"Current address mismatch: profile has '{full_address}', searched for '{searched_address}'"
                            logger.info(f"Skipping profile due to address mismatch: {details['skip_reason']}")
                            if job_id in processing_jobs:
                                processing_jobs[job_id]['results'].append({
                                    'type': 'status',
                                    'message': f"Skipped: {details.get('first_name', '')} {details.get('last_name', '')} - {details['skip_reason']}"
                                })
                else:
                    # If current address components not found, skip
                    details['skip'] = True
                    details['skip_reason'] = "Current address components not found in profile"
                    logger.info(f"Skipping profile: {details['skip_reason']}")
                    if job_id in processing_jobs:
                        processing_jobs[job_id]['results'].append({
                            'type': 'status',
                            'message': f"Skipped: {details.get('first_name', '')} {details.get('last_name', '')} - {details['skip_reason']}"
                        })
            else:
                # If no current address link found, skip
                details['skip'] = True
                details['skip_reason'] = "No current address link found in profile"
                logger.info(f"Skipping profile: {details['skip_reason']}")
                if job_id in processing_jobs:
                    processing_jobs[job_id]['results'].append({
                        'type': 'status',
                        'message': f"Skipped: {details.get('first_name', '')} {details.get('last_name', '')} - {details['skip_reason']}"
                    })

            if not details['addresses']:
                address_link = address_section.find_next('a', href=re.compile(r'/find/address/')) if address_section else None
                if address_link:
                    address_parts = [part.strip() for part in address_link.stripped_strings]
                    full_address = ' '.join(address_parts)
                    if full_address and full_address not in details['addresses']:
                        details['addresses'].append(full_address)

            bio_address_links = person_card.select('a[data-link-to-more="bio-address"]')
            processed_hrefs = set()
            for link in bio_address_links:
                href = link.get('href')
                if href in processed_hrefs:
                    continue

                all_links_for_href = person_card.select(f'a[href="{href}"]')
                full_address_parts = []
                for part_link in all_links_for_href:
                    full_address_parts.append(' '.join(part_link.stripped_strings))

                processed_hrefs.add(href)
                full_address = ' '.join(full_address_parts)
                if full_address and full_address not in details['addresses']:
                    details['addresses'].append(full_address)

            # Update stats & push details event only if not skipped
            if not details.get('skip') and job_id in processing_jobs:
                processing_jobs[job_id]['stats']['phones_found'] += len(details['phones'])
                processing_jobs[job_id]['stats']['emails_found'] += len(details['emails'])
                processing_jobs[job_id]['stats']['addresses_found'] += len(details['addresses'])

                processing_jobs[job_id]['results'].append({
                    'type': 'details',
                    'data': {
                        'first_name': details['first_name'],
                        'last_name': details['last_name'],
                        'phones': details['phones'],
                        'emails': details['emails'],
                        'addresses': details['addresses'],
                        'url': details['url']
                    }
                })

        except Exception as e:
            logger.exception(f"Error fetching person details: {str(e)}")
            details['error'] = str(e)

        return details

    def process_address(self, row: Dict[str, Any], row_index: int, job_id: str, address: str, city: str, state: str, zip_code: str, county: str = "", existing_urls: set = None) -> List[Dict[str, Any]]:
        if existing_urls is None:
            existing_urls = set()

        all_results = []
        expanded_addresses = self.expand_address(address)

        logger.info(f"Original address: {address}, Expanded to: {expanded_addresses}")

        if job_id in processing_jobs:
            processing_jobs[job_id]['results'].append({
                'type': 'status',
                'message': f"Original address: {address}, Expanded to: {expanded_addresses}"
            })

        for exp_address in expanded_addresses:
            if job_id in processing_jobs and processing_jobs[job_id]['cancelled']:
                break

            if job_id in processing_jobs:
                processing_jobs[job_id]['stats']['addresses_processed'] += 1

            page = 1
            all_person_summaries = []
            while True:
                if job_id in processing_jobs and processing_jobs[job_id]['cancelled']:
                    break

                soup = self.search_address(exp_address, city, state, zip_code, page)
                if not soup:
                    break

                person_summaries = self.parse_search_results(soup, job_id)
                if not person_summaries:
                    break

                all_person_summaries.extend(person_summaries)

                # Check for next page button
                next_button = soup.find('a', id='btnNextPage')
                if not next_button:
                    break

                page += 1
                time.sleep(0.5)  # delay between pages

            # Update people_found stat for this address
            if job_id in processing_jobs:
                processing_jobs[job_id]['stats']['people_found'] += len(all_person_summaries)
                processing_jobs[job_id]['results'].append({
                    'type': 'status',
                    'message': f"Found {len(all_person_summaries)} people for this address, starting to scrape details..."
                })

            for person in all_person_summaries:
                if job_id in processing_jobs and processing_jobs[job_id]['cancelled']:
                    break

                # Check if this person's URL has already been scraped for this job
                person_url = f"{TRUE_PEOPLE_SEARCH_BASE}{person['detail_url']}"
                logger.info(f"Checking URL: {person_url}")
                if person_url in existing_urls:
                    # Person already scraped, skip
                    logger.info(f"Skipping already scraped person: {person.get('first_name', '')} {person.get('last_name', '')} - URL: {person_url}")
                    if job_id in processing_jobs:
                        processing_jobs[job_id]['results'].append({
                            'type': 'status',
                            'message': f"Skipping already scraped: {person.get('first_name', '')} {person.get('last_name', '')}"
                        })
                    continue
                else:
                    logger.info(f"URL not found in existing URLs, will scrape: {person_url}")

                if job_id in processing_jobs:
                    processing_jobs[job_id]['results'].append({
                        'type': 'status',
                        'message': f"Getting details for: {person.get('first_name', '')} {person.get('last_name', '')}"
                    })

                searched_address = f"{exp_address}, {city}, {state} {zip_code}"
                details = self.get_person_details(person['detail_url'], searched_address)

                if details.get('skip'):
                    # Skip this person due to address mismatch
                    continue

                person.update(details)
                all_results.append(person)

                # Save result immediately after scraping
                result_row = row.copy()
                result_row["Owner's First Name"] = person.get('first_name', '')
                result_row["Owner's Last Name"] = person.get('last_name', '')
                result_row['phones'] = person.get('phones', [])
                result_row['Mailing Address'] = '; '.join(person.get('addresses', []))
                result_row['Email Address'] = '; '.join(person.get('emails', []))
                result_row['URL'] = person.get('url', '')
                result_row['full address'] = address
                save_result_to_supabase(job_id, row_index, result_row)

                time.sleep(0.5)  # delay between detail fetches

            time.sleep(0.5)  # respectful delay between address searches

        # If no people found, save empty result
        if not all_results:
            result_row = row.copy()
            result_row["Owner's First Name"] = ''
            result_row["Owner's Last Name"] = ''
            result_row['phones'] = []
            result_row['Mailing Address'] = ''
            result_row['Email Address'] = ''
            result_row['URL'] = ''
            result_row['full address'] = address
            save_result_to_supabase(job_id, row_index, result_row)

        return all_results


# Initialize the skip tracer (API key may be None in dev)
skip_tracer = SkipTracer(ZENROWS_API_KEY)


def test_zenrows_connection():
    test_url = "https://www.truepeoplesearch.com"
    params = {
        'url': test_url,
        'apikey': ZENROWS_API_KEY,
        'js_render': 'true',
        'premium_proxy': 'true',
        'proxy_country': 'us',
        'wait': str(ZENROWS_WAIT_MS),
    }
    try:
        response = requests.get(ZENROWS_BASE_URL, params=params, timeout=30)
        if response.status_code == 200:
            logger.info("Zenrows connection test successful")
            logger.info(f"Response (truncated): {response.text[:120]}...")
            return True
        else:
            logger.error(f"Zenrows connection test failed: {response.status_code}")
            logger.debug(f"Response content: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Zenrows connection test error: {str(e)}")
        return False


def compute_csv_hash(file_bytes: bytes) -> str:
    """Compute SHA256 hash of CSV content for job resumption."""
    return hashlib.sha256(file_bytes).hexdigest()


def check_existing_job(csv_hash: str) -> Optional[Dict[str, Any]]:
    """Check if a job with the same CSV hash exists and is resumable."""
    if not supabase:
        return None
    try:
        response = supabase.table('jobs').select('*').eq('csv_hash', csv_hash).eq('status', 'in_progress').execute()
        if response.data:
            return response.data[0]
    except Exception as e:
        logger.error(f"Error checking existing job: {e}")
    return None


def create_job_in_supabase(job_id: str, csv_hash: str, total_rows: int) -> bool:
    """Create a new job entry in Supabase."""
    if not supabase:
        return False
    try:
        supabase.table('jobs').insert({
            'job_id': job_id,
            'csv_hash': csv_hash,
            'status': 'in_progress',
            'total_rows': total_rows,
            'current_row': 0,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'stats': {
                'addresses_processed': 0,
                'people_found': 0,
                'phones_found': 0,
                'emails_found': 0,
                'addresses_found': 0
            }
        }).execute()
        return True
    except Exception as e:
        logger.error(f"Error creating job in Supabase: {e}")
        return False


def update_job_progress(job_id: str, current_row: int, stats: Dict[str, Any]):
    """Update job progress in Supabase."""
    if not supabase:
        return
    try:
        supabase.table('jobs').update({
            'current_row': current_row,
            'stats': stats,
            'updated_at': datetime.now(timezone.utc).isoformat()
        }).eq('job_id', job_id).execute()
    except Exception as e:
        logger.error(f"Error updating job progress: {e}")


def save_result_to_supabase(job_id: str, row_index: int, result_data: Dict[str, Any]):
    """Save result row to Supabase."""
    if not supabase:
        return
    try:
        supabase.table('results').insert({
            'job_id': job_id,
            'row_index': row_index,
            'data': result_data,
            'created_at': datetime.now(timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"Error saving result to Supabase: {e}")


def complete_job_in_supabase(job_id: str, status: str = 'completed'):
    """Mark job as completed or cancelled in Supabase."""
    if not supabase:
        return
    try:
        supabase.table('jobs').update({
            'status': status,
            'completed_at': datetime.now(timezone.utc).isoformat()
        }).eq('job_id', job_id).execute()
    except Exception as e:
        logger.error(f"Error completing job in Supabase: {e}")


# run a quick test at startup (safe to ignore failure)
test_zenrows_connection()


def clean_phone_number(phone: str) -> str:
    """Remove all non-digit characters from phone number."""
    return re.sub(r'\D', '', phone)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if not file.filename.lower().endswith('.csv'):
        return jsonify({'error': 'File must be a CSV'}), 400

    # Read file bytes immediately within the request context
    try:
        file_bytes = file.read()
    except Exception as e:
        logger.exception("Failed to read uploaded file")
        return jsonify({'error': 'Failed to read uploaded file'}), 400

    if not file_bytes:
        return jsonify({'error': 'Empty file uploaded'}), 400

    # Compute CSV hash for job resumption
    csv_hash = compute_csv_hash(file_bytes)

    # Check if job already exists

    existing_job = check_existing_job(csv_hash)
    if existing_job:
        job_id = existing_job['job_id']
        # Resume existing job
        processing_jobs[job_id] = {
            'active': True,
            'cancelled': False,
            'results': deque(maxlen=500),
            'current_row': existing_job['current_row'],
            'total_rows': existing_job['total_rows'],
            'stats': existing_job['stats'],
            'csv_hash': csv_hash,
            'resumed': True
        }
        message = f'Resuming existing job from row {existing_job["current_row"] + 1}'
    else:
        # Create new job
        job_id = f"job_{int(time.time())}_{random.randint(1000, 9999)}"
        # Decode to get total_rows
        stream = io.StringIO(file_bytes.decode('utf-8', errors='replace'), newline=None)
        csv_input = csv.DictReader(stream)
        rows = list(csv_input)
        total_rows = len(rows)

        processing_jobs[job_id] = {
            'active': True,
            'cancelled': False,
            'results': deque(maxlen=500),
            'current_row': 0,
            'total_rows': total_rows,
            'stats': {
                'addresses_processed': 0,
                'people_found': 0,
                'phones_found': 0,
                'emails_found': 0,
                'addresses_found': 0
            },
            'csv_hash': csv_hash,
            'resumed': False
        }
        create_job_in_supabase(job_id, csv_hash, total_rows)
        message = 'Processing started'

    def process_file(job_id_local: str, file_bytes_local: bytes):
        try:
            # Decode safely and parse CSV
            stream = io.StringIO(file_bytes_local.decode('utf-8', errors='replace'), newline=None)
            csv_input = csv.DictReader(stream)
            rows = list(csv_input)
            job = processing_jobs.get(job_id_local)
            if not job:
                logger.error("Job missing during processing")
                return

            job['total_rows'] = len(rows)

            # If no rows, finish early
            if job['total_rows'] == 0:
                job['results'].append({
                    'type': 'complete',
                    'message': 'No rows to process',
                    'stats': job['stats']
                })
                job['active'] = False
                complete_job_in_supabase(job_id_local, 'completed')
                return

            # Fetch all existing URLs once for the entire job (global deduplication)
            existing_urls = set()
            if supabase:
                try:
                    response = supabase.table('results').select('data').execute()
                    existing_urls = set(result['data'].get('URL', '') for result in response.data if result['data'].get('URL'))
                    logger.info(f"Fetched {len(existing_urls)} existing URLs for global deduplication")
                except Exception as e:
                    logger.error(f"Error fetching existing URLs: {e}")

            fieldnames = list(rows[0].keys()) + [
                "Owner's First Name", "Owner's Last Name", 'Phone Number(s)',
                'Mailing Address', 'Email Address', 'URL', 'full address'
            ]

            # Determine start index for resumption
            start_index = job.get('current_row', 0)
            if job.get('resumed'):
                job['results'].append({
                    'type': 'status',
                    'message': f'Resuming from row {start_index + 1}'
                })

            for i in range(start_index, len(rows)):
                if job['cancelled']:
                    break

                row = rows[i]

                # Check if this row has already been processed
                if supabase:
                    existing_results = supabase.table('results').select('id').eq('job_id', job_id_local).eq('row_index', i).limit(1).execute()
                    if existing_results.data:
                        # Row already processed, skip
                        job['current_row'] = i + 1
                        update_job_progress(job_id_local, job['current_row'], job['stats'])
                        continue

                job['current_row'] = i + 1

                address = row.get('Address', '') or row.get('address', '')
                city = row.get('City', '') or row.get('city', '')
                state = row.get('State', '') or row.get('state', '')
                zip_code = row.get('Zip', '') or row.get('zip', '')
                county = row.get('County', '') or row.get('county', '')

                if address and city and state:
                    logger.info(f"Processing {i+1}/{job['total_rows']}: {address}, {city}, {state} {zip_code}")

                    progress_data = {
                        'type': 'progress',
                        'current': i + 1,
                        'total': job['total_rows'],
                        'address': address,
                        'stats': job['stats']
                    }
                    job['results'].append(progress_data)

                    people_data = skip_tracer.process_address(row, i, job_id_local, address, city, state, zip_code, county, existing_urls)

                else:
                    # Empty result for missing fields
                    result_row = row.copy()
                    result_row["Owner's First Name"] = ''
                    result_row["Owner's Last Name"] = ''
                    result_row['Phone Number(s)'] = ''
                    result_row['Mailing Address'] = ''
                    result_row['Email Address'] = ''
                    result_row['URL'] = ''
                    result_row['full address'] = ''
                    save_result_to_supabase(job_id_local, i, result_row)

                # Checkpoint progress
                update_job_progress(job_id_local, job['current_row'], job['stats'])

            # final event
            if not job['cancelled']:
                job['results'].append({
                    'type': 'complete',
                    'message': 'Processing completed successfully',
                    'stats': job['stats']
                })
                complete_job_in_supabase(job_id_local, 'completed')
            else:
                job['results'].append({
                    'type': 'cancelled',
                    'message': 'Processing cancelled by user',
                    'stats': job['stats']
                })
                complete_job_in_supabase(job_id_local, 'cancelled')

        except Exception as e:
            logger.exception(f"Error processing file for job {job_id_local}: {str(e)}")
            if job_id_local in processing_jobs:
                processing_jobs[job_id_local]['results'].append({
                    'type': 'error',
                    'message': f'Error processing file: {str(e)}'
                })
            complete_job_in_supabase(job_id_local, 'error')
        finally:
            if job_id_local in processing_jobs:
                processing_jobs[job_id_local]['active'] = False

    # Start processing thread and return immediately
    thread = threading.Thread(target=process_file, args=(job_id, file_bytes), name=job_id)
    thread.daemon = True
    thread.start()

    return jsonify({'message': 'Processing started', 'job_id': job_id}), 202


@app.route('/stream/<job_id>')
def stream(job_id):
    def generate():
        last_index = 0
        last_sent = time.time()

        if job_id not in processing_jobs:
            # Check Supabase if not in memory
            if supabase:
                try:
                    response = supabase.table('jobs').select('*').eq('job_id', job_id).execute()
                    if response.data:
                        job_data = response.data[0]
                        if job_data['status'] == 'in_progress':
                            # Job was in progress but server restarted, mark as error
                            complete_job_in_supabase(job_id, 'error')
                            yield f"data: {json.dumps({'type':'error','message':'Processing was interrupted due to server restart. Please check your results.'})}\n\n"
                        else:
                            # Job completed or cancelled
                            yield f"data: {json.dumps({'type':'complete','message':'Job is not currently active.'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type':'error','message':'Invalid job_id'})}\n\n"
                except Exception as e:
                    logger.error(f"Error fetching job from Supabase: {e}")
                    yield f"data: {json.dumps({'type':'error','message':'Invalid job_id'})}\n\n"
            else:
                yield f"data: {json.dumps({'type':'error','message':'Invalid job_id'})}\n\n"
            return

        while True:
            if job_id not in processing_jobs:
                break

            job = processing_jobs[job_id]

            # snapshot the deque so iteration is consistent
            results_snapshot = list(job['results'])

            while last_index < len(results_snapshot):
                result = results_snapshot[last_index]
                last_index += 1
                last_sent = time.time()
                try:
                    yield f"data: {json.dumps(result)}\n\n"
                except GeneratorExit:
                    return

            # break if done
            if not job['active'] and last_index >= len(results_snapshot):
                break

            # heartbeat to keep worker alive for Gunicorn timeouts
            if time.time() - last_sent > HEARTBEAT_INTERVAL:
                heartbeat = {'type': 'heartbeat', 'ts': int(time.time())}
                yield f"data: {json.dumps(heartbeat)}\n\n"
                last_sent = time.time()

            time.sleep(0.5)

    return Response(generate(), mimetype='text/event-stream')


@app.route('/cancel', methods=['POST'])
def cancel_processing():
    payload = request.get_json(silent=True) or {}
    job_id = payload.get('job_id')
    if job_id and job_id in processing_jobs:
        processing_jobs[job_id]['cancelled'] = True
        return jsonify({'message': 'Cancellation requested'})
    return jsonify({'error': 'Invalid job ID'}), 400


@app.route('/download/<job_id>')
def download_file(job_id):
    if not supabase:
        return jsonify({'error': 'Database not available'}), 500

    try:
        # Fetch results from Supabase
        response = supabase.table('results').select('data').eq('job_id', job_id).order('row_index').execute()
        if not response.data:
            return jsonify({'error': 'No results found for this job'}), 404

        # Process data to split phone numbers into separate columns
        processed_data = []
        max_phones = 0

        for result in response.data:
            data = result['data'].copy()
            phones = data.get('phones', [])
            if not phones and 'Phone Number(s)' in data:
                phones_str = data['Phone Number(s)']
                phones = [p.strip() for p in phones_str.split(';') if p.strip()]

            # Update max phones count
            max_phones = max(max_phones, len(phones))

            # Create new data dict with only required columns
            filtered_data = {}

            # Add required fields
            filtered_data["Owner's First Name"] = data.get("Owner's First Name", '')
            filtered_data["Owner's Last Name"] = data.get("Owner's Last Name", '')
            filtered_data['Mailing Address'] = data.get('Mailing Address', '')
            filtered_data['Email Address'] = data.get('Email Address', '')
            filtered_data['URL'] = data.get('URL', '')

            # Add individual phone columns
            for i, phone in enumerate(phones):
                filtered_data[f'Phone Number({i+1})'] = clean_phone_number(phone)

            processed_data.append(filtered_data)

        # Ensure all rows have the same number of phone columns (fill with empty strings)
        for data in processed_data:
            for i in range(1, max_phones + 1):
                if f'Phone Number({i})' not in data:
                    data[f'Phone Number({i})'] = ''

        # Create CSV in memory
        output = io.StringIO()
        if processed_data:
            writer = csv.DictWriter(output, fieldnames=processed_data[0].keys())
            writer.writeheader()
            for data in processed_data:
                writer.writerow(data)

        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={job_id}_results.csv'}
        )
    except Exception as e:
        logger.exception(f"Error downloading job {job_id}: {e}")
        return jsonify({'error': 'Failed to generate download'}), 500


@app.route('/status/<job_id>')
def get_status(job_id):
    if job_id in processing_jobs:
        job = processing_jobs[job_id]
        return jsonify({
            'active': job['active'],
            'cancelled': job['cancelled'],
            'current_row': job['current_row'],
            'total_rows': job['total_rows'],
            'stats': job['stats']
        })

    # Check Supabase if not in memory
    if supabase:
        try:
            response = supabase.table('jobs').select('*').eq('job_id', job_id).execute()
            if response.data:
                job_data = response.data[0]
                return jsonify({
                    'active': False,  # Not active since not in memory
                    'cancelled': job_data['status'] == 'cancelled',
                    'current_row': job_data['current_row'],
                    'total_rows': job_data['total_rows'],
                    'stats': job_data['stats']
                })
        except Exception as e:
            logger.error(f"Error fetching job from Supabase: {e}")

    return jsonify({'error': 'Invalid job ID'}), 404


@app.template_filter('datetimeformat')
def datetimeformat(value, format='%Y-%m-%d %H:%M:%S'):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).strftime(format)
    elif isinstance(value, str):
        try:
            # Try to parse ISO format
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return dt.strftime(format)
        except ValueError:
            return value
    return value


@app.route('/downloads')
def list_downloads():
    if not supabase:
        return "Database not available", 500

    try:
        # Fetch past jobs from Supabase (completed, cancelled, error)
        response = supabase.table('jobs').select('job_id, created_at, status').in_('status', ['completed', 'cancelled', 'error']).order('created_at', desc=True).execute()
        files = []
        for job in response.data:
            files.append({
                'name': f"{job['job_id']}_results.csv",
                'job_id': job['job_id'],
                'creation_time': job['created_at'],  # This is ISO string, template filter will handle it
                'status': job['status']
            })
        return render_template('download_jobs.html', files=files)
    except Exception as e:
        logger.exception("Error fetching jobs list: {e}")
        return "Error loading jobs", 500


@app.route('/preview/<job_id>')
def preview_job(job_id):
    if not supabase:
        return jsonify({'error': 'Database not available'}), 500

    try:
        # Fetch first 10 results from Supabase
        response = supabase.table('results').select('data').eq('job_id', job_id).order('row_index').limit(10).execute()
        if not response.data:
            return jsonify({'error': 'No results found for this job'}), 404

        # Process data to split phone numbers into separate columns
        processed_data = []
        max_phones = 0

        for result in response.data:
            data = result['data'].copy()
            phones = data.get('phones', [])
            if not phones and 'Phone Number(s)' in data:
                phones_str = data['Phone Number(s)']
                phones = [p.strip() for p in phones_str.split(';') if p.strip()]

            # Update max phones count
            max_phones = max(max_phones, len(phones))

            # Remove the combined phone columns
            if 'phones' in data:
                del data['phones']
            if 'Phone Number(s)' in data:
                del data['Phone Number(s)']

            # Add individual phone columns
            for i, phone in enumerate(phones):
                data[f'Phone Number({i+1})'] = clean_phone_number(phone)

            processed_data.append(data)

        # Ensure all rows have the same number of phone columns (fill with empty strings)
        for data in processed_data:
            for i in range(1, max_phones + 1):
                if f'Phone Number({i})' not in data:
                    data[f'Phone Number({i})'] = ''

        headers = list(processed_data[0].keys()) if processed_data else []
        rows = [list(data.values()) for data in processed_data]

        return jsonify({
            'headers': headers,
            'rows': rows
        })
    except Exception as e:
        logger.exception(f"Error previewing job {job_id}: {e}")
        return jsonify({'error': 'Failed to preview job'}), 500


@app.route('/view/<job_id>')
def view_job(job_id):
    if not supabase:
        return "Database not available", 500

    try:
        # Fetch all results from Supabase
        response = supabase.table('results').select('data').eq('job_id', job_id).order('row_index').execute()
        if not response.data:
            return "No results found", 404

        # Process data to split phone numbers into separate columns
        processed_data = []
        max_phones = 0

        for result in response.data:
            data = result['data'].copy()
            phones = data.get('phones', [])
            if not phones and 'Phone Number(s)' in data:
                phones_str = data['Phone Number(s)']
                phones = [p.strip() for p in phones_str.split(';') if p.strip()]

            # Update max phones count
            max_phones = max(max_phones, len(phones))

            # Remove the combined phone columns
            if 'phones' in data:
                del data['phones']
            if 'Phone Number(s)' in data:
                del data['Phone Number(s)']

            # Add individual phone columns
            for i, phone in enumerate(phones):
                data[f'Phone Number({i+1})'] = clean_phone_number(phone)

            processed_data.append(data)

        # Ensure all rows have the same number of phone columns (fill with empty strings)
        for data in processed_data:
            for i in range(1, max_phones + 1):
                if f'Phone Number({i})' not in data:
                    data[f'Phone Number({i})'] = ''

        headers = list(processed_data[0].keys()) if processed_data else []
        rows = [list(data.values()) for data in processed_data]

        return render_template('job_results.html', job_id=job_id, headers=headers, rows=rows)
    except Exception as e:
        logger.exception(f"Error viewing job {job_id}: {e}")
        return "Error loading job", 500


# duplicate /download route removed — use download_file above
if __name__ == '__main__':
    # For local testing - in production use gunicorn
    app.run(debug=True, threaded=True, port=int(os.getenv("PORT", 5001)))
