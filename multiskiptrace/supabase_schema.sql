-- =====================================================
-- MULTISKIPTRACE SUPABASE DATABASE SCHEMA
-- =====================================================
-- Run this SQL in your Supabase SQL Editor to create
-- the required tables for the multiskiptrace application
-- =====================================================

-- Enable UUID extension (if not already enabled)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =====================================================
-- 1. JOBS TABLE
-- =====================================================
-- Stores information about each skip tracing job
CREATE TABLE IF NOT EXISTS jobs (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    job_id VARCHAR(255) UNIQUE NOT NULL,
    csv_hash VARCHAR(64) NOT NULL,
    status VARCHAR(50) DEFAULT 'in_progress',
    total_rows INTEGER DEFAULT 0,
    current_row INTEGER DEFAULT 0,
    stats JSONB DEFAULT '{
        "addresses_processed": 0,
        "people_found": 0,
        "phones_found": 0,
        "emails_found": 0,
        "addresses_found": 0
    }'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- Create index on job_id for faster lookups
CREATE INDEX IF NOT EXISTS idx_jobs_job_id ON jobs(job_id);

-- Create index on csv_hash for job resumption
CREATE INDEX IF NOT EXISTS idx_jobs_csv_hash ON jobs(csv_hash);

-- Create index on status for filtering
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- Create index on created_at for ordering
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);

-- =====================================================
-- 2. RESULTS TABLE
-- =====================================================
-- Stores individual skip trace results for each row
CREATE TABLE IF NOT EXISTS results (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    job_id VARCHAR(255) NOT NULL,
    row_index INTEGER NOT NULL,
    data JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create index on job_id for faster lookups
CREATE INDEX IF NOT EXISTS idx_results_job_id ON results(job_id);

-- Create composite index on job_id and row_index for ordering
CREATE INDEX IF NOT EXISTS idx_results_job_id_row_index ON results(job_id, row_index);

-- Create index on created_at for ordering
CREATE INDEX IF NOT EXISTS idx_results_created_at ON results(created_at);

-- =====================================================
-- 3. ROW LEVEL SECURITY (RLS) POLICIES
-- =====================================================
-- Enable RLS on both tables
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE results ENABLE ROW LEVEL SECURITY;

-- Create policies to allow all operations (adjust as needed for your security requirements)
CREATE POLICY "Allow all operations on jobs" ON jobs
    FOR ALL USING (true) WITH CHECK (true);

CREATE POLICY "Allow all operations on results" ON results
    FOR ALL USING (true) WITH CHECK (true);

-- =====================================================
-- 4. OPTIONAL: TRIGGER FOR UPDATED_AT
-- =====================================================
-- Function to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger to call the function on jobs table
DROP TRIGGER IF EXISTS update_jobs_updated_at ON jobs;
CREATE TRIGGER update_jobs_updated_at
    BEFORE UPDATE ON jobs
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- =====================================================
-- 5. SAMPLE DATA STRUCTURE FOR REFERENCE
-- =====================================================
-- The 'data' JSONB column in 'results' table will contain:
-- {
--   "Address": "123 Main St",
--   "City": "Columbus",
--   "State": "OH",
--   "Zip": "43215",
--   "County": "Franklin",
--   "Owner Name": "John Doe",
--   "Owner's First Name": "John",
--   "Owner's Last Name": "Doe",
--   "phones": ["(614) 555-1234", "(614) 555-5678"],
--   "Mailing Address": "123 Main St, Columbus, OH 43215",
--   "Current Address": "123 Main St, Columbus, OH 43215",
--   "Email Address": "john.doe@email.com",
--   "URL": "https://www.truepeoplesearch.com/details?person=...",
--   "full address": "123 Main St"
-- }

-- =====================================================
-- 6. VERIFICATION QUERIES
-- =====================================================
-- Run these to verify your tables were created correctly:
-- SELECT * FROM jobs LIMIT 5;
-- SELECT * FROM results LIMIT 5;

-- =====================================================
-- 7. USEFUL MAINTENANCE QUERIES
-- =====================================================
-- Delete all jobs and results (CAUTION: This will clear all data)
-- DELETE FROM results;
-- DELETE FROM jobs;

-- Get job statistics
-- SELECT
--     job_id,
--     status,
--     total_rows,
--     current_row,
--     stats->>'phones_found' as phones_found,
--     stats->>'emails_found' as emails_found,
--     created_at
-- FROM jobs
-- ORDER BY created_at DESC;

-- Count results per job
-- SELECT job_id, COUNT(*) as result_count
-- FROM results
-- GROUP BY job_id;

-- =====================================================
-- 8. SCRAPED URLS TABLE (for fast deduplication)
-- =====================================================
-- Stores scraped URLs for fast deduplication across all jobs
-- This prevents re-scraping the same person/profile URL
CREATE TABLE IF NOT EXISTS scraped_urls (
    id UUID DEFAULT uuid_generate_v4() PRIMARY KEY,
    url VARCHAR(500) UNIQUE NOT NULL,
    url_hash VARCHAR(64) UNIQUE NOT NULL,
    platform VARCHAR(50) NOT NULL,
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    job_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Create index on url for faster lookups
CREATE INDEX IF NOT EXISTS idx_scraped_urls_url ON scraped_urls(url);

-- Create index on url_hash for ultra-fast hash lookups
CREATE INDEX IF NOT EXISTS idx_scraped_urls_url_hash ON scraped_urls(url_hash);

-- Create index on platform for filtering by platform
CREATE INDEX IF NOT EXISTS idx_scraped_urls_platform ON scraped_urls(platform);

-- Create index on created_at for ordering
CREATE INDEX IF NOT EXISTS idx_scraped_urls_created_at ON scraped_urls(created_at DESC);

-- Enable RLS on scraped_urls table
ALTER TABLE scraped_urls ENABLE ROW LEVEL SECURITY;

-- Create policy to allow all operations on scraped_urls
CREATE POLICY "Allow all operations on scraped_urls" ON scraped_urls
    FOR ALL USING (true) WITH CHECK (true);

-- =====================================================
-- 9. FUNCTION TO CHECK IF URL EXISTS (fast lookup)
-- =====================================================
CREATE OR REPLACE FUNCTION url_exists(p_url_hash VARCHAR(64))
RETURNS BOOLEAN AS $$
BEGIN
    RETURN EXISTS (SELECT 1 FROM scraped_urls WHERE url_hash = p_url_hash);
END;
$$ LANGUAGE plpgsql;

-- =====================================================
-- 10. FUNCTION TO GET URL STATS
-- =====================================================
CREATE OR REPLACE FUNCTION get_url_stats()
RETURNS TABLE(platform VARCHAR(50), total_urls BIGINT) AS $$
BEGIN
    RETURN QUERY
    SELECT su.platform, COUNT(*)::BIGINT as total_urls
    FROM scraped_urls su
    GROUP BY su.platform;
END;
$$ LANGUAGE plpgsql;
