#!/usr/bin/env python3
"""
TEFAS Fund Daily Flow Tracker v2
Pulls data from TEFAS and calculates daily inflows/outflows.

Usage:
    python tefas_flow.py ZBJ
    python tefas_flow.py ZBJ --dry-run

Requirements:
    pip install requests pandas
"""

import sys
import re
import json
import csv
import argparse
import logging
from datetime import datetime
from pathlib import Path

try:
    import requests
    import pandas as pd
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install requests pandas")
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


class TEFAScraper:
    """Scrapes TEFAS fund data."""

    def __init__(self, fund_code: str):
        self.fund_code = fund_code.upper()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7',
            'Origin': 'https://www.tefas.gov.tr',
            'Referer': 'https://www.tefas.gov.tr/',
        })

    def fetch_from_api(self):
        """Try to fetch from TEFAS internal API endpoints."""
        endpoints = [
            f"https://www.tefas.gov.tr/api/DB/BindHistoryInfo?fundCode={self.fund_code}",
            f"https://www.tefas.gov.tr/api/funds/{self.fund_code}",
            f"https://www.tefas.gov.tr/api/FonAnaliz/GetFundDetail?fonKodu={self.fund_code}",
        ]

        for url in endpoints:
            try:
                resp = self.session.get(url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        return self._parse_api_response(data)
            except Exception:
                continue
        return None

    def _parse_api_response(self, data):
        """Parse API response into standard format."""
        result = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'fund_code': self.fund_code,
        }

        if isinstance(data, list) and len(data) > 0:
            item = data[-1]
        elif isinstance(data, dict):
            item = data
        else:
            return None

        field_map = {
            'son_fiyat': ['sonFiyat', 'fiyat', 'price', 'Fiyat', 'Price'],
            'pay_adet': ['payAdet', 'pay', 'shares', 'Pay', 'Shares', 'number_of_shares'],
            'fon_toplam_deger': ['fonToplamDeger', 'toplamDeger', 'nav', 'NAV', 'market_cap', 'aum'],
            'gunluk_getiri_pct': ['gunlukGetiri', 'getiri', 'dailyReturn', 'return_1d', 'gunluk_getiri'],
            'yatirimci_sayisi': ['yatirimciSayisi', 'yatirimci', 'investors', 'Investors', 'number_of_investors'],
        }

        for key, aliases in field_map.items():
            for alias in aliases:
                if alias in item:
                    val = item[alias]
                    if isinstance(val, str):
                        val = parse_turkish_number(val)
                    if val is not None:
                        result[key] = val
                        break

        return result

    def fetch_from_html(self):
        """Scrape from the HTML detail page."""
        url = f"https://www.tefas.gov.tr/tr/fon-detayli-analiz/{self.fund_code}"

        try:
            resp = fetch_with_retry(self.session, url, max_retries=3, backoff=2, timeout=30)
        except Exception as e:
            print(f"HTTP error: {e}")
            return None

        html = resp.text
        result = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'fund_code': self.fund_code,
        }

        # Try RSC payload (Next.js App Router format)
        parsed = self._parse_rsc_payload(html)
        if parsed:
            return parsed

        # Try to find JSON embedded in script tags
        state_patterns = [
            r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
            r'window\.__DATA__\s*=\s*({.*?});',
            r'"fundDetail":\s*({.*?})',
        ]

        for pattern in state_patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    json_str = match.group(1)
                    data = json.loads(json_str)
                    parsed = self._parse_api_response(data)
                    if parsed:
                        return parsed
                except json.JSONDecodeError:
                    continue

        # Fallback: regex extraction from HTML text
        price_match = re.search(r'Son\s*Fiyat.*?([\d]+,[\d]{3,6})', html, re.I | re.DOTALL)
        if price_match:
            result['son_fiyat'] = parse_turkish_number(price_match.group(1))

        shares_match = re.search(r'Pay\s*\(Adet\).*?([\d.,]+)', html, re.I | re.DOTALL)
        if shares_match:
            result['pay_adet'] = safe_int(shares_match.group(1))

        nav_match = re.search(r'Fon\s*Toplam\s*Değer.*?([\d.,]+)', html, re.I | re.DOTALL)
        if nav_match:
            result['fon_toplam_deger'] = safe_float(nav_match.group(1))

        return_match = re.search(r'Günlük\s*Getiri.*?([\d.,]+)\s*%', html, re.I | re.DOTALL)
        if return_match:
            result['gunluk_getiri_pct'] = parse_turkish_number(return_match.group(1))

        inv_match = re.search(r'Yatırımcı\s*Sayısı.*?([\d.,]+)', html, re.I | re.DOTALL)
        if inv_match:
            result['yatirimci_sayisi'] = safe_int(inv_match.group(1))

        return result if len(result) > 2 else None

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

    def fetch(self):
        """Fetch data using best available method."""
        data = self.fetch_from_api()
        if data:
            print("Data fetched from API")
            return data

        data = self.fetch_from_html()
        if data:
            print("Data fetched from HTML")
            return data

        print("ERROR: Could not fetch data. TEFAS may have changed their site.")
        return None


