import json
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

# -----------------------------
# Helpers: parse inputs
# -----------------------------
def normalize_option_code(code_or_slug_or_url: str) -> str:
    s = code_or_slug_or_url.strip()
    if "://" in s:
        s = s.split("?", 1)[0].rstrip("/")
        s = s.rsplit("/", 1)[-1]
    if "---" in s:
        s = s.split("---", 1)[-1]
    return s.lower()

def parse_symbol_from_nasdaq_url(url: str) -> Optional[str]:
    m = re.search(r"/market-activity/stocks/([a-z0-9\.-]+)/option-chain/", url.lower())
    return m.group(1) if m else None

def parse_code_parts(option_code: str) -> Tuple[int, int, int, str, float, str]:
    """
    option_code like: 271217c00370000
      YYMMDD  = 271217 -> 2027-12-17
      CP      = c / p
      STRIKE8 = 00370000 -> 370.000 (8 digits, thousandths)
    Returns: (year, month, day, cp, strike, strike8)
    """
    code = normalize_option_code(option_code)

    m = re.fullmatch(r"(\d{6})([cp])(\d{8})", code)
    if not m:
        raise ValueError("Bad option_code format. Expected like 271217c00370000")

    yymmdd, cp, strike8 = m.groups()
    yy = int(yymmdd[0:2])
    mm = int(yymmdd[2:4])
    dd = int(yymmdd[4:6])

    year = 2000 + yy  # OCC uses 2-digit year; assume 20xx
    strike = int(strike8) / 1000.0

    return year, mm, dd, cp.upper(), strike, strike8

def build_occ_contract_symbol(underlying: str, option_code: str) -> str:
    """
    OCC contract symbol like: AMD271217C00370000
    """
    underlying = underlying.upper().strip()
    year, mm, dd, cp, strike, strike8 = parse_code_parts(option_code)
    yymmdd = f"{year % 100:02d}{mm:02d}{dd:02d}"
    return f"{underlying}{yymmdd}{cp}{strike8}"

def expiration_to_unix_utc(year: int, month: int, day: int) -> int:
    """
    Yahoo option-chain expects 'date' parameter as unix timestamp (seconds) for expiration date.
    We'll use 00:00:00 UTC of that day; Yahoo typically matches by date.
    """
    dt = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
    return int(dt.timestamp())

# -----------------------------
# HTTP (stdlib only)
# -----------------------------
def http_get_json(url: str, timeout: int = 25) -> Dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PythonStdlib/1.0)",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    txt = raw.decode("utf-8", errors="replace")
    return json.loads(txt)

# -----------------------------
# Yahoo option chain lookup
# -----------------------------
def fetch_yahoo_option_chain(underlying: str, expiration_unix: int) -> Dict[str, Any]:
    """
    Endpoint:
      https://query2.finance.yahoo.com/v7/finance/options/{symbol}?date={unix}
    """
    sym = underlying.upper().strip()
    url = f"https://query2.finance.yahoo.com/v7/finance/options/{urllib.parse.quote(sym)}?date={expiration_unix}"
    return http_get_json(url)

def find_contract_in_yahoo(payload: Dict[str, Any], contract_symbol: str, cp: str) -> Optional[Dict[str, Any]]:
    """
    Searches calls/puts for exact contractSymbol match.
    """
    try:
        result = payload["optionChain"]["result"]
        if not result:
            return None
        opt = result[0].get("options", [])
        if not opt:
            return None
        chain = opt[0]
        side = "calls" if cp == "C" else "puts"
        items = chain.get(side, []) or []
        for it in items:
            if str(it.get("contractSymbol", "")).upper() == contract_symbol.upper():
                return it
        return None
    except Exception:
        return None

def get_option_quote_yahoo(underlying: str, option_code: str) -> Dict[str, Any]:
    """
    Returns normalized quote fields for the given option_code using Yahoo.
    """
    year, mm, dd, cp, strike, strike8 = parse_code_parts(option_code)
    expiration_unix = expiration_to_unix_utc(year, mm, dd)
    contract_symbol = build_occ_contract_symbol(underlying, option_code)

    payload = fetch_yahoo_option_chain(underlying, expiration_unix)
    contract = find_contract_in_yahoo(payload, contract_symbol, cp)

    if not contract:
        # Helpful debug hints
        available = []
        try:
            opt = payload["optionChain"]["result"][0]["options"][0]
            available = [x.get("contractSymbol") for x in opt.get("calls" if cp == "C" else "puts", [])[:10]]
        except Exception:
            pass
        raise RuntimeError(
            f"Contract not found on Yahoo for expiration {year}-{mm:02d}-{dd:02d}. "
            f"Looking for contractSymbol={contract_symbol}. "
            f"Sample symbols from that chain: {available}"
        )

    # Normalize output
    out = {
        "underlying": underlying.upper(),
        "option_code": normalize_option_code(option_code),
        "contractSymbol": contract.get("contractSymbol"),
        "type": "call" if cp == "C" else "put",
        "expiration": f"{year}-{mm:02d}-{dd:02d}",
        "strike": contract.get("strike", strike),
        "currency": contract.get("currency"),
        "last": contract.get("lastPrice"),
        "bid": contract.get("bid"),
        "ask": contract.get("ask"),
        "change": contract.get("change"),
        "percentChange": contract.get("percentChange"),
        "volume": contract.get("volume"),
        "openInterest": contract.get("openInterest"),
        "impliedVolatility": contract.get("impliedVolatility"),
        "inTheMoney": contract.get("inTheMoney"),
        "contractSize": contract.get("contractSize"),
        "lastTradeDate": contract.get("lastTradeDate"),  # unix
        "_raw": contract,  # keep everything
    }
    return out

# -----------------------------
# Convenience: accept Nasdaq URL
# -----------------------------
def get_ask(underlying_or_url: str, option_code: Optional[str] = None) -> Any:
    """
    - get_ask("AMD", "271217c00370000")
    - get_ask("https://www.nasdaq.com/.../amd---271217c00370000")
    """
    if option_code is None:
        url = underlying_or_url
        sym = parse_symbol_from_nasdaq_url(url)
        if not sym:
            raise ValueError("Could not parse symbol from URL; pass symbol explicitly.")
        code = normalize_option_code(url)
        quote = get_option_quote_yahoo(sym, code)
    else:
        quote = get_option_quote_yahoo(underlying_or_url, option_code)
    return quote.get("ask")

# -----------------------------
# Example
# -----------------------------
if __name__ == "__main__":
    nasdaq_url = "https://www.nasdaq.com/market-activity/stocks/amd/option-chain/call-put-options/amd---271217c00370000"
    sym = parse_symbol_from_nasdaq_url(nasdaq_url) or "AMD"
    code = normalize_option_code(nasdaq_url)

    q = get_option_quote_yahoo(sym, code)
    print("ASK:", q.get("ask"))
    print("BID:", q.get("bid"))
    print("LAST:", q.get("last"))
    print("OI:", q.get("openInterest"), "VOL:", q.get("volume"), "IV:", q.get("impliedVolatility"))
    print("Contract:", q.get("contractSymbol"))
