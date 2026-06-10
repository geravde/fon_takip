#!/usr/bin/env python3
"""
Shared utilities for TEFAS Fund Daily Flow Tracker.
"""

import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def parse_turkish_number(text) -> Optional[float]:
    """Parse Turkish formatted numbers like '50.226.984.550,34' or '%0,1093'."""
    if text is None:
        return None
    # If already a number, return as-is
    if isinstance(text, (int, float)):
        return float(text)
    text = str(text).strip()
    if not text or text == '-':
        return None

    text = text.replace('%', '').strip()

    if ',' in text:
        parts = text.split(',')
        integer_part = parts[0].replace('.', '')
        decimal_part = parts[1].replace('.', '') if len(parts) > 1 else '0'
        clean = f"{integer_part}.{decimal_part}"
    else:
        clean = text.replace('.', '')

    try:
        return float(clean)
    except ValueError:
        return None


def safe_int(value) -> Optional[int]:
    """Convert a value to int safely, returning None on failure."""
    parsed = parse_turkish_number(value)
    if parsed is None:
        return None
    return int(parsed)


def safe_float(value) -> Optional[float]:
    """Convert a value to float safely, returning None on failure."""
    return parse_turkish_number(value)


def calculate_flow(today: dict, yesterday: dict) -> dict:
    """Calculate daily flow between two days of fund data."""
    result = {
        'date': today.get('date'),
        'fund_code': today.get('fund_code'),
    }

    # Method 1: Share change x current price
    pay_adet_today = today.get('pay_adet')
    pay_adet_yesterday = yesterday.get('pay_adet')
    son_fiyat_today = today.get('son_fiyat')

    if pay_adet_today is not None and pay_adet_yesterday is not None and son_fiyat_today is not None:
        share_change = pay_adet_today - pay_adet_yesterday
        flow = share_change * son_fiyat_today
        result['share_change'] = share_change
        result['flow_shares_tl'] = flow
        result['flow_shares_mil'] = round(flow / 1_000_000, 2)
        result['direction'] = 'INFLOW' if flow > 0 else 'OUTFLOW'

    # Method 2: NAV minus expected return
    fon_toplam_today = today.get('fon_toplam_deger')
    fon_toplam_yesterday = yesterday.get('fon_toplam_deger')
    getiri_yesterday = yesterday.get('gunluk_getiri_pct')

    if fon_toplam_today is not None and fon_toplam_yesterday is not None and getiri_yesterday is not None:
        ret = getiri_yesterday / 100
        expected = fon_toplam_yesterday * (1 + ret)
        flow = fon_toplam_today - expected
        result['flow_nav_tl'] = flow
        result['flow_nav_mil'] = round(flow / 1_000_000, 2)

    # Investor change
    inv_today = today.get('yatirimci_sayisi')
    inv_yesterday = yesterday.get('yatirimci_sayisi')
    if inv_today is not None and inv_yesterday is not None:
        result['investor_change'] = inv_today - inv_yesterday

    return result


def fetch_with_retry(session, url, max_retries=3, backoff=2, timeout=30):
    """Fetch a URL with exponential backoff retry."""
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            logger.warning(f"Attempt {attempt}/{max_retries} failed for {url}: {e}")
            if attempt < max_retries:
                sleep_time = backoff * (2 ** (attempt - 1))
                logger.info(f"Retrying in {sleep_time}s...")
                time.sleep(sleep_time)
    raise last_exc


def format_optional(value, fmt=",.0f", fallback="N/A"):
    """Format a value if not None, else return fallback."""
    if value is None:
        return fallback
    return f"{value:{fmt}}"
