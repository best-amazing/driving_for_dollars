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

def rearrange_name(name_text: str) -> Tuple[str, str]:
    """
    Rearrange name if it's in LAST FIRST format or LAST FIRST MIDDLE format to FIRST LAST or FIRST MIDDLE LAST.
    Otherwise, keep as FIRST LAST...
    """
    name_parts = name_text.split()
    if len(name_parts) == 3 and len(name_parts[2]) == 1 and name_parts[2].isalpha():
        # Assume LAST FIRST MIDDLE -> FIRST MIDDLE LAST
        first_name = name_parts[1]
        last_name = f"{name_parts[2]} {name_parts[0]}"
        return first_name, last_name
    elif len(name_parts) == 2:
        # Assume LAST FIRST -> FIRST LAST
        first_name = name_parts[1]
        last_name = name_parts[0]
        return first_name, last_name
    else:
        # Default: first part is first name, rest is last name
        first_name = name_parts[0] if name_parts else ""
        last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
        return first_name, last_name

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
NATIONAL_PUBLIC_DATA_BASE = "https://nationalpublicdata.com/people/"
ZENROWS_WAIT_MS = int(os.getenv("ZENROWS_WAIT_MS", "250"))  # reduced from 500ms for faster scraping
ZENROWS_TIMEOUT = int(os.getenv("ZENROWS_TIMEOUT", "20"))  # reduced from 60s - fail faster on timeouts
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "15.0"))

# Pagination limits for search results
MIN_PAGES = int(os.getenv("MIN_PAGES", "1"))  # minimum pages to scrape
MAX_PAGES = int(os.getenv("MAX_PAGES", "1"))  # maximum pages to scrape (changed from 2 to 1)

# Maximum people to scrape per address
MAX_PEOPLE_PER_ADDRESS = int(os.getenv("MAX_PEOPLE_PER_ADDRESS", "5"))  # limit people per address

# Concurrent request settings
MAX_CONCURRENT_WORKERS = int(os.getenv("MAX_CONCURRENT_WORKERS", "10"))  # parallel workers (5-10 recommended)
MIN_CONCURRENT_WORKERS = int(os.getenv("MIN_CONCURRENT_WORKERS", "1"))  # minimum parallel workers

# Job cleanup settings
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "3600"))  # Keep completed jobs for 1 hour (default)

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Global dictionary to track the state of multiple processing jobs (in-memory)
processing_jobs: Dict[str, Dict[str, Any]] = {}
RESULTS_FOLDER = 'results'
os.makedirs(RESULTS_FOLDER, exist_ok=True)

# Global flag to track ZenRows credit exhaustion
zenrows_credits_exhausted = False
zenrows_credits_lock = threading.Lock()

# Lock for thread-safe operations on processing_jobs
job_results_lock = threading.Lock()

# Lock for thread-safe stats updates
stats_lock = threading.Lock()

# Shared semaphore for rate limiting across all SkipTracer instances
# This ensures we don't exceed rate limits while allowing parallel processing
request_semaphore = threading.Semaphore(MAX_CONCURRENT_WORKERS)

# Shared last request time for rate limiting across all threads
shared_last_request_time = time.time()
request_time_lock = threading.Lock()


def cleanup_old_jobs():
    """
    Remove completed jobs older than JOB_TTL_SECONDS from memory.
    This prevents memory leaks from accumulating old job data.
    """
    current_time = time.time()
    jobs_to_remove = []

    for job_id, job in processing_jobs.items():
        # Only clean up inactive (completed/cancelled) jobs
        if not job.get('active', False):
            # Check if job has a completion time
            completed_at = job.get('completed_at')
            if completed_at:
                # Parse ISO format timestamp
                try:
                    if isinstance(completed_at, str):
                        completed_timestamp = datetime.fromisoformat(completed_at.replace('Z', '+00:00')).timestamp()
                    else:
                        completed_timestamp = completed_at

                    if current_time - completed_timestamp > JOB_TTL_SECONDS:
                        jobs_to_remove.append(job_id)
                except (ValueError, TypeError):
                    # If we can't parse, use creation time instead
                    created_at = job.get('created_at', current_time)
                    if isinstance(created_at, str):
                        try:
                            created_timestamp = datetime.fromisoformat(created_at.replace('Z', '+00:00')).timestamp()
                            if current_time - created_timestamp > JOB_TTL_SECONDS * 2:  # Double TTL for jobs without completion time
                                jobs_to_remove.append(job_id)
                        except:
                            pass
            else:
                # For jobs without completion time, use a default TTL from creation
                created_at = job.get('created_at')
                if created_at:
                    try:
                        if isinstance(created_at, str):
                            created_timestamp = datetime.fromisoformat(created_at.replace('Z', '+00:00')).timestamp()
                            if current_time - created_timestamp > JOB_TTL_SECONDS * 2:
                                jobs_to_remove.append(job_id)
                    except:
                        pass

    # Remove old jobs
    for job_id in jobs_to_remove:
        del processing_jobs[job_id]
        logger.info(f"Cleaned up old job: {job_id}")

    if jobs_to_remove:
        logger.info(f"Cleaned up {len(jobs_to_remove)} old jobs from memory")

    return len(jobs_to_remove)


