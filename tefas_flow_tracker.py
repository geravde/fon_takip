#!/usr/bin/env python3
"""
TEFAS Fund Daily Flow Tracker
Scrapes fund detail page from tefas.gov.tr and calculates daily inflows/outflows.

Usage:
    python tefas_flow_tracker.py ZBJ
    python tefas_flow_tracker.py ZBJ --history-file zbj_history.csv
    python tefas_flow_tracker.py ZBJ --daily  # Run once and append to history
    python tefas_flow_tracker.py ZBJ --dry-run

Requirements:
    pip install requests beautifulsoup4 pandas
"""

import sys
import re
import json
import csv
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Tuple

try:
    import requests
    from bs4 import BeautifulSoup
    import pandas as pd
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install requests beautifulsoup4 pandas")
    sys.exit(1)

from tefas_common import (
    parse_turkish_number,
    safe_int,
    safe_float,
    calculate_flow,
    fetch_with_retry,
    format_optional,
    generate_flow_chart,
)

logger = logging.getLogger(__name__)


class TEFASFlowTracker:
    """Tracks daily inflows/outflows for TEFAS mutual funds."""

    BASE_URL = "https://www.tefas.gov.tr/tr/fon-detayli-analiz/{code}"

    def __init__(self, fund_code: str, history_file: Optional[str] = None):
        self.fund_code = fund_code.upper()
        self.history_file = history_file or f"{self.fund_code}_history.csv"
        self.flow_file = f"{self.fund_code}_flows.csv"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7',
            'Origin': 'https://www.tefas.gov.tr',
            'Referer': 'https://www.tefas.gov.tr/',
        })

    def _extract_value_by_label(self, soup: BeautifulSoup, label: str) -> Optional[str]:
        """Extract value from TEFAS detail page by label text."""
        for elem in soup.find_all(text=re.compile(re.escape(label), re.I)):
            parent = elem.parent
            if parent:
                next_sib = parent.find_next_sibling()
                if next_sib and next_sib.get_text(strip=True):
                    return next_sib.get_text(strip=True)

                grandparent = parent.parent
                if grandparent:
                    for child in grandparent.children:
                        if hasattr(child, 'get_text') and child != parent:
                            text = child.get_text(strip=True)
                            if text and text != label:
                                return text

        for td in soup.find_all(['td', 'div', 'span']):
            text = td.get_text(strip=True)
            if label.lower() in text.lower():
                next_td = td.find_next_sibling(['td', 'div', 'span'])
                if next_td:
                    return next_td.get_text(strip=True)

        return None

    def _extract_all_metrics(self, soup: BeautifulSoup) -> Dict:
        """Extract all key metrics from the fund detail page."""
        metrics = {}

        # Son Fiyat (Unit Price)
        for elem in soup.find_all(text=re.compile(r'Son\s*Fiyat', re.I)):
            parent = elem.parent
            if parent:
                container = parent.parent or parent
                numbers = re.findall(r'[\d.,]+', container.get_text())
                for num in numbers:
                    parsed = parse_turkish_number(num)
                    if parsed is not None and 1 < parsed < 1000:
                        metrics['son_fiyat'] = parsed
                        break

        # Pay (Shares)
        for elem in soup.find_all(text=re.compile(r'Pay\s*\(Adet\)|Pay\s*Sayısı', re.I)):
            parent = elem.parent
            if parent:
                container = parent.parent or parent
                numbers = re.findall(r'[\d.]+(?:,[\d]+)?', container.get_text())
                for num in numbers:
                    parsed = parse_turkish_number(num)
                    if parsed is not None and parsed > 1_000_000:
                        metrics['pay_adet'] = int(parsed)
                        break

        # Fon Toplam Değer (Total NAV)
        for elem in soup.find_all(text=re.compile(r'Fon\s*Toplam\s*Değer|Toplam\s*Değer', re.I)):
            parent = elem.parent
            if parent:
                container = parent.parent or parent
                numbers = re.findall(r'[\d.]+,[\d]+', container.get_text())
                for num in numbers:
                    parsed = parse_turkish_number(num)
                    if parsed is not None and parsed > 1_000_000_000:
                        metrics['fon_toplam_deger'] = parsed
                        break

        # Günlük Getiri (Daily Return)
        for elem in soup.find_all(text=re.compile(r'Günlük\s*Getiri|Günlük', re.I)):
            parent = elem.parent
            if parent:
                container = parent.parent or parent
                pct_match = re.search(r'(%?\s*[\d,]+\s*%)', container.get_text())
                if pct_match:
                    parsed = parse_turkish_number(pct_match.group(1))
                    if parsed is not None and -10 < parsed < 10:
                        metrics['gunluk_getiri_pct'] = parsed
                        break

        # Yatırımcı Sayısı (Investor Count)
        for elem in soup.find_all(text=re.compile(r'Yatırımcı\s*Sayısı|Yatırımcı', re.I)):
            parent = elem.parent
            if parent:
                container = parent.parent or parent
                numbers = re.findall(r'[\d.]+', container.get_text())
                for num in numbers:
                    parsed = parse_turkish_number(num)
                    if parsed is not None and 1000 < parsed < 100_000_000:
                        metrics['yatirimci_sayisi'] = int(parsed)
                        break

        return metrics

    def _parse_rsc_payload(self, html):
        """Extract fund data from Next.js RSC payload (bilgiData)."""
        start = html.find('\\"bilgiData\\":{')
        if start == -1:
            return None

        brace_start = html.find('{', start)
        if brace_start == -1:
            return None

        count = 0
        in_escape = False
        end = brace_start
        for i in range(brace_start, len(html)):
            if html[i] == '\\' and not in_escape:
                in_escape = True
                continue
            if in_escape:
                in_escape = False
                continue
            if html[i] == '{':
                count += 1
            elif html[i] == '}':
                count -= 1
                if count == 0:
                    end = i + 1
                    break

        raw_json = html[brace_start:end]
        json_str = raw_json.replace('\\"', '"')

        result = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'fund_code': self.fund_code,
        }

        fields = {
            'son_fiyat': r'"sonFiyat":([0-9.]+)',
            'gunluk_getiri_pct': r'"gunlukGetiri":([0-9.]+)',
            'pay_adet': r'"payAdet":([0-9]+)',
            'fon_toplam_deger': r'"portBuyukluk":([0-9.]+)',
            'yatirimci_sayisi': r'"yatirimciSayi":([0-9]+)',
        }

        for key, pattern in fields.items():
            m = re.search(pattern, json_str)
            if m:
                val = m.group(1)
                result[key] = float(val) if '.' in val else int(val)

        return result if len(result) > 2 else None

    def fetch_today(self) -> Dict:
        """Fetch today's fund data from TEFAS."""
        url = self.BASE_URL.format(code=self.fund_code)
        print(f"Fetching {url}...")

        try:
            response = fetch_with_retry(self.session, url, max_retries=3, backoff=2, timeout=30)
        except Exception as e:
            print(f"Error fetching page: {e}")
            return {}

        html = response.text

        rsc_data = self._parse_rsc_payload(html)
        if rsc_data:
            print("Data fetched from RSC payload")
            return rsc_data

        soup = BeautifulSoup(html, 'html.parser')

        metrics = self._extract_all_metrics(soup)

        if not metrics:
            print("WARNING: Could not extract metrics from page. TEFAS may have changed layout.")
            print("Page title:", soup.title.string if soup.title else "No title")
            debug_file = f"{self.fund_code}_debug.html"
            with open(debug_file, 'w', encoding='utf-8') as f:
                f.write(response.text)
            print(f"Saved debug HTML to {debug_file}")

        metrics['date'] = datetime.now().strftime('%Y-%m-%d')
        metrics['fund_code'] = self.fund_code
        metrics['fetch_time'] = datetime.now().isoformat()

        return metrics

    def load_history(self) -> pd.DataFrame:
        """Load historical data from CSV."""
        if not Path(self.history_file).exists():
            return pd.DataFrame()
        return pd.read_csv(self.history_file)

    def save_to_history(self, data: Dict):
        """Append today's data to history CSV."""
        df = self.load_history()
        new_row = pd.DataFrame([data])

        if not df.empty and 'date' in df.columns:
            df = df[df['date'] != data['date']]

        df = pd.concat([df, new_row], ignore_index=True)
        df.to_csv(self.history_file, index=False)
        print(f"Saved to {self.history_file}")

    def run_daily(self, dry_run: bool = False) -> Optional[Dict]:
        """Run the daily fetch and flow calculation. Returns flow dict or None."""
        print(f"\n{'='*60}")
        print(f"TEFAS Flow Tracker - {self.fund_code}")
        print(f"{'='*60}\n")

        today = self.fetch_today()
        if not today:
            print("Failed to fetch today's data. Exiting.")
            return None

        print(f"\nToday's Data:")
        print(f"  Date: {today['date']}")
        print(f"  Price: {format_optional(today.get('son_fiyat'), fmt=',.6f')} TL")
        print(f"  Shares: {format_optional(today.get('pay_adet'), fmt=',.0f')}")
        print(f"  NAV: {format_optional(today.get('fon_toplam_deger'), fmt=',.0f')} TL")
        print(f"  Daily Return: {format_optional(today.get('gunluk_getiri_pct'), fmt='.4f')}%")
        print(f"  Investors: {format_optional(today.get('yatirimci_sayisi'), fmt=',.0f')}")

        history = self.load_history()

        if history.empty:
            print(f"\nNo history found. Saving today's data as baseline.")
            if not dry_run:
                self.save_to_history(today)
            else:
                print(f"[DRY-RUN] Would save to {self.history_file}")
            print(f"Run again tomorrow to see flow calculations.")
            return None

        prior = history[history['date'] != today.get('date')]
        if prior.empty:
            print(f"\nNo prior day data. Saving today's data as baseline.")
            if not dry_run:
                self.save_to_history(today)
            else:
                print(f"[DRY-RUN] Would save to {self.history_file}")
            print(f"Run again tomorrow to see flow calculations.")
            return None

        yesterday_raw = prior.iloc[-1].to_dict()

        yesterday = {}
        for key in ['son_fiyat', 'fon_toplam_deger', 'gunluk_getiri_pct']:
            val = yesterday_raw.get(key)
            if val is not None and val != '':
                try:
                    yesterday[key] = float(val)
                except (ValueError, TypeError):
                    pass
        for key in ['pay_adet', 'yatirimci_sayisi']:
            val = safe_int(yesterday_raw.get(key))
            if val is not None:
                yesterday[key] = val
        for k, v in yesterday_raw.items():
            if k not in yesterday:
                yesterday[k] = v

        flow = calculate_flow(today, yesterday)

        print(f"\n{'='*60}")
        print(f"DAILY FLOW CALCULATION")
        print(f"{'='*60}")

        if flow.get('flow_shares_tl') is not None:
            direction = "INFLOW" if flow['flow_shares_tl'] > 0 else "OUTFLOW"
            print(f"\nMethod 1 (Share Change x Price):")
            print(f"  Share Change: {flow['share_change']:+,.0f}")
            print(f"  Net Flow: {flow['flow_shares_tl']:+,.0f} TL")
            print(f"  = {flow['flow_shares_mil']:+.1f} million TL ({direction})")

        if flow.get('flow_nav_tl') is not None:
            direction = "INFLOW" if flow['flow_nav_tl'] > 0 else "OUTFLOW"
            print(f"\nMethod 2 (NAV Change - Return):")
            print(f"  Net Flow: {flow['flow_nav_tl']:+,.0f} TL")
            print(f"  = {flow['flow_nav_mil']:+.1f} million TL ({direction})")

        if flow.get('investor_change') is not None:
            direction = "NEW" if flow['investor_change'] > 0 else "LEFT"
            print(f"\nInvestor Change: {flow['investor_change']:+,.0f} ({direction})")

        if dry_run:
            print(f"\n[DRY-RUN] Would save to history and flow files.")
            return flow

        self.save_to_history(today)

        flow_df = pd.DataFrame([flow])
        if Path(self.flow_file).exists():
            existing = pd.read_csv(self.flow_file)
            existing = existing[existing['date'] != flow['date']]
            flow_df = pd.concat([existing, flow_df], ignore_index=True)
        flow_df.to_csv(self.flow_file, index=False)
        print(f"\nFlow report saved to {self.flow_file}")
        return flow


