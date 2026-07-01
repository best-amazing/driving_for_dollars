# Skip Trace Platform

A web-based skip tracing application that searches for people using TruePeopleSearch.com data. The application uses ZenRows for web scraping and Supabase for data storage.

## Features

- Upload CSV files with addresses to search
- Automatically expand address ranges (e.g., "123-125 Main St" becomes individual addresses)
- Scrape contact information (phones, emails, addresses) from TruePeopleSearch.com
- Validate current addresses to ensure accuracy
- Real-time progress tracking
- Download results as CSV
- Resume interrupted jobs
- Global deduplication to avoid duplicate processing

## Prerequisites

Before you begin, ensure you have the following:

### 1. Python Installation

**Windows:**
1. Go to https://www.python.org/downloads/
2. Download the latest Python 3.x version (3.8 or higher recommended)
3. Run the installer
4. **Important:** Check the box "Add Python to PATH" during installation
5. Verify installation by opening Command Prompt and typing: `python --version`

**macOS:**
1. Python usually comes pre-installed. Check by opening Terminal and typing: `python3 --version`
2. If not installed, download from https://www.python.org/downloads/ or use Homebrew: `brew install python`

**Linux (Ubuntu/Debian):**
```bash
sudo apt update
sudo apt install python3 python3-pip
```

### 2. Git (Optional but recommended)
Download from https://git-scm.com/downloads

## Installation

### Step 1: Download the Project

**Option A: Download ZIP**
1. Go to the project repository
2. Click "Code" → "Download ZIP"
3. Extract the ZIP file to a folder on your computer

**Option B: Clone with Git (if you have Git installed)**
```bash
git clone <repository-url>
cd skip-trace-platform
```

### Step 2: Install Required Packages

1. Open Command Prompt (Windows) or Terminal (macOS/Linux)
2. Navigate to the project folder:
   ```bash
   cd path/to/skip-trace-platform
   ```
3. Install the required packages:
   ```bash
   pip install -r requirements.txt
   ```

   If you get permission errors on macOS/Linux, use:
   ```bash
   pip3 install -r requirements.txt
   ```

## Configuration

### Step 1: Get API Keys

#### ZenRows API Key (Required)
1. Go to https://www.zenrows.com/
2. Sign up for an account
3. Get your API key from the dashboard
4. The free plan includes 1,000 requests per month

#### Supabase Setup (Required)
1. Go to https://supabase.com/
2. Create a new project
3. Go to Settings → API
4. Copy your Project URL and anon/public key

### Step 2: Configure Environment Variables

1. In the project folder, create a file named `.env`
2. Open `.env` in a text editor and add:

```env
# ZenRows Configuration
ZENROWS_API_KEY=your_zenrows_api_key_here
ZENROWS_WAIT_MS=1000

# Supabase Configuration
SUPABASE_URL=https://your-project-id.supabase.co
SUPABASE_KEY=your-supabase-anon-key-here

# Optional: Heartbeat interval for long-running requests
HEARTBEAT_INTERVAL=15.0
```

**Important:** Replace the placeholder values with your actual API keys and URLs.

### Step 3: Set Up Database Tables

1. In your Supabase dashboard, go to the SQL Editor
2. Copy the contents of `supabase_setup.sql` and run it
3. This will create the necessary tables: `jobs` and `results`

## Running the Application

### Step 1: Start the Server

#### Windows (Using Command Prompt)

1. **Open Command Prompt:**
   - Press `Win + R`, type `cmd`, and press Enter
   - Or search for "Command Prompt" in the Start menu

2. **Navigate to the project folder:**
   ```cmd
   cd C:\path\to\your\skip-trace-platform
   ```
   *(Replace with your actual folder path)*

3. **Run the application:**
   ```cmd
   python app.py
   ```

4. **Expected output:**
   ```
   * Running on http://127.0.0.1:5001/ (Press CTRL+C to quit)
   ```

5. **Keep the Command Prompt window open** - the application is running as long as this window is open

#### macOS/Linux (Using Terminal)

1. **Open Terminal:**
   - macOS: Search for "Terminal" in Spotlight (Cmd + Space)
   - Linux: Search for "Terminal" in applications

2. **Navigate to the project folder:**
   ```bash
   cd /path/to/your/skip-trace-platform
   ```

3. **Run the application:**
   ```bash
   python3 app.py
   ```

4. **Expected output:**
   ```
   * Running on http://127.0.0.1:5001/ (Press CTRL+C to quit)
   ```

**Note:** Keep the terminal/command prompt window open while using the application. Closing it will stop the server.

### Step 2: Open in Browser

1. Open your web browser
2. Go to: http://127.0.0.1:5001/
3. You should see the Skip Trace Platform interface

## Usage

### Step 1: Prepare Your CSV File

Create a CSV file with columns containing address information. Supported column names:
- `Address` or `address`
- `City` or `city`
- `State` or `state`
- `Zip` or `zip`
- `County` or `county` (optional)

Example CSV content:
```csv
Address,City,State,Zip
123 Main St,Springfield,IL,62701
456-458 Oak Ave,Chicago,IL,60601
789 Pine Rd,Madison,WI,53703
```

