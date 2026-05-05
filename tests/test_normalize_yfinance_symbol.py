"""Tests for Yahoo Finance symbol normalization (China A-shares)."""

from tradingagents.dataflows.utils import normalize_symbol_for_yfinance


def test_bare_shanghai_six_digit_gets_ss():
    assert normalize_symbol_for_yfinance("601800") == "601800.SS"


def test_bare_shenzhen_zero_prefix_gets_sz():
    assert normalize_symbol_for_yfinance("000001") == "000001.SZ"


def test_bare_chi_next_gets_sz():
    assert normalize_symbol_for_yfinance("300750") == "300750.SZ"


def test_bare_beijing_gets_bj():
    assert normalize_symbol_for_yfinance("835185") == "835185.BJ"


def test_already_qualified_normalizes_suffix_case():
    assert normalize_symbol_for_yfinance("601800.ss") == "601800.SS"


def test_us_ticker_uppercased():
    assert normalize_symbol_for_yfinance("nvda") == "NVDA"