def main():
    parser = argparse.ArgumentParser(description='TEFAS Fund Daily Flow Tracker')
    parser.add_argument('fund_code', nargs='?', help='Fund code (e.g., ZBJ, AFT, MAC)')
    parser.add_argument('--funds-file', help='File with one fund code per line')
    parser.add_argument('--history-file', help='Path to history CSV file')
    parser.add_argument('--daily', action='store_true', help='Run daily fetch and calculation')
    parser.add_argument('--show-history', action='store_true', help='Show saved history')
    parser.add_argument('--chart', action='store_true', help='Generate cumulative flow chart')
    parser.add_argument('--dry-run', action='store_true', help='Print without saving')

    args = parser.parse_args()

    fund_codes = []
    if args.fund_code:
        fund_codes.append(args.fund_code.upper())
    if args.funds_file:
        with open(args.funds_file, 'r') as f:
            for line in f:
                code = line.strip()
                if code:
                    fund_codes.append(code.upper())

    if not fund_codes:
        print("ERROR: Provide a fund code or --funds-file")
        sys.exit(1)

    if args.show_history:
        if len(fund_codes) > 1:
            print("ERROR: --show-history requires a single fund code")
            sys.exit(1)
        tracker = TEFASFlowTracker(fund_codes[0], args.history_file)
        history = tracker.load_history()
        print(history.to_string())
        return

    trackers = []
    for code in fund_codes:
        tracker = TEFASFlowTracker(code, args.history_file)
        tracker.run_daily(dry_run=args.dry_run)
        trackers.append(tracker)

    if args.chart and fund_codes:
        flow_dfs = []
        for tracker in trackers:
            if Path(tracker.flow_file).exists():
                flow_dfs.append(pd.read_csv(tracker.flow_file))
        if flow_dfs:
            all_flows = pd.concat(flow_dfs, ignore_index=True)
            chart_file = 'portfolio_cumulative_flow.png' if len(fund_codes) > 1 else f'{fund_codes[0]}_cumulative_flow.png'
            generate_flow_chart(all_flows, chart_file)


if __name__ == '__main__':
    main()
