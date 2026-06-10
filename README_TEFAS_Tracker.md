# TEFAS Fund Daily Flow Tracker

Track daily inflows and outflows for Turkish TEFAS mutual funds (like ZBJ).

## Files

| File | Description |
|------|-------------|
| `tefas_common.py` | Shared utilities (number parsing, flow calc, retry) |
| `tefas_flow.py` | Lightweight script with `requests` + `pandas` |
| `tefas_flow_tracker.py` | Full version with `BeautifulSoup`, `argparse`, `pandas` |
| `test_tefas_common.py` | Tests for shared utilities (30 tests) |
| `requirements.txt` | Python package dependencies |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run for your fund (e.g., ZBJ)
python tefas_flow.py ZBJ

# 3. Run again tomorrow to see flow calculation
python tefas_flow.py ZBJ
```

### Dry Run (print without saving)

```bash
python tefas_flow.py ZBJ --dry-run
python tefas_flow_tracker.py ZBJ --dry-run
```

## How It Works

### Data Fetched Daily
- **Son Fiyat** (Unit Price)
- **Pay (Adet)** (Shares Outstanding)
- **Fon Toplam Değer** (Total NAV)
- **Günlük Getiri** (Daily Return %)
- **Yatırımcı Sayısı** (Number of Investors)

### Flow Calculation

**Method 1 (Most Accurate):**
```
Net Flow = (Today's Shares - Yesterday's Shares) × Today's Price
```

**Method 2 (Cross-check):**
```
Net Flow = Today's NAV - [Yesterday's NAV × (1 + Yesterday's Daily Return)]
```

## Output Files

| File | Content |
|------|---------|
| `ZBJ_history.csv` | Raw daily data from TEFAS |
| `ZBJ_flows.csv` | Calculated daily flows |

## Features

- **Dual calculation methods** for cross-verification
- **Automatic retry** with exponential backoff (3 attempts)
- **API-first, HTML-fallback** fetching strategy
- **Safe number parsing** (Turkish format: `50.226.984.550,34`)
- `--dry-run` flag to preview without saving
- Turkish number parsing handles `None`, `-`, empty, and already-numeric inputs

## Testing

```bash
pip install -r requirements.txt pytest pytest-mock
pytest test_tefas_common.py -v
```

## Automation

### Linux/Mac (Cron)
```bash
# Edit crontab
crontab -e

# Add line to run daily at 9:00 AM (after TEFAS updates overnight)
0 9 * * * cd /path/to/script && python tefas_flow.py ZBJ >> tefas.log 2>&1
```

### Windows (Task Scheduler)
1. Open Task Scheduler
2. Create Basic Task → Daily
3. Action: Start a program
4. Program: `python.exe`
5. Arguments: `C:\path\to\tefas_flow.py ZBJ`

### Python Schedule (Cross-platform)
```bash
pip install schedule
```

Create `run_daily.py`:
```python
import schedule
import time
import os

def job():
    os.system("python tefas_flow.py ZBJ")

schedule.every().day.at("09:00").do(job)

while True:
    schedule.run_pending()
    time.sleep(60)
```

## Important Notes

1. **TEFAS updates overnight** - best to run after 9 AM Istanbul time
2. **T+1 settlement** - flows reflect transaction date, not settlement
3. **TEFAS may block scraping** - if blocked, use Fonoloji API or manual entry
4. **No official API** - TEFAS doesn't provide an official API, so scraping may break if they change the site

## Alternative: Manual Entry

If scraping fails, use the CSV template and enter data manually from tefas.gov.tr:

```csv
date,fund_code,son_fiyat,pay_adet,fon_toplam_deger,gunluk_getiri_pct,yatirimci_sayisi
2026-06-09,ZBJ,4.783720,10475000000,50100000000,0.1093,19700
2026-06-10,ZBJ,4.788947,10488106381,50226984550,0.1093,19760
```

The script will still calculate flows from your manual entries.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Could not fetch data" | TEFAS changed their site. Check if page loads in browser. Try updating regex patterns. |
| "HTTP error" | Check internet connection. TEFAS may be blocking your IP. The script retries 3x automatically. |
| Wrong flow numbers | Verify yesterday's data is correct. Check that price/share/NAV are in Turkish format. |

## Data Sources

- Primary: https://www.tefas.gov.tr/tr/fon-detayli-analiz/ZBJ
- Alternative APIs: Fonoloji (fonoloji.com), tefasfon PyPI package
