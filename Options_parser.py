import json
import re
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import streamlit as st


# =============================
# Parsing helpers
# =============================
def normalize_option_code(code_or_slug_or_url: str) -> str:
    s = (code_or_slug_or_url or "").strip()
    if "://" in s:
        s = s.split("?", 1)[0].rstrip("/")
        s = s.rsplit("/", 1)[-1]
    if "---" in s:
        s = s.split("---", 1)[-1]
    return s.lower()


def parse_symbol_from_nasdaq_url(url: str) -> Optional[str]:
    m = re.search(r"/market-activity/stocks/([a-z0-9\.-]+)/option-chain/", (url or "").lower())
    return m.group(1) if m else None


def parse_code_parts(option_code: str) -> Tuple[int, int, int, str, float, str]:
    """
    option_code: YYMMDD + [c/p] + 8 digits strike in 1/1000 dollars
    Example: 271217c00370000
      - date: 2027-12-17
      - type: C
      - strike8: 00370000 -> 370.000
    """
    code = normalize_option_code(option_code)
    m = re.fullmatch(r"(\d{6})([cp])(\d{8})", code)
    if not m:
        raise ValueError("Bad option_code format. Expected like 271217c00370000")

    yymmdd, cp, strike8 = m.groups()
    yy = int(yymmdd[0:2])
    mm = int(yymmdd[2:4])
    dd = int(yymmdd[4:6])
    year = 2000 + yy
    strike = int(strike8) / 1000.0
    return year, mm, dd, cp.upper(), strike, strike8


def build_occ_contract_symbol(underlying: str, option_code: str) -> str:
    """
    Yahoo uses OCC-like contractSymbol, e.g. AMD271217C00370000
    """
    underlying = underlying.upper().strip()
    year, mm, dd, cp, _, strike8 = parse_code_parts(option_code)
    yymmdd = f"{year % 100:02d}{mm:02d}{dd:02d}"
    return f"{underlying}{yymmdd}{cp}{strike8}"


def expiration_to_unix_utc(year: int, month: int, day: int) -> int:
    # Yahoo expects unix seconds for the expiration date
    dt = datetime(year, month, day, 0, 0, 0, tzinfo=timezone.utc)
    return int(dt.timestamp())


# =============================
# HTTP (stdlib only)
# =============================
def http_get_json(url: str, timeout: int = 25) -> Dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; StreamlitApp/1.0)",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "close",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    txt = raw.decode("utf-8", errors="replace")
    return json.loads(txt)


# =============================
# Yahoo option chain
# =============================
def fetch_yahoo_option_chain(underlying: str, expiration_unix: int) -> Dict[str, Any]:
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
    year, mm, dd, cp, strike, strike8 = parse_code_parts(option_code)
    expiration_unix = expiration_to_unix_utc(year, mm, dd)
    contract_symbol = build_occ_contract_symbol(underlying, option_code)

    payload = fetch_yahoo_option_chain(underlying, expiration_unix)
    contract = find_contract_in_yahoo(payload, contract_symbol, cp)

    if not contract:
        # show small sample of symbols to debug mismatches
        sample = []
        try:
            opt = payload["optionChain"]["result"][0]["options"][0]
            sample = [x.get("contractSymbol") for x in (opt.get("calls" if cp == "C" else "puts", [])[:12])]
        except Exception:
            pass
        raise RuntimeError(
            f"Contract not found on Yahoo for expiration {year}-{mm:02d}-{dd:02d}. "
            f"Looking for contractSymbol={contract_symbol}. "
            f"Sample from that chain: {sample}"
        )

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
        "lastTradeDate": contract.get("lastTradeDate"),  # unix seconds
        "_raw": contract,
        "_source": "query2.finance.yahoo.com/v7/finance/options",
    }
    return out


# =============================
# Streamlit UI
# =============================
st.set_page_config(page_title="Option ASK by Code", layout="wide")
st.title("Option ASK по коду опциона (стабильно через Yahoo)")
st.caption("Вводишь Nasdaq URL или symbol+код. Получаешь bid/ask/last/OI/IV без ключей и без pip install.")

colA, colB = st.columns(2)

with colA:
    nasdaq_url = st.text_input(
        "Nasdaq URL (опционально)",
        value="https://www.nasdaq.com/market-activity/stocks/amd/option-chain/call-put-options/amd---271217c00370000",
    )
with colB:
    symbol_manual = st.text_input("Symbol (если URL пустой или не распарсился)", value="")

option_code_manual = st.text_input("Option code (если не URL). Пример: 271217c00370000", value="")

run = st.button("Fetch", type="primary")

if run:
    # decide symbol + code
    if nasdaq_url.strip():
        symbol = parse_symbol_from_nasdaq_url(nasdaq_url) or symbol_manual.strip()
        code = normalize_option_code(nasdaq_url)
    else:
        symbol = symbol_manual.strip()
        code = normalize_option_code(option_code_manual)

    if not symbol:
        st.error("Не удалось определить symbol. Введи его вручную (например AMD).")
        st.stop()

    if not code:
        st.error("Не удалось определить option code. Введи код вручную (например 271217c00370000).")
        st.stop()

    st.info(f"Symbol: {symbol.upper()} | code: {code}")

    try:
        quote = get_option_quote_yahoo(symbol, code)
    except Exception as e:
        st.error(str(e))
        st.stop()

    st.subheader("Результат")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("ASK", str(quote.get("ask", "")))
    c2.metric("BID", str(quote.get("bid", "")))
    c3.metric("LAST", str(quote.get("last", "")))
    c4.metric("OI", str(quote.get("openInterest", "")))

    st.write({
        "contractSymbol": quote.get("contractSymbol"),
        "type": quote.get("type"),
        "expiration": quote.get("expiration"),
        "strike": quote.get("strike"),
        "volume": quote.get("volume"),
        "impliedVolatility": quote.get("impliedVolatility"),
        "currency": quote.get("currency"),
        "source": quote.get("_source"),
    })

    raw = json.dumps(quote, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "Download raw JSON",
        data=raw,
        file_name=f"{quote.get('underlying','SYM')}_{code}_quote.json",
        mime="application/json",
        use_container_width=True,
    )

    with st.expander("Raw contract"):
        st.json(quote.get("_raw", {}))
else:
    st.write("Нажми **Fetch** чтобы получить ask/bid/last.")
