#!/usr/bin/env python3
"""Tests for tefas_common shared utilities."""

import pytest
from tefas_common import (
    parse_turkish_number,
    safe_int,
    safe_float,
    calculate_flow,
    format_optional,
    fetch_with_retry,
)


class TestParseTurkishNumber:
    def test_simple_integer(self):
        assert parse_turkish_number("1234") == 1234.0

    def test_thousand_separator(self):
        assert parse_turkish_number("1.234") == 1234.0

    def test_turkish_decimal(self):
        assert parse_turkish_number("1234,56") == 1234.56

    def test_turkish_full_format(self):
        assert parse_turkish_number("50.226.984.550,34") == 50226984550.34

    def test_percentage(self):
        assert parse_turkish_number("%0,1093") == 0.1093

    def test_small_decimal(self):
        assert parse_turkish_number("4,788947") == 4.788947

    def test_none_input(self):
        assert parse_turkish_number(None) is None

    def test_empty_string(self):
        assert parse_turkish_number("") is None

    def test_dash_input(self):
        assert parse_turkish_number("-") is None

    def test_whitespace(self):
        assert parse_turkish_number("  1234,56  ") == 1234.56

    def test_invalid(self):
        assert parse_turkish_number("N/A") is None

    def test_already_float(self):
        assert parse_turkish_number(1234.56) == 1234.56

    def test_no_decimal_dots_removed(self):
        assert parse_turkish_number("1.234.567") == 1234567.0


class TestSafeInt:
    def test_simple(self):
        assert safe_int("1234") == 1234

    def test_with_thousands(self):
        assert safe_int("1.234") == 1234

    def test_none_input(self):
        assert safe_int(None) is None

    def test_invalid(self):
        assert safe_int("not a number") is None


class TestSafeFloat:
    def test_simple(self):
        assert safe_float("1234,56") == 1234.56

    def test_none_input(self):
        assert safe_float(None) is None


class TestFormatOptional:
    def test_with_value(self):
        assert format_optional(1234.5, ",.1f") == "1,234.5"

    def test_none_value(self):
        assert format_optional(None) == "N/A"

    def test_custom_fallback(self):
        assert format_optional(None, fallback="---") == "---"

    def test_int_format(self):
        assert format_optional(50000, ",.0f") == "50,000"


class TestCalculateFlow:
    def test_basic_inflow(self):
        today = {
            'date': '2026-06-11',
            'fund_code': 'ZBJ',
            'son_fiyat': 5.0,
            'pay_adet': 10_500_000_000,
            'fon_toplam_deger': 52_500_000_000.0,
            'gunluk_getiri_pct': 0.1,
            'yatirimci_sayisi': 20000,
        }
        yesterday = {
            'date': '2026-06-10',
            'fund_code': 'ZBJ',
            'son_fiyat': 4.9,
            'pay_adet': 10_000_000_000,
            'fon_toplam_deger': 49_000_000_000.0,
            'gunluk_getiri_pct': 0.05,
            'yatirimci_sayisi': 19700,
        }

        flow = calculate_flow(today, yesterday)

        # Share change: 500M shares x 5.0 TL = 2.5B TL inflow
        assert flow['share_change'] == 500_000_000
        assert flow['flow_shares_tl'] == 2_500_000_000.0
        assert flow['flow_shares_mil'] == 2500.0
        assert flow['direction'] == 'INFLOW'

        # NAV method: today's NAV - (yesterday * (1 + price-derived return))
        ret = 5.0 / 4.9 - 1
        expected = 49_000_000_000.0 * (1 + ret)
        assert flow['flow_nav_tl'] == pytest.approx(52_500_000_000.0 - expected)
        # M1 and M2 should match when derived from same price data
        assert flow['flow_nav_tl'] == pytest.approx(flow['flow_shares_tl'])
        assert flow['investor_change'] == 300

    def test_outflow(self):
        today = {
            'date': '2026-06-11',
            'fund_code': 'ZBJ',
            'son_fiyat': 5.0,
            'pay_adet': 9_500_000_000,
        }
        yesterday = {
            'date': '2026-06-10',
            'fund_code': 'ZBJ',
            'son_fiyat': 5.0,
            'pay_adet': 10_000_000_000,
        }

        flow = calculate_flow(today, yesterday)
        assert flow['share_change'] == -500_000_000
        assert flow['direction'] == 'OUTFLOW'

    def test_missing_data_graceful(self):
        today = {'date': '2026-06-11', 'fund_code': 'ZBJ'}
        yesterday = {'date': '2026-06-10', 'fund_code': 'ZBJ'}

        flow = calculate_flow(today, yesterday)
        assert 'share_change' not in flow
        assert 'flow_nav_tl' not in flow
        assert 'investor_change' not in flow

    def test_partial_data(self):
        today = {
            'date': '2026-06-11',
            'fund_code': 'ZBJ',
            'son_fiyat': 5.0,
            'pay_adet': 10_500_000_000,
        }
        yesterday = {
            'date': '2026-06-10',
            'fund_code': 'ZBJ',
            'son_fiyat': 5.0,
            'pay_adet': 10_000_000_000,
        }

        flow = calculate_flow(today, yesterday)
        assert flow['share_change'] == 500_000_000
        assert 'flow_nav_tl' not in flow
        assert 'investor_change' not in flow


class TestFetchWithRetry:
    def test_success_first_try(self, mocker):
        mock_session = mocker.Mock()
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_session.get.return_value = mock_resp

        result = fetch_with_retry(mock_session, "http://example.com", max_retries=3)
        assert result == mock_resp
        assert mock_session.get.call_count == 1

    def test_retry_then_success(self, mocker):
        mock_session = mocker.Mock()
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None

        # Fail twice, succeed on third
        from requests.exceptions import ConnectionError
        mock_session.get.side_effect = [
            ConnectionError("fail"),
            ConnectionError("fail"),
            mock_resp,
        ]

        result = fetch_with_retry(mock_session, "http://example.com", max_retries=3, backoff=0.01)
        assert result == mock_resp
        assert mock_session.get.call_count == 3

    def test_all_retries_fail(self, mocker):
        mock_session = mocker.Mock()
        from requests.exceptions import ConnectionError
        mock_session.get.side_effect = ConnectionError("always fail")

        with pytest.raises(ConnectionError):
            fetch_with_retry(mock_session, "http://example.com", max_retries=2, backoff=0.01)
        assert mock_session.get.call_count == 2
