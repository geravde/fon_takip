#!/usr/bin/env python3
"""
Shared utilities for TEFAS Fund Daily Flow Tracker.
"""

import time
import logging
from typing import Optional

try:
    import pandas as pd
except ImportError:
    pd = None

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
        result['direction'] = 'INFLOW' if flow > 0 else ('NEUTRAL' if flow == 0 else 'OUTFLOW')

    # Method 2: NAV minus expected return (return derived from prices)
    fon_toplam_today = today.get('fon_toplam_deger')
    fon_toplam_yesterday = yesterday.get('fon_toplam_deger')
    son_fiyat_yesterday = yesterday.get('son_fiyat')

    if (fon_toplam_today is not None and fon_toplam_yesterday is not None
            and son_fiyat_today is not None and son_fiyat_yesterday is not None
            and son_fiyat_yesterday != 0):
        ret = son_fiyat_today / son_fiyat_yesterday - 1
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


def backfill_flows(history_file, flow_file):
    """Backfill missing flow entries from history CSV into flows CSV.

    Scans history for consecutive day-pairs that don't yet have a
    corresponding row in the flows file, computes them, and appends.
    """
    from pathlib import Path

    if pd is None:
        return

    try:
        history = pd.read_csv(history_file)
    except Exception:
        return

    if len(history) < 2:
        return

    # Collect existing flow dates
    flow_dates = set()
    existing = None
    if Path(flow_file).exists():
        try:
            existing = pd.read_csv(flow_file)
            flow_dates = set(existing['date'].astype(str))
        except Exception:
            existing = None

    def _parse(r):
        d = {'date': str(r.get('date', '')), 'fund_code': str(r.get('fund_code', ''))}
        for key in ['son_fiyat', 'fon_toplam_deger', 'gunluk_getiri_pct']:
            val = r.get(key)
            if val is not None and val != '' and not (isinstance(val, float) and pd.isna(val)):
                try:
                    d[key] = float(val)
                except (ValueError, TypeError):
                    pass
        for key in ['pay_adet', 'yatirimci_sayisi']:
            val = r.get(key)
            if val is not None and val != '' and not (isinstance(val, float) and pd.isna(val)):
                try:
                    d[key] = int(float(val))
                except (ValueError, TypeError):
                    pass
        return d

    new_flows = []
    for i in range(1, len(history)):
        today_date = str(history.iloc[i]['date'])
        if today_date in flow_dates:
            continue
        flow = calculate_flow(_parse(history.iloc[i]), _parse(history.iloc[i-1]))
        new_flows.append(flow)

    if not new_flows:
        return

    new_df = pd.DataFrame(new_flows)
    combined = pd.concat([existing, new_df], ignore_index=True) if existing is not None else new_df
    combined = combined.sort_values(['fund_code', 'date']).reset_index(drop=True)
    combined.to_csv(flow_file, index=False)
    print(f"Backfilled {len(new_flows)} missing flow entries to {flow_file}")


def generate_flow_chart(flow_df, output_path):
    """Generate a cumulative flow chart and save to PNG."""
    if flow_df.empty:
        print("No flow data to chart.")
        return

    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib not installed. Install with: pip install matplotlib")
        return

    df = flow_df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values(['fund_code', 'date'])

    df['cum_shares'] = df.groupby('fund_code')['flow_shares_mil'].cumsum()
    df['cum_nav'] = df.groupby('fund_code')['flow_nav_mil'].cumsum()

    fig, ax = plt.subplots(figsize=(14, 7))
    funds = df['fund_code'].unique()
    colors = plt.cm.tab10(np.linspace(0, 1, len(funds)))

    for i, fund in enumerate(funds):
        fdata = df[df['fund_code'] == fund]
        ax.plot(fdata['date'], fdata['cum_shares'], color=colors[i], linestyle='-',
                linewidth=2, label=f'{fund} – Method 1')
        ax.plot(fdata['date'], fdata['cum_nav'], color=colors[i], linestyle='--',
                linewidth=1.5, alpha=0.7, label=f'{fund} – Method 2')

    ax.axhline(y=0, color='gray', linestyle=':', linewidth=0.8)
    ax.set_xlabel('Date')
    ax.set_ylabel('Cumulative Flow (Million TL)')
    ax.set_title('Cumulative Fund Flows')
    ax.legend(loc='best', fontsize=9)
    ax.grid(True, alpha=0.3)
    plt.xticks(rotation=45)
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f"Chart saved: {output_path}")