def process_fund(fund_code, dry_run=False):
    """Fetch, calculate, save for a single fund. Returns flow dict or None."""
    fund_code = fund_code.upper()
    history_file = f"{fund_code}_history.csv"
    flow_file = f"{fund_code}_flows.csv"

    print(f"\n{'='*60}")
    print(f"TEFAS Flow Tracker - {fund_code}")
    print(f"{'='*60}\n")

    scraper = TEFAScraper(fund_code)
    today = scraper.fetch()

    if not today:
        return None

    print(f"\nToday's Data ({today['date']}):")
    print(f"  Price:        {format_optional(today.get('son_fiyat'), fmt=',.6f')} TL")
    print(f"  Shares:       {format_optional(today.get('pay_adet'), fmt=',.0f')}")
    print(f"  NAV:          {format_optional(today.get('fon_toplam_deger'), fmt=',.0f')} TL")
    print(f"  Daily Return: {format_optional(today.get('gunluk_getiri_pct'), fmt='.4f')}%")
    print(f"  Investors:    {format_optional(today.get('yatirimci_sayisi'), fmt=',.0f')}")

    history = []
    if Path(history_file).exists():
        with open(history_file, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            history = list(reader)

    if not history:
        print(f"\nNo history found. Saving baseline.")
        if not dry_run:
            with open(history_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=today.keys())
                writer.writeheader()
                writer.writerow(today)
            print(f"Saved to {history_file}")
        else:
            print(f"[DRY-RUN] Would save to {history_file}")
        print("Run again tomorrow to calculate flows.")
        return None

    yesterday = history[-1]
    yesterday_parsed = {}
    for key in ['son_fiyat', 'fon_toplam_deger', 'gunluk_getiri_pct']:
        val = yesterday.get(key)
        if val is not None and val != '':
            try:
                yesterday_parsed[key] = float(val)
            except (ValueError, TypeError):
                pass
    for key in ['pay_adet', 'yatirimci_sayisi']:
        val = safe_int(yesterday.get(key))
        if val is not None:
            yesterday_parsed[key] = val
    for k, v in yesterday.items():
        if k not in yesterday_parsed:
            yesterday_parsed[k] = v

    flow = calculate_flow(today, yesterday_parsed)

    print(f"\n{'='*60}")
    print(f"DAILY FLOW CALCULATION")
    print(f"{'='*60}")

    if 'flow_shares_tl' in flow:
        print(f"\nMethod 1 (Share Change):")
        print(f"  Share Change: {flow['share_change']:+,.0f}")
        print(f"  Net Flow:     {flow['flow_shares_tl']:+,.0f} TL")
        print(f"  = {flow['flow_shares_mil']:+.1f} million TL [{flow['direction']}]")

    if 'flow_nav_tl' in flow:
        print(f"\nMethod 2 (NAV - Return):")
        print(f"  Net Flow:     {flow['flow_nav_tl']:+,.0f} TL")
        print(f"  = {flow['flow_nav_mil']:+.1f} million TL")

    if 'investor_change' in flow:
        print(f"\nInvestor Change: {flow['investor_change']:+,.0f}")

    if dry_run:
        print(f"\n[DRY-RUN] Would save to {history_file} and {flow_file}")
        return flow

    with open(history_file, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=today.keys())
        writer.writerow(today)

    flow_exists = Path(flow_file).exists()
    with open(flow_file, 'a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=flow.keys())
        if not flow_exists:
            writer.writeheader()
        writer.writerow(flow)

    print(f"\nSaved to {history_file} and {flow_file}")
    return flow


def main():
    parser = argparse.ArgumentParser(description='TEFAS Fund Daily Flow Tracker')
    parser.add_argument('fund_code', nargs='?', help='Fund code (e.g., ZBJ, AFT, MAC)')
    parser.add_argument('--funds-file', help='File with one fund code per line')
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

    for code in fund_codes:
        process_fund(code, dry_run=args.dry_run)

    if args.chart and fund_codes:
        flow_dfs = []
        for code in fund_codes:
            flow_file = f"{code}_flows.csv"
            if Path(flow_file).exists():
                flow_dfs.append(pd.read_csv(flow_file))
        if flow_dfs:
            all_flows = pd.concat(flow_dfs, ignore_index=True)
            chart_file = 'portfolio_cumulative_flow.png' if len(fund_codes) > 1 else f'{fund_codes[0]}_cumulative_flow.png'
            generate_flow_chart(all_flows, chart_file)


if __name__ == '__main__':
    main()