def start_cleanup_thread():
    """
    Start a background thread that periodically cleans up old jobs.
    """
    def cleanup_loop():
        while True:
            time.sleep(JOB_TTL_SECONDS // 2)  # Run cleanup every half TTL period
            try:
                cleanup_old_jobs()
            except Exception as e:
                logger.error(f"Error in cleanup thread: {e}")

    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()
    logger.info(f"Started job cleanup thread (TTL: {JOB_TTL_SECONDS}s)")
    return cleanup_thread


# Start the cleanup thread when the module loads
cleanup_thread = start_cleanup_thread()


class SkipTracer:
    def __init__(self, api_key: str, platform: str = 'truepeoplesearch', job_id: str = None):
        self.api_key = api_key
        self.platform = platform
        self.job_id = job_id  # Store job_id for parallel processing
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        self.request_count = 0
        self.last_request_time = time.time()
        # Use shared semaphore for rate limiting (no blocking)

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

    def make_request(self, url: str, max_retries: int = 2) -> Optional[requests.Response]:
        """
        Make a request using Zenrows for truepeoplesearch or direct requests for nationalpublicdata,
        with retry logic and defensive error handling.

        Defensive improvements:
        - Rate-limits to avoid 429s
        - Exponential backoff with jitter on retry
        - Catches RecursionError (observed in SSLContext recursion), SSLError, and RequestException
        - Returns None on unrecoverable errors so callers can continue gracefully
        - Reduced retries from 3 to 2 to fail faster and save time
        """
        if self.platform == 'nationalpublicdata':
            # Use direct requests for nationalpublicdata
            for attempt in range(max_retries):
                try:
                    # Add a small delay between retries (exponential backoff + jitter)
                    if attempt > 0:
                        wait_time = (2 ** attempt) + random.uniform(0, 1)
                        logger.info(f"Waiting {wait_time:.2f} seconds before retry (attempt {attempt + 1})...")
                        time.sleep(wait_time)

                    response = self.session.get(url, timeout=60)

                    if response.status_code == 200:
                        return response
                    else:
                        logger.error(f"Direct request failed: {response.status_code} for {url}")
                        if response.text:
                            logger.debug(f"Response content (truncated): {response.text[:500]}")
                        # Retry on failure
                        continue

                except requests.exceptions.RequestException as req_e:
                    logger.error(f"Attempt {attempt + 1}: Direct request error: {req_e}")
                    # For timeout errors, add extra delay before retry
                    if "Read timed out" in str(req_e) or "timeout" in str(req_e).lower():
                        logger.info("Timeout detected, adding extra delay...")
                        time.sleep(5)  # Extra delay for timeout recovery
                    continue
                except Exception as e:
                    logger.exception(f"Unexpected error during direct request attempt {attempt + 1}: {e}")
                    time.sleep(random.uniform(0.5, 1.5))
                    continue

            # If all retries failed, return None
            logger.error(f"All {max_retries} attempts to fetch {url} failed.")
            return None
        else:
            # Use Zenrows for truepeoplesearch
            with request_semaphore:
                # Global rate limiting to avoid 429 errors across all threads
                global shared_last_request_time
                with request_time_lock:
                    current_time = time.time()
                    time_since_last_request = current_time - shared_last_request_time

                    # Ensure at least 0.1 second between requests to avoid rate limiting (faster)
                    if time_since_last_request < 0.1:
                        time.sleep(0.1 - time_since_last_request)

                    # Update shared last request time
                    shared_last_request_time = time.time()

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
                            timeout=ZENROWS_TIMEOUT  # configurable timeout (default 30s)
                        )
                    except RecursionError as rec_err:
                        # Observed when SSLContext property recurses (likely SSL/monkeypatch issue).
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
                        # For timeout errors, add small delay before retry (reduced from 10s+)
                        if "Read timed out" in str(req_e) or "timeout" in str(req_e).lower():
                            logger.info("Timeout detected, adding small delay...")
                            time.sleep(2 + attempt)  # Minimal delay: 2s, 3s, etc.
                        response = None

                    # If we got a response, handle status codes
                    if response is not None:
                        if response.status_code == 200:
                            self.request_count += 1
                            return response
                        elif response.status_code == 402:
                            # CREDITS EXHAUSTED - Set global flag and stop processing
                            global zenrows_credits_exhausted
                            with zenrows_credits_lock:
                                zenrows_credits_exhausted = True
                            logger.critical("ZENROWS CREDITS EXHAUSTED! HTTP 402 - Payment Required")

                            # Notify the job about credit exhaustion
                            job_id = threading.current_thread().name
                            if job_id in processing_jobs:
                                processing_jobs[job_id]['results'].append({
                                    'type': 'error',
                                    'message': '⚠️ ZENROWS CREDITS EXHAUSTED! Please add more credits to continue scraping. Visit: https://www.zenrows.com/dashboard'
                                })
                                processing_jobs[job_id]['credits_exhausted'] = True
                            return None
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
        if self.platform == 'nationalpublicdata':
            # National Public Data doesn't support address-only searches
            # This would typically be called with owner_name, so we can handle it in search_name_address
            return None

        search_url = f"{TRUE_PEOPLE_SEARCH_BASE}/results"
        query_params = {
            'streetaddress': address,
            'citystatezip': f"{city}, {state} {zip_code}"
        }
        if page > 1:
            query_params['page'] = page

        # Use self.job_id for parallel processing, fallback to thread name
        job_id = self.job_id if self.job_id else threading.current_thread().name
        if job_id in processing_jobs:
            processing_jobs[job_id]['results'].append({
                'type': 'status',
                'message': f"Searching for: {address}, {city}, {state} {zip_code}",
                'url': f"{search_url}?{urllib.parse.urlencode(query_params)}"
            })

        target_url = f"{search_url}?{urllib.parse.urlencode(query_params)}"
        logger.info(f"Requesting URL: {target_url}")

        response = self.make_request(target_url)
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

    def search_name_address(self, name: str, address: str, city: str, state: str, zip_code: str, page: int = 1) -> Optional[BeautifulSoup]:
        if self.platform == 'nationalpublicdata':
            # National Public Data uses direct URL search with query parameters
            target_url = f"https://nationalpublicdata.com/search/?name={urllib.parse.quote(name)}&city={urllib.parse.quote(city)}&state={urllib.parse.quote(state.lower())}"

            job_id = threading.current_thread().name
            if job_id in processing_jobs:
                processing_jobs[job_id]['results'].append({
                    'type': 'status',
                    'message': f"Searching for name: {name}, location: {city}, {state}",
                    'url': target_url
                })

            logger.info(f"Requesting URL: {target_url}")

            response = self.make_request(target_url)
        else:
            # True People Search
            search_url = f"{TRUE_PEOPLE_SEARCH_BASE}/results"
            citystatezip = f"{address} {city} {state} {zip_code}".strip()
            query_params = {
                'name': name,
                'citystatezip': citystatezip
            }
            if page > 1:
                query_params['page'] = page
            target_url = f"{search_url}?{urllib.parse.urlencode(query_params)}"

            job_id = threading.current_thread().name
            if job_id in processing_jobs:
                processing_jobs[job_id]['results'].append({
                    'type': 'status',
                    'message': f"Searching for name: {name}, location: {city}, {state}",
                    'url': target_url
                })

            logger.info(f"Requesting URL: {target_url}")

            response = self.make_request(target_url)
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

            # Log the page content for debugging (truncated)
            logger.debug(f"Page content preview: {response.text[:500]}...")

            if self.platform == 'nationalpublicdata':
                # Check for no results on National Public Data
                no_results = soup.find('div', string=lambda text: text and 'No results found' in text.lower())
                if no_results:
                    if job_id in processing_jobs:
                        processing_jobs[job_id]['results'].append({
                            'type': 'status',
                            'message': "No results found for this name and location"
                        })
                    logger.info("No results found for this name and location")
                    return soup

                # Extract the actual number of people found
                people_count = 0

                # Try to get count from h1 span
                h1_span = soup.find('h1')
                if h1_span:
                    span_text = h1_span.find('span')
                    if span_text:
                        count_match = re.search(r'(\d+)\s+people?\s+found', span_text.text, re.IGNORECASE)
                        if count_match:
                            people_count = int(count_match.group(1))

                # If not found in h1, try description
                if people_count == 0:
                    desc_div = soup.find('div', class_='name-cards-description')
                    if desc_div:
                        desc_text = desc_div.get_text()
                        count_match = re.search(r'records?\s+of\s+(\d+)\s+people', desc_text, re.IGNORECASE)
                        if count_match:
                            people_count = int(count_match.group(1))

                # Update the status message with correct count
                if job_id in processing_jobs and people_count > 0:
                    processing_jobs[job_id]['results'].append({
                        'type': 'status',
                        'message': f"Found {people_count} people for this search, starting to scrape details..."
                    })
                logger.info(f"Found {people_count} people for this search")
            else:
                # True People Search
                record_count_elem = soup.find('div', class_='h2')
                if job_id in processing_jobs:
                    if record_count_elem and "No Results Found" in record_count_elem.text:
                        processing_jobs[job_id]['results'].append({
                            'type': 'status',
                            'message': "No results found for this name and address"
                        })
                        logger.info("No results found for this name and address")
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
                    'message': f"Failed to search for name: {name}, location: {city}, {state}"
                })
        return None

    def parse_search_results(self, soup: BeautifulSoup, job_id: str = None) -> List[Dict[str, Any]]:
        results = []

        if self.platform == 'nationalpublicdata':
            # Parse National Public Data results
            no_results = soup.find('div', string=lambda text: text and 'No results found' in text.lower())
            if no_results:
                logger.info("No results found on this page")
                return results

            # National Public Data uses div.name-cards-block
            name_cards_block = soup.find('div', class_='name-cards-block')
            if not name_cards_block:
                logger.info("No name-cards-block found")
                return results

            # Only parse cards within the persons-list-container
            persons_container = soup.find('div', id='persons-list-container')
            if persons_container:
                person_cards = persons_container.find_all('div', class_='name-cards-block')
            else:
                # Fallback to the old method if container not found
                person_cards = name_cards_block.find_all('div', class_=lambda c: c and 'card' in c and 'name-card' in c)

            # Filter out any cards that have TruthFinder ads (outside main container)
            filtered_person_cards = []
            for card in person_cards:
                # Skip cards that contain TruthFinder ad indicators
                if card.find('span', class_='add-info') or card.find('span', {'data-title': lambda x: x and 'TruthFinder' in x}):
                    continue
                filtered_person_cards.append(card)

            person_cards = filtered_person_cards

            for card in person_cards:
                try:
                    # Try different ways to find the name and URL
                    name_elem = None
                    detail_url = ""

                    # First try: h3 or a tag
                    name_elem = card.find('h3') or card.find('a')
                    if name_elem and name_elem.name == 'a':
                        detail_url = name_elem['href'] if name_elem.has_attr('href') else ""
                        name_text = name_elem.text.strip()
                    elif name_elem:
                        name_text = name_elem.text.strip()
                        # Find the associated a tag
                        detail_link = card.find('a')
                        detail_url = detail_link['href'] if detail_link and detail_link.has_attr('href') else ""

                    # Second try: span with data-a-link
                    if not name_elem or not detail_url:
                        span_elem = card.find('span', {'data-a-link': 'db-name'})
                        if span_elem:
                            name_text = span_elem.text.strip()
                            # Find the button/link for URL
                            button_elem = card.find('span', {'data-a-link': 'db-ctabutton'})
                            if button_elem:
                                # This is a sponsored card, skip it
                                continue
                            # For regular cards, the URL might be in onclick or data attributes
                            # For now, skip cards without proper links
                            continue

                    if not name_text or not detail_url:
                        continue

                    first_name, last_name = rearrange_name(name_text)

                    # National Public Data may have age/location in different structure
                    age_elem = card.find('span', class_=lambda c: c and 'age' in c.lower())
                    age = age_elem.text.strip() if age_elem else ""

                    location_elem = card.find('span', class_=lambda c: c and ('location' in c.lower() or 'city' in c.lower()))
                    location = location_elem.text.strip() if location_elem else ""

                    results.append({
                        'first_name': first_name,
                        'last_name': last_name,
                        'age': age,
                        'location': location,
                        'detail_url': detail_url
                    })
                except Exception as e:
                    logger.error(f"Error parsing National Public Data person card: {str(e)}")
                    continue
        else:
            # Parse True People Search results
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
                    logger.error(f"Error parsing True People Search person card: {str(e)}")
                    continue

        return results

    def get_person_details(self, detail_url: str, searched_address: str = "", owner_name: str = "") -> Dict[str, Any]:
        details = {
            'first_name': '',
            'last_name': '',
            'phones': [],
            'emails': [],
            'addresses': [],
            'current_address': '',
            'previous_addresses': [],  # New field for previous addresses
            'is_previous_resident': False,  # Flag to indicate if this is a previous resident
            'url': '',
            'error': None
        }

        try:
            if self.platform == 'nationalpublicdata':
                # detail_url is already a full URL from the search results
                full_url = detail_url
            else:
                full_url = f"{TRUE_PEOPLE_SEARCH_BASE}{detail_url}"
            details['url'] = full_url
            logger.info(f"Getting details for: {full_url}")

            # Use self.job_id for parallel processing, fallback to thread name
            job_id = self.job_id if self.job_id else threading.current_thread().name
            if job_id in processing_jobs:
                processing_jobs[job_id]['results'].append({
                    'type': 'status',
                    'message': f"Fetching details for: {full_url}",
                    'url': full_url
                })

            response = self.make_request(full_url)

            if not response or response.status_code != 200:
                error_msg = f"HTTP Error: {response.status_code}" if response else "Failed to fetch details"
                details['error'] = error_msg
                if job_id in processing_jobs:
                    processing_jobs[job_id]['results'].append({'type': 'status', 'message': error_msg})
                return details

            soup = BeautifulSoup(response.content, 'html.parser')

            if self.platform == 'nationalpublicdata':
                # Parse National Public Data profile page
                # Extract name from h1 or similar
                name_elem = soup.find('h1') or soup.find('h2')
                if name_elem:
                    name_text = name_elem.text.strip()
                    details['first_name'], details['last_name'] = rearrange_name(name_text)

                # Extract phones - look for phone patterns
                phone_text = soup.get_text()
                phone_matches = re.findall(r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}', phone_text)
                for phone in phone_matches:
                    clean_phone = re.sub(r'[^\d]', '', phone)
                    if len(clean_phone) == 10 and clean_phone not in [re.sub(r'[^\d]', '', p) for p in details['phones']]:
                        details['phones'].append(phone)

                # Extract emails
                email_matches = re.findall(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', phone_text)
                for email in email_matches:
                    if email not in details['emails']:
                        details['emails'].append(email)

                # Extract addresses - look for address patterns
                address_patterns = [
                    r'\d+\s+[A-Za-z0-9\s,.-]+,\s*[A-Za-z\s]+,\s*[A-Z]{2}\s*\d{5}',
                    r'\d+\s+[A-Za-z0-9\s,.-]+,\s*[A-Za-z\s]+,\s*[A-Z]{2}',
                ]
                for pattern in address_patterns:
                    address_matches = re.findall(pattern, phone_text)
                    for addr in address_matches:
                        if addr not in details['addresses']:
                            details['addresses'].append(addr.strip())

                # Set current address as first found address
                if details['addresses']:
                    details['current_address'] = details['addresses'][0]

                # National Public Data doesn't require address matching like True People Search
                # since searches are by name and location, not exact address
            else:
                # Parse True People Search profile page
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
                        details['current_address'] = full_address

                # Extract all addresses from the profile (including previous addresses)
                # Look for bio-address links which contain previous/other addresses
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
                        # Add to previous addresses if it's not the current address
                        if full_address != details['current_address']:
                            details['previous_addresses'].append(full_address)

                # Also look for addresses in the "Addresses" section
                address_section = soup.find('h5', string='Addresses')
                if address_section:
                    parent_div = address_section.find_parent('div', class_='row')
                    if parent_div:
                        # Find all address links in this section
                        address_links = parent_div.find_all('a', href=re.compile(r'/find/address/'))
                        for addr_link in address_links:
                            addr_text = addr_link.get_text(strip=True)
                            if addr_text and addr_text not in details['addresses']:
                                details['addresses'].append(addr_text)
                                if addr_text != details['current_address']:
                                    details['previous_addresses'].append(addr_text)

                # COMMENTED OUT: Address matching check - now we save all profiles regardless of address match
                # This allows scraping and saving data even when the address doesn't exactly match
                # if searched_address and not owner_name:
                #     # Normalize the searched address for comparison
                #     searched_normalized = re.sub(r'\s+', ' ', searched_address.lower().strip())

                #     # Check if any address (current or previous) matches the searched address
                #     address_match_found = False
                #     matched_address_type = ""

                #     # Check current address first
                #     if details['current_address']:
                #         current_normalized = re.sub(r'\s+', ' ', details['current_address'].lower().strip())
                #         if current_normalized == searched_normalized:
                #             address_match_found = True
                #             matched_address_type = "current resident"

                #     # If no match with current address, check previous addresses
                #     if not address_match_found and details['previous_addresses']:
                #         for prev_addr in details['previous_addresses']:
                #             prev_normalized = re.sub(r'\s+', ' ', prev_addr.lower().strip())
                #             if prev_normalized == searched_normalized:
                #                 address_match_found = True
                #                 matched_address_type = "previous resident"
                #                 details['is_previous_resident'] = True
                #                 break

                #     # Also check all addresses in the list
                #     if not address_match_found:
                #         for addr in details['addresses']:
                #             addr_normalized = re.sub(r'\s+', ' ', addr.lower().strip())
                #             if addr_normalized == searched_normalized:
                #                 address_match_found = True
                #                 if addr == details['current_address']:
                #                     matched_address_type = "current resident"
                #                 else:
                #                     matched_address_type = "previous resident"
                #                     details['is_previous_resident'] = True
                #                 break

                #     if address_match_found:
                #         logger.info(f"Address match found for {details.get('first_name', '')} {details.get('last_name', '')}: {matched_address_type}")
                #         if job_id in processing_jobs:
                #             processing_jobs[job_id]['results'].append({
                #                 'type': 'status',
                #                 'message': f"Found {matched_address_type}: {details.get('first_name', '')} {details.get('last_name', '')}"
                #             })
                #     else:
                #         # No address match found - skip this profile
                #         details['skip'] = True
                #         details['skip_reason'] = f"Address not found in profile. Searched: '{searched_address}'"
                #         logger.info(f"Skipping profile - address not found: {details['skip_reason']}")
                #         if job_id in processing_jobs:
                #             processing_jobs[job_id]['results'].append({
                #                 'type': 'status',
                #                 'message': f"Skipped: {details.get('first_name', '')} {details.get('last_name', '')} - {details['skip_reason']}"
                #             })

                # If no current address was found but we have other addresses, use the first one
                if not details['current_address'] and details['addresses']:
                    details['current_address'] = details['addresses'][0]

            # Update stats & push details event only if not skipped
            if not details.get('skip') and job_id in processing_jobs:
                with stats_lock:
                    processing_jobs[job_id]['stats']['phones_found'] += len(details['phones'])
                    processing_jobs[job_id]['stats']['emails_found'] += len(details['emails'])
                    processing_jobs[job_id]['stats']['addresses_found'] += len(details['addresses'])

                # Send success status message
                processing_jobs[job_id]['results'].append({
                    'type': 'status',
                    'message': f"Skiptrace successful: Found {len(details['phones'])} phone(s), {len(details['emails'])} email(s) for {details['first_name']} {details['last_name']}"
                })

                processing_jobs[job_id]['results'].append({
                    'type': 'details',
                    'data': {
                        'first_name': details['first_name'],
                        'last_name': details['last_name'],
                        'phones': details['phones'],
                        'emails': details['emails'],
                        'addresses': details['addresses'],
                        'current_address': details['current_address'],
                        'full_address': details['current_address'] or searched_address,
                        'url': details['url']
                    }
                })

        except Exception as e:
            logger.exception(f"Error fetching person details: {str(e)}")
            details['error'] = str(e)

        return details

    def process_address(self, row: Dict[str, Any], row_index: int, job_id: str, address: str, city: str, state: str, zip_code: str, county: str = "", owner_name: str = "", existing_urls: set = None) -> List[Dict[str, Any]]:
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
                with stats_lock:
                    processing_jobs[job_id]['stats']['addresses_processed'] += 1

            # Determine search strategy:
            # 1. If owner_name exists, first try name + address search
            # 2. If no results with name + address, fallback to address-only search
            # 3. If results found with name + address, skip address-only search

            all_person_summaries = []
            tried_name_search = False
            name_search_had_results = False

            # Step 1: Try name + address search if owner_name exists
            if owner_name:
                tried_name_search = True
                search_names = []

                if self.platform == 'nationalpublicdata':
                    # For National Public Data, try rearranged name first, then original if 2 parts
                    rearranged_first, rearranged_last = rearrange_name(owner_name)
                    rearranged_name = f"{rearranged_first} {rearranged_last}".strip()
                    search_names.append(rearranged_name)
                    if len(owner_name.split()) == 2:
                        search_names.append(owner_name)  # Try original if 2 parts
                else:
                    search_names.append(owner_name)

                if job_id in processing_jobs:
                    processing_jobs[job_id]['results'].append({
                        'type': 'status',
                        'message': f"Searching with owner name: {owner_name}"
                    })

                for search_name in search_names:
                    if job_id in processing_jobs and processing_jobs[job_id]['cancelled']:
                        break

                    page = 1
                    while True:
                        if job_id in processing_jobs and processing_jobs[job_id]['cancelled']:
                            break

                        soup = self.search_name_address(search_name, exp_address, city, state, zip_code, page)
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

                        # Apply pagination limits: min MIN_PAGES, max MAX_PAGES
                        if page >= MAX_PAGES:
                            if job_id in processing_jobs:
                                processing_jobs[job_id]['results'].append({
                                    'type': 'status',
                                    'message': f"Reached maximum page limit ({MAX_PAGES} pages), moving to next search..."
                                })
                            logger.info(f"Reached maximum page limit ({MAX_PAGES} pages)")
                            break

                        page += 1
                        time.sleep(0.2)  # delay between pages (reduced for faster scraping)

                    # If we found people with this search name, stop trying other names
                    if all_person_summaries:
                        name_search_had_results = True
                        break

                if name_search_had_results:
                    if job_id in processing_jobs:
                        processing_jobs[job_id]['results'].append({
                            'type': 'status',
                            'message': f"Found {len(all_person_summaries)} result(s) with owner name, skipping address-only search"
                        })
                    logger.info(f"Found {len(all_person_summaries)} result(s) with owner name, skipping address-only search")

            # Step 2: Fallback to address-only search if:
            # - No owner_name was provided, OR
            # - Owner name search was tried but found no results
            if not tried_name_search or (tried_name_search and not name_search_had_results):
                if tried_name_search and not name_search_had_results:
                    if job_id in processing_jobs:
                        processing_jobs[job_id]['results'].append({
                            'type': 'status',
                            'message': f"No results found with owner name '{owner_name}', falling back to address-only search"
                        })
                    logger.info(f"No results found with owner name '{owner_name}', falling back to address-only search")

                page = 1
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

                    # Apply pagination limits: min MIN_PAGES, max MAX_PAGES
                    if page >= MAX_PAGES:
                        if job_id in processing_jobs:
                            processing_jobs[job_id]['results'].append({
                                'type': 'status',
                                'message': f"Reached maximum page limit ({MAX_PAGES} pages), moving to next search..."
                            })
                        logger.info(f"Reached maximum page limit ({MAX_PAGES} pages)")
                        break

                    page += 1
                    time.sleep(0.2)  # delay between pages (reduced for faster scraping)

            # Update people_found stat for this address
            if job_id in processing_jobs:
                with stats_lock:
                    processing_jobs[job_id]['stats']['people_found'] += len(all_person_summaries)
                processing_jobs[job_id]['results'].append({
                    'type': 'status',
                    'message': f"Found {len(all_person_summaries)} people for this address, starting to scrape details..."
                })

            # Limit people to scrape per address
            people_scraped_count = 0

            for person in all_person_summaries:
                # Check if we've reached the maximum people per address limit
                if people_scraped_count >= MAX_PEOPLE_PER_ADDRESS:
                    if job_id in processing_jobs:
                        processing_jobs[job_id]['results'].append({
                            'type': 'status',
                            'message': f"Reached maximum people limit ({MAX_PEOPLE_PER_ADDRESS}) for this address, moving to next address..."
                        })
                    logger.info(f"Reached maximum people limit ({MAX_PEOPLE_PER_ADDRESS}) for this address")
                    break
                if job_id in processing_jobs and processing_jobs[job_id]['cancelled']:
                    break

                # Check if this person's URL has already been scraped (using fast scraped_urls table)
                if self.platform == 'nationalpublicdata':
                    person_url = person['detail_url']  # Already a full URL
                else:
                    person_url = f"{TRUE_PEOPLE_SEARCH_BASE}{person['detail_url']}"

                person_name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
                logger.info(f"Checking URL for {person_name}: {person_url}")

                # Fast check using scraped_urls table (with hash index)
                if check_url_exists(person_url):
                    # Person already scraped, skip
                    logger.info(f"Skipping already scraped person: {person_name} - URL: {person_url}")
                    if job_id in processing_jobs:
                        processing_jobs[job_id]['results'].append({
                            'type': 'status',
                            'message': f"Skipping already scraped: {person_name}"
                        })
                    continue
                else:
                    logger.info(f"URL not found in database, will scrape: {person_url}")

                if job_id in processing_jobs:
                    processing_jobs[job_id]['results'].append({
                        'type': 'status',
                        'message': f"Getting details for: {person_name}"
                    })

                searched_address = f"{exp_address}, {city}, {state} {zip_code}"
                details = self.get_person_details(person['detail_url'], searched_address, owner_name)

                if details.get('skip'):
                    # Skip this person due to address mismatch
                    logger.info(f"Skipped {person_name}: {details.get('skip_reason', 'address mismatch')}")
                    continue

                logger.info(f"Successfully scraped {person_name}: {len(details.get('phones', []))} phones, {len(details.get('emails', []))} emails")

                person.update(details)
                all_results.append(person)

                # Increment the counter for successfully scraped people
                people_scraped_count += 1

                # Save result immediately after scraping
                result_row = row.copy()
                result_row["Owner's First Name"] = person.get('first_name', '')
                result_row["Owner's Last Name"] = person.get('last_name', '')
                result_row['phones'] = person.get('phones', [])
                result_row['Mailing Address'] = '; '.join(person.get('addresses', []))
                result_row['Current Address'] = person.get('current_address', '')
                result_row['Email Address'] = '; '.join(person.get('emails', []))
                result_row['URL'] = person.get('url', '')
                result_row['full address'] = address
                save_result_to_supabase(job_id, row_index, result_row)

                # Save URL to scraped_urls table for fast future deduplication
                save_scraped_url(
                    url=person.get('url', ''),
                    platform=self.platform,
                    first_name=person.get('first_name', ''),
                    last_name=person.get('last_name', ''),
                    job_id=job_id
                )

                time.sleep(0.2)  # delay between detail fetches (reduced for faster scraping)

            time.sleep(0.2)  # delay between address searches (reduced for faster scraping)

        # If no people found, save empty result
        if not all_results:
            result_row = row.copy()
            result_row["Owner's First Name"] = ''
            result_row["Owner's Last Name"] = ''
            result_row['phones'] = []
            result_row['Mailing Address'] = ''
            result_row['Current Address'] = ''
            result_row['Email Address'] = ''
            result_row['URL'] = ''
            result_row['full address'] = address
            save_result_to_supabase(job_id, row_index, result_row)

        return all_results


# Initialize the skip tracer (API key may be None in dev)
skip_tracer = SkipTracer(ZENROWS_API_KEY)


def test_zenrows_connection():
    logger.info(f"Using ZenRows API Key: {ZENROWS_API_KEY}")
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


def compute_url_hash(url: str) -> str:
    """Compute SHA256 hash of URL for fast lookups."""
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def check_url_exists(url: str) -> bool:
    """Check if a URL has already been scraped using the scraped_urls table."""
    if not supabase:
        return False
    try:
        url_hash = compute_url_hash(url)
        response = supabase.table('scraped_urls').select('id').eq('url_hash', url_hash).limit(1).execute()
        return len(response.data) > 0
    except Exception as e:
        logger.error(f"Error checking URL existence: {e}")
        return False


def check_urls_batch(urls: List[str]) -> set:
    """Check multiple URLs at once and return the set of already scraped URLs."""
    if not supabase or not urls:
        return set()
    try:
        url_hashes = [compute_url_hash(url) for url in urls]
        response = supabase.table('scraped_urls').select('url').in_('url_hash', url_hashes).execute()
        return set(item['url'] for item in response.data)
    except Exception as e:
        logger.error(f"Error checking URLs batch: {e}")
        return set()


def save_scraped_url(url: str, platform: str, first_name: str = '', last_name: str = '', job_id: str = ''):
    """Save a scraped URL to the scraped_urls table for future deduplication."""
    if not supabase or not url:
        return
    try:
        url_hash = compute_url_hash(url)
        supabase.table('scraped_urls').insert({
            'url': url,
            'url_hash': url_hash,
            'platform': platform,
            'first_name': first_name,
            'last_name': last_name,
            'job_id': job_id,
            'created_at': datetime.now(timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        # Ignore duplicate key errors (URL already exists)
        if 'duplicate' not in str(e).lower() and 'unique' not in str(e).lower():
            logger.error(f"Error saving scraped URL: {e}")


def get_scraped_urls_count() -> Dict[str, int]:
    """Get count of scraped URLs by platform."""
    if not supabase:
        return {}
    try:
        response = supabase.table('scraped_urls').select('platform').execute()
        counts = {}
        for item in response.data:
            platform = item.get('platform', 'unknown')
            counts[platform] = counts.get(platform, 0) + 1
        return counts
    except Exception as e:
        logger.error(f"Error getting scraped URLs count: {e}")
        return {}


# run a quick test at startup (safe to ignore failure)
test_zenrows_connection()


def get_zenrows_credits() -> Dict[str, Any]:
    """
    Check ZenRows API credits remaining.
    Returns dict with 'credits' and 'limit' or error message.

    ZenRows provides credit info via:
    1. Account API endpoint: https://api.zenrows.com/v1/account
    2. Response headers on API requests (X-ZR-Credits-Remaining-Success, etc.)
    3. Status codes (402 = credits exhausted)

    We try multiple methods to get credit information.
    """
    if not ZENROWS_API_KEY:
        return {'error': 'No ZenRows API key configured', 'credits': 0}

    try:
        # Method 1: Try ZenRows account API endpoint
        # This is the official way to check account balance
        account_response = requests.get(
            'https://api.zenrows.com/v1/account',
            params={'apikey': ZENROWS_API_KEY},
            timeout=15
        )

        if account_response.status_code == 200:
            try:
                account_data = account_response.json()
                logger.debug(f"ZenRows account response: {account_data}")

                if isinstance(account_data, dict):
                    # Try various possible field names for credits
                    credits = None
                    limit = None

                    # Check for credits in various possible fields
                    for field in ['credits', 'remaining_credits', 'remaining', 'balance', 'api_credits']:
                        if field in account_data and account_data[field] is not None:
                            credits = account_data[field]
                            break

                    # Check for limit in various possible fields
                    for field in ['limit', 'total_credits', 'monthly_limit', 'plan_limit', 'max_credits']:
                        if field in account_data and account_data[field] is not None:
                            limit = account_data[field]
                            break

                    if credits is not None:
                        remaining = int(credits) if isinstance(credits, (int, float, str)) else 0
                        total = int(limit) if limit and isinstance(limit, (int, float, str)) else remaining
                        used = total - remaining if total >= remaining else 0

                        return {
                            'credits': remaining,
                            'limit': total,
                            'used': used,
                            'percentage_remaining': round((remaining / total) * 100, 1) if total > 0 else 100
                        }
            except (ValueError, TypeError) as e:
                logger.debug(f"Failed to parse ZenRows account response: {e}")

        elif account_response.status_code == 401:
            return {'error': 'Invalid API key', 'credits': 0}
        elif account_response.status_code == 402:
            return {'error': 'Credits exhausted - no remaining credits', 'credits': 0}

        # Method 2: Make a minimal API request and check headers
        # ZenRows includes credit info in response headers
        params = {
            'url': 'https://example.com',
            'apikey': ZENROWS_API_KEY,
        }

        response = requests.get(
            ZENROWS_BASE_URL,
            params=params,
            timeout=15,
            allow_redirects=True
        )

        # Check for credit headers in response (ZenRows uses these header names)
        credits_remaining = response.headers.get('X-ZR-Credits-Remaining-Success') or \
                           response.headers.get('X-ZR-Credits-Remaining') or \
                           response.headers.get('X-Credits-Remaining')
        credits_used = response.headers.get('X-ZR-Credits-Used') or \
                      response.headers.get('X-Credits-Used')

        if credits_remaining is not None:
            remaining = int(credits_remaining)
            used = int(credits_used) if credits_used else 0
            total = remaining + used

            return {
                'credits': remaining,
                'limit': total,
                'used': used,
                'percentage_remaining': round((remaining / total) * 100, 1) if total > 0 else 0
            }

        # Check status code for credit status
        if response.status_code == 200:
            # API key works but no credit headers available
            # Try to get info from response body
            try:
                resp_data = response.json()
                if isinstance(resp_data, dict):
                    # Some responses include credit info
                    credits = resp_data.get('credits') or resp_data.get('remaining')
                    if credits is not None:
                        return {
                            'credits': int(credits) if isinstance(credits, (int, float)) else 0,
                            'limit': 'Unknown',
                            'used': 'Unknown',
                            'percentage_remaining': 100
                        }
            except:
                pass

            # API key is valid but we can't determine exact credits
            return {
                'credits': 'Unknown',
                'limit': 'Unknown',
                'used': 'Unknown',
                'percentage_remaining': 100,
                'message': 'API key valid - check ZenRows dashboard for credit info',
                'dashboard_url': 'https://www.zenrows.com/dashboard'
            }
        elif response.status_code == 401:
            return {'error': 'Invalid API key', 'credits': 0}
        elif response.status_code == 402:
            return {'error': 'Credits exhausted - no remaining credits', 'credits': 0}
        elif response.status_code == 403:
            return {'error': 'Access forbidden - check API key permissions', 'credits': 0}
        elif response.status_code == 429:
            return {'error': 'Rate limited - too many requests', 'credits': 0}
        else:
            return {'error': f'HTTP {response.status_code}', 'credits': 0}

    except requests.exceptions.Timeout:
        return {'error': 'Request timed out', 'credits': 0}
    except requests.exceptions.ConnectionError:
        return {'error': 'Connection error - check network', 'credits': 0}
    except Exception as e:
        logger.exception(f"Error checking ZenRows credits: {e}")
        return {'error': str(e), 'credits': 0}


def check_credits_and_alert() -> Tuple[bool, str]:
    """
    Check credits and return (has_credits, message).
    Logs warning if credits are low.
    """
    credit_info = get_zenrows_credits()

    if 'error' in credit_info:
        logger.error(f"ZenRows credit check failed: {credit_info['error']}")
        return False, credit_info['error']

    credits = credit_info.get('credits', 0)
    limit = credit_info.get('limit', 0)
    percentage = credit_info.get('percentage_remaining', 0)

    # Handle case where credits might be a string (e.g., 'Unknown')
    if isinstance(credits, str):
        # If credits is 'Unknown', API key is valid but we can't determine exact credits
        logger.info(f"ZenRows credits: {credits} (API key valid)")
        return True, f"Credits: {credits} - API key valid, check dashboard for details"

    # Convert to int for comparison
    try:
        credits_int = int(credits) if credits is not None else 0
    except (ValueError, TypeError):
        credits_int = 0

    if credits_int <= 0:
        logger.error(f"ZenRows credits EXHAUSTED! Used {credit_info.get('used', 0)}/{limit}")
        return False, f"Credits exhausted! Used {credit_info.get('used', 0)}/{limit}"
    elif percentage < 10:
        logger.warning(f"ZenRows credits LOW: {credits_int}/{limit} remaining ({percentage}%)")
        return True, f"WARNING: Only {credits_int} credits remaining ({percentage}%)"
    elif percentage < 25:
        logger.warning(f"ZenRows credits running low: {credits_int}/{limit} remaining ({percentage}%)")
        return True, f"Low credits: {credits_int}/{limit} ({percentage}%)"
    else:
        logger.info(f"ZenRows credits: {credits_int}/{limit} remaining ({percentage}%)")
        return True, f"Credits OK: {credits_int}/{limit} ({percentage}%)"


def clean_phone_number(phone: str) -> str:
    """Remove all non-digit characters from phone number."""
    return re.sub(r'\D', '', phone)


def process_single_row(row_data: Tuple[int, Dict[str, Any], str, str]) -> Optional[Dict[str, Any]]:
    """
    Process a single row for parallel execution.
    Returns result data or None if skipped.

    Args:
        row_data: Tuple of (row_index, row, job_id, platform)
    """
    row_index, row, job_id, platform = row_data

    # Check if job is cancelled or credits exhausted
    if job_id not in processing_jobs:
        return None

    job = processing_jobs[job_id]

    if job['cancelled']:
        return None

    # Check if ZenRows credits have been exhausted
    global zenrows_credits_exhausted
    with zenrows_credits_lock:
        if zenrows_credits_exhausted:
            return None

    # Check if this row has already been processed
    if supabase:
        existing_results = supabase.table('results').select('id').eq('job_id', job_id).eq('row_index', row_index).limit(1).execute()
        if existing_results.data:
            # Row already processed, skip
            return None

    # Extract address data
    address = row.get('Address', '') or row.get('address', '')
    city = row.get('City', '') or row.get('city', '')
    state = row.get('State', '') or row.get('state', '')
    zip_code = row.get('Zip', '') or row.get('zip', '')
    county = row.get('County', '') or row.get('county', '')
    owner_name = row.get('Owner Name', '')
    row_platform = row.get('Platform', '').lower().strip() or platform

    # If Owner Name is empty, we still proceed with address-only skip tracing
    # The process_address method will handle the fallback to address-only search
    if not owner_name.strip():
        logger.info(f"Owner Name is empty for row {row_index + 1}, proceeding with address-only skip tracing")

    if address and city and state:
        logger.info(f"Processing {row_index + 1}: {address}, {city}, {state} {zip_code}")

        # Thread-safe progress update
        with job_results_lock:
            if job_id in processing_jobs:
                progress_data = {
                    'type': 'progress',
                    'current': row_index + 1,
                    'total': job['total_rows'],
                    'address': address,
                    'stats': job['stats']
                }
                job['results'].append(progress_data)

        # Create platform-specific tracer for this row with job_id for parallel processing
        row_tracer = SkipTracer(ZENROWS_API_KEY, row_platform, job_id)
        people_data = row_tracer.process_address(row, row_index, job_id, address, city, state, zip_code, county, owner_name, set())

        return {'row_index': row_index, 'processed': True}
    else:
        # Empty result for missing fields
        result_row = row.copy()
        result_row["Owner's First Name"] = ''
        result_row["Owner's Last Name"] = ''
        result_row['phones'] = []
        result_row['Mailing Address'] = ''
        result_row['Current Address'] = ''
        result_row['Email Address'] = ''
        result_row['URL'] = ''
        result_row['full address'] = ''
        return {'row_index': row_index, 'result_data': result_row, 'empty': True}


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/credits')
def api_credits():
    """API endpoint to check ZenRows credits remaining."""
    credit_info = get_zenrows_credits()
    return jsonify(credit_info)


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if not file.filename.lower().endswith('.csv'):
        return jsonify({'error': 'File must be a CSV'}), 400

    # Get platform from form data
    platform = request.form.get('platform', 'truepeoplesearch').lower().strip()
    if platform not in ['truepeoplesearch', 'nationalpublicdata']:
        return jsonify({'error': 'Invalid platform selected'}), 400

    # Check ZenRows credits before starting (for truepeoplesearch platform)
    if platform == 'truepeoplesearch':
        # Reset the credit exhaustion flag when starting a new job
        # This allows new jobs to run after credits are replenished
        global zenrows_credits_exhausted
        with zenrows_credits_lock:
            zenrows_credits_exhausted = False

        has_credits, credit_message = check_credits_and_alert()
        if not has_credits:
            return jsonify({'error': f'ZenRows credits exhausted: {credit_message}'}), 402

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

    def process_file(job_id_local: str, file_bytes_local: bytes, platform_local: str = 'truepeoplesearch'):
        """Process CSV file with parallel execution for faster scraping."""
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

            # Determine start index for resumption
            start_index = job.get('current_row', 0)
            if job.get('resumed'):
                job['results'].append({
                    'type': 'status',
                    'message': f'Resuming from row {start_index + 1}'
                })

            # Log parallel processing configuration
            logger.info(f"Starting parallel processing with {MAX_CONCURRENT_WORKERS} workers for {len(rows) - start_index} rows")

            # Add status message about parallel processing
            job['results'].append({
                'type': 'status',
                'message': f'Processing with {MAX_CONCURRENT_WORKERS} parallel workers'
            })

            # Prepare row data for parallel processing
            rows_to_process = []
            for i in range(start_index, len(rows)):
                rows_to_process.append((i, rows[i], job_id_local, platform_local))

            # Process rows in parallel using ThreadPoolExecutor
            processed_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as executor:
                # Submit all tasks
                future_to_row = {executor.submit(process_single_row, row_data): row_data for row_data in rows_to_process}

                # Process completed tasks as they finish
                for future in concurrent.futures.as_completed(future_to_row):
                    # Check for cancellation or credit exhaustion
                    if job['cancelled']:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

                    # Check if ZenRows credits have been exhausted during processing
                    global zenrows_credits_exhausted
                    with zenrows_credits_lock:
                        if zenrows_credits_exhausted:
                            job['results'].append({
                                'type': 'error',
                                'message': '⚠️ ZENROWS CREDITS EXHAUSTED! Processing stopped. Please add more credits at https://www.zenrows.com/dashboard'
                            })
                            job['credits_exhausted'] = True
                            logger.critical(f"Job {job_id_local} stopped due to ZenRows credit exhaustion")
                            executor.shutdown(wait=False, cancel_futures=True)
                            break

                    row_data = future_to_row[future]
                    row_index = row_data[0]

                    try:
                        result = future.result()
                        if result:
                            # Handle empty results that need to be saved
                            if result.get('empty'):
                                save_result_to_supabase(job_id_local, result['row_index'], result['result_data'])

                            processed_count += 1

                            # Update progress
                            with job_results_lock:
                                if job_id_local in processing_jobs:
                                    job['current_row'] = result['row_index'] + 1

                                    # Periodic progress update (every 5 rows)
                                    if processed_count % 5 == 0:
                                        update_job_progress(job_id_local, job['current_row'], job['stats'])
                                        logger.info(f"Processed {processed_count} rows, current row: {job['current_row']}")

                    except Exception as e:
                        logger.error(f"Error processing row {row_index}: {str(e)}")
                        # Save empty result for failed row
                        result_row = rows[row_index].copy()
                        result_row["Owner's First Name"] = ''
                        result_row["Owner's Last Name"] = ''
                        result_row['phones'] = []
                        result_row['Mailing Address'] = ''
                        result_row['Current Address'] = ''
                        result_row['Email Address'] = ''
                        result_row['URL'] = ''
                        result_row['full address'] = ''
                        save_result_to_supabase(job_id_local, row_index, result_row)

            # Final progress update
            update_job_progress(job_id_local, job.get('current_row', 0), job['stats'])

            # final event
            if not job['cancelled']:
                job['results'].append({
                    'type': 'complete',
                    'message': f'Processing completed successfully. Processed {processed_count} rows with {MAX_CONCURRENT_WORKERS} parallel workers.',
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

            # Set completed_at timestamp for cleanup
            job['completed_at'] = datetime.now(timezone.utc).isoformat()

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
    thread = threading.Thread(target=process_file, args=(job_id, file_bytes, platform), name=job_id)
    thread.daemon = True
    thread.start()

    return jsonify({'message': 'Processing started', 'job_id': job_id, 'workers': MAX_CONCURRENT_WORKERS}), 202


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

            # Add required fields in desired order: Property first, then Owner, then Phones
            # Construct address as "Address, City, State"
            address = data.get('Address', '') or data.get('address', '')
            city = data.get('City', '') or data.get('city', '')
            state = data.get('State', '') or data.get('state', '')
            filtered_data['Property'] = f"{address}, {city}, {state}".strip(', ')
            # Combine first and last name into single Owner column
            first_name = data.get("Owner's First Name", '').strip()
            last_name = data.get("Owner's Last Name", '').strip()
            filtered_data['Owner'] = f"{first_name} {last_name}".strip()

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
        # Fetch all jobs from Supabase (including in_progress)
        response = supabase.table('jobs').select('job_id, created_at, status, total_rows, stats, current_row').order('created_at', desc=True).execute()
        files = []
        for job in response.data:
            # Get result count from Supabase
            result_count = 0
            try:
                count_response = supabase.table('results').select('id', count='exact').eq('job_id', job['job_id']).execute()
                result_count = count_response.count if hasattr(count_response, 'count') else 0
            except:
                pass

            files.append({
                'name': f"{job['job_id']}_results.csv",
                'job_id': job['job_id'],
                'creation_time': job['created_at'],  # This is ISO string, template filter will handle it
                'status': job['status'],
                'total_rows': job.get('total_rows', 0),
                'result_count': result_count,
                'stats': job.get('stats', {})
            })
        return render_template('download_jobs.html', files=files)
    except Exception as e:
        logger.exception(f"Error fetching jobs list: {e}")
        return "Error loading jobs", 500


@app.route('/preview/<job_id>')
def preview_job(job_id):
    if not supabase:
        return jsonify({'error': 'Database not available'}), 500

    try:
        # Fetch ALL results from Supabase (no limit)
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
            'rows': rows,
            'total_rows': len(rows)
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
# Ayodeji#@23