### Step 2: Upload and Process

1. On the main page, click "Choose File"
2. Select your CSV file
3. Click "Upload and Process"
4. The system will start processing automatically
5. Monitor progress in real-time on the page

### Step 3: Download Results

1. Once processing is complete, go to the Downloads page
2. Find your job in the list
3. Click the download link to get your results as CSV

## Understanding the Results

The output CSV will contain:
- Original address information
- Owner's first and last name
- Phone numbers (split into separate columns)
- Email addresses
- Mailing addresses
- Profile URLs
- Processing status

## Troubleshooting

### Common Issues

**1. "Module not found" errors:**
- Make sure you ran `pip install -r requirements.txt`
- Try `pip3` instead of `pip` on macOS/Linux

**2. "Connection refused" or API errors:**
- Check your internet connection
- Verify your ZenRows API key is correct and has credits
- Check your Supabase URL and key

**3. "No results found":**
- The address might not have public records
- Try different address formats
- Check if the address actually exists

**4. Slow processing:**
- This is normal - web scraping takes time
- The application processes addresses sequentially to avoid being blocked
- Large CSV files will take longer

**5. Application won't start:**
- Make sure no other application is using port 5001
- Check that Python is properly installed
- Look for error messages in the terminal/command prompt

### Checking Logs

- The application creates a `app.log` file with detailed information
- Check this file if you encounter issues
- For desktop app issues, also check `desktop_app.log`

### Desktop App Issues

**App closes immediately on startup:**
- Run the debug script first: `python debug_desktop.py`
- Check for missing dependencies or import errors
- Ensure PyQt6 is properly installed
- Look for Qt platform plugin errors

**Qt platform plugin errors:**
- On macOS: May need to install Qt plugins
- Try: `pip install PyQt6-Qt6` (includes platform plugins)
- Or set environment variable: `export QT_QPA_PLATFORM_PLUGIN_PATH=/path/to/plugins`

**Build failures:**
- Ensure all dependencies are installed before building
- Try different PyInstaller versions
- Check available disk space
- On macOS, ensure Xcode command line tools are installed

### Rate Limits

- ZenRows has monthly limits based on your plan
- The application includes delays to avoid being blocked
- If you get blocked, wait a few minutes before trying again

## Advanced Configuration

### Environment Variables

You can customize behavior with these environment variables in your `.env` file:

- `ZENROWS_WAIT_MS`: Time to wait for page rendering (default: 1000ms)
- `HEARTBEAT_INTERVAL`: Interval for progress updates (default: 15.0 seconds)
- `PORT`: Port to run the server on (default: 5001)

### Performance Tuning

For better performance:
- Use a paid ZenRows plan for higher limits
- Process smaller CSV files
- Avoid processing the same addresses multiple times

## Support

If you encounter issues:
1. Check the troubleshooting section above
2. Review the `app.log` file for error details
3. Ensure all API keys and configurations are correct

## License

This project is for educational and legitimate skip tracing purposes only. Ensure you comply with local laws and terms of service of data sources.

## Desktop Application

A standalone desktop version is available that doesn't require command-line usage.

### Building the Desktop App

#### Prerequisites
- Python 3.8+ installed
- All requirements from `requirements.txt` installed
- PyInstaller (automatically installed by build script)

#### Build Instructions

**Windows:**
```cmd
python build_desktop.py
```

**macOS:**
```bash
python3 build_desktop.py
```

#### Pre-Build Testing

Before building, test that everything works:

**Windows:**
```cmd
python debug_desktop.py
```

**macOS:**
```bash
python3 debug_desktop.py
```

This will test all imports and basic functionality. Fix any issues before building.

#### Build Process

The build script will:
1. Install PyInstaller automatically (with version fallback)
2. Create a standalone executable/application
3. Package all necessary files and dependencies
4. Output to `dist/` folder

#### Running the Desktop App

**Windows:**
- Run `SkipTraceDesktop.exe` from the `dist/SkipTraceDesktop/` folder
- No installation required - just extract and run

**macOS:**
- Copy `SkipTraceDesktop.app` to your Applications folder
- Run like any other macOS application

### Desktop App Features

- **Native GUI**: Professional desktop interface with tabs and menus
- **File Browser**: Click to select CSV files instead of typing paths
- **Real-time Progress**: Visual progress bars and live status updates
- **Background Processing**: Non-blocking UI during long operations
- **Settings Management**: Built-in settings editor for API keys
- **Results Viewer**: Table view of all processing jobs and results
- **Export Functionality**: Direct CSV export from the application

### Desktop vs Web Version

| Feature | Web Version | Desktop Version |
|---------|-------------|-----------------|
| Interface | Browser-based | Native desktop GUI |
| Installation | Requires Python + Flask | Standalone executable |
| User Experience | Web interface | Native app feel |
| System Resources | Higher (runs web server) | Lower (direct execution) |
| Portability | Requires internet browser | Self-contained app |
| Concurrent Jobs | Limited by server | Single application instance |

## Disclaimer

This tool scrapes publicly available information. Use responsibly and in accordance with applicable laws and regulations. The authors are not responsible for misuse of this tool.
