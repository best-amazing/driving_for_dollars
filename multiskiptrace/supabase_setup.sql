-- Supabase setup for skiptrace app
-- Run this in your Supabase SQL editor

-- Create jobs table
CREATE TABLE jobs (
    job_id TEXT PRIMARY KEY,
    csv_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    total_rows INTEGER NOT NULL,
    current_row INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    stats JSONB DEFAULT '{
        "addresses_processed": 0,
        "people_found": 0,
        "phones_found": 0,
        "emails_found": 0,
        "addresses_found": 0
    }'::jsonb
);

-- Create results table
CREATE TABLE results (
    id SERIAL PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    row_index INTEGER NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for better performance
CREATE INDEX idx_jobs_csv_hash ON jobs(csv_hash);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_results_job_id ON results(job_id);
CREATE INDEX idx_results_row_index ON results(job_id, row_index);

-- Enable Row Level Security (optional, but recommended)
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE results ENABLE ROW LEVEL SECURITY;

-- Create policies to allow all operations (adjust as needed for security)
CREATE POLICY "Allow all operations on jobs" ON jobs FOR ALL USING (true);
CREATE POLICY "Allow all operations on results" ON results FOR ALL USING (true);
