import json
import re
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, List, Tuple

import streamlit as st


# -----------------------------
# HTTP (stdlib only)
# -----------------------------
def http_get(url: str, timeout: int = 25) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nasdaq.com/",
        "Connection": "close",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def http_get_json(url: str, timeout: int = 25) -> Dict[str, Any]:
    return json.loads(http_get(url, timeout=timeout))


# -----------------------------
# Parsing helpers
# -----------------------------
def normalize_option_code(option_code_or_slug_or_url: str) -> str:
    s = option_code_or_slug_or_url.strip()
    if "://" in s:
        s = s.split("?", 1)[0].rstrip("/")
        s = s.rsplit("/", 1)[-1]
    if "---" in s:
        s = s.split("---", 1)[-1]
    return s.lower()


def parse_symbol_from_nasdaq_url(url: str) -> Optional[str]:
    m = re.search(r"/market-activity/stocks/([a-z0-9\.-]+)/option-chain/", url.lower())
    return m.group(1) if m else None


def iter_option_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    data = payload.get("data") or {}
    table = data.get("table") or {}
    rows = table.get("rows") or []
    return rows if isinstance(rows, list) else []


def extract_expirations(payload: Dict[str, Any]) -> List[str]:
    """
    Nasdaq sometimes includes available expirations list in:
      payload["data"]["expirations"]  OR other shapes.
    We try a few common places.
    """
    data = payload.get("data") or {}

    # common candidates
    candidates = []
    for key in ("expirations", "expirationDates", "dates", "expirationDateList"):
        v = data.get(key)
        if isinstance(v, list):
            candidates.extend([str(x) for x in v if x])
        elif isinstance(v, dict):
            # sometimes { "rows": [...] } etc.
            rows = v.get("rows")
            if isinstance(rows, list):
                candidates.extend([str(x) for x in rows if x])

    # de-dup, keep order
    out = []
    seen = set()
    for x in candidates:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def match_contract_dict(d: Any, option_code: str) -> bool:
    if not isinstance(d, dict):
        return False
    option_code = option_code.lower()

    for k in ("optionSymbol", "symbol", "contractSymbol", "displaySymbol"):
        v = d.get(k)
        if isinstance(v, str) and option_code in v.lower():
            return True

    for v in d.values():
        if isinstance(v, str) and option_code in v.lower():
            return True

    return False


def normalize_quote_fields(contract: Dict[str, Any]) -> Dict[str, Any]:
    key_map = {
        "optionSymbol": "option_symbol",
        "symbol": "option_symbol",
        "contractSymbol": "option_symbol",
        "displaySymbol": "option_symbol",
        "bid": "bid",
        "ask": "ask",
        "last": "last",
        "lastPrice": "last",
        "lastSalePrice": "last",
        "volume": "volume",
        "openInterest": "open_interest",
        "impliedVolatility": "iv",
        "strikePrice": "strike",
        "expirationDate": "expiration",
        "expiryDate": "expiration",
    }
    out: Dict[str, Any] = {}
    for src, dst in key_map.items():
        if src in contract and contract[src] not in (None, ""):
            out.setdefault(dst, contract[src])
    out["_raw"] = contract
    return out


# -----------------------------
# Nasdaq option-chain fetching
# -----------------------------
def build_option_chain_url(symbol: str, params: Dict[str, str]) -> str:
    base = f"https://api.nasdaq.com/api/quote/{symbol}/option-chain"
    q = {"assetclass": "stocks"}
    q.update({k: v for k, v in params.items() if v})
    return base + "?" + urllib.parse.urlencode(q)


def fetch_chain(symbol: str, params: Dict[str, str]) -> Dict[str, Any]:
    url = build_option_chain_url(symbol, params)
    return http_get_json(url)


def find_contract_in_payload(payload: Dict[str, Any], option_code: str) -> Optional[Dict[str, Any]]:
    for row in iter_option_rows(payload):
        call = row.get("call")
        if match_contract_dict(call, option_code):
            out = normalize_quote_fields(call)
            out["type"] = "call"
            return out

        put = row.get("put")
        if match_contract_dict(put, option_code):
            out = normalize_quote_fields(put)
            out["type"] = "put"
            return out

        if match_contract_dict(row, option_code):
            out = normalize_quote_fields(row)
            out["type"] = "unknown"
            return out

    return None


def get_option_quote_by_code(symbol: str, option_code: str, log_fn=None) -> Dict[str, Any]:
    """
    1) Fetch default chain
    2) If not found, try iterating expirations (if present) with multiple possible param names.
    """
    symbol = symbol.lower().strip()
    option_code = normalize_option_code(option_code)

    # Step 1: default request
    payload = fetch_chain(symbol, params={})
    found = find_contract_in_payload(payload, option_code)
    if found:
        found.update({"underlying": symbol, "option_code": option_code, "source": "api.nasdaq.com"})
        return found

    expirations = extract_expirations(payload)
    if log_fn:
        log_fn(f"Not found in default chain. expirations discovered: {len(expirations)}")

    if not expirations:
        raise RuntimeError(
            "Contract not found in default option-chain response, and no expirations list available. "
            "Nasdaq may have changed the payload shape."
        )

    # Nasdaq parameter name can vary; try a small set
    param_names = ["expirationdate", "expirationDate", "expiryDate", "date", "expiration"]
    tried = 0

    for exp in expirations:
        for pname in param_names:
            tried += 1
            if log_fn and tried % 10 == 0:
                log_fn(f"Trying expirations… attempts: {tried}")

            try:
                payload2 = fetch_chain(symbol, params={pname: exp})
            except Exception:
                continue

            found2 = find_contract_in_payload(payload2, option_code)
            if found2:
                found2.update({
                    "underlying": symbol,
                    "option_code": option_code,
                    "source": "api.nasdaq.com",
                    "queried_expiration": exp,
                    "queried_param": pname,
                })
                return found2

    raise RuntimeError(f"Contract not found after checking {len(expirations)} expirations.")


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="Nasdaq Option Ask by Code", layout="wide")
st.title("Nasdaq: получить ASK по коду опциона")
st.caption("Источник: api.nasdaq.com (тот же JSON, который часто использует сайт Nasdaq).")

url_or_symbol = st.text_input(
    "Nasdaq URL (рекомендуется) ИЛИ symbol (например, amd)",
    value="https://www.nasdaq.com/market-activity/stocks/amd/option-chain/call-put-options/amd---271217c00370000",
)
option_code_input = st.text_input(
    "Option code (если не URL). Пример: 271217c00370000",
    value="",
)

run = st.button("Fetch", type="primary")

log_box = st.empty()

def log(msg: str):
    st.session_state.setdefault("log", [])
    st.session_state["log"].append(msg)
    log_box.code("\n".join(st.session_state["log"][-20:]))

if run:
    st.session_state["log"] = []

    # Determine symbol + code
    if "://" in url_or_symbol:
        symbol = parse_symbol_from_nasdaq_url(url_or_symbol)
        if not symbol:
            st.error("Не смог извлечь symbol из URL. Введи symbol вручную.")
            st.stop()
        option_code = normalize_option_code(url_or_symbol)
    else:
        symbol = url_or_symbol.strip().lower()
        if not symbol:
            st.error("Введи symbol или URL.")
            st.stop()
        if not option_code_input.strip():
            st.error("Если вводишь symbol, нужно указать option_code.")
            st.stop()
        option_code = normalize_option_code(option_code_input)

    st.info(f"Symbol: {symbol} | option_code: {option_code}")

    try:
        quote = get_option_quote_by_code(symbol, option_code, log_fn=log)
    except Exception as e:
        st.error(str(e))
        st.stop()

    # Show main fields
    st.subheader("Result")
    cols = st.columns(4)
    cols[0].metric("ASK", str(quote.get("ask", "")))
    cols[1].metric("BID", str(quote.get("bid", "")))
    cols[2].metric("LAST", str(quote.get("last", "")))
    cols[3].metric("TYPE", str(quote.get("type", "")))

    st.write({
        "underlying": quote.get("underlying"),
        "option_code": quote.get("option_code"),
        "option_symbol": quote.get("option_symbol"),
        "queried_expiration": quote.get("queried_expiration", ""),
        "queried_param": quote.get("queried_param", ""),
        "volume": quote.get("volume", ""),
        "open_interest": quote.get("open_interest", ""),
        "iv": quote.get("iv", ""),
        "strike": quote.get("strike", ""),
        "expiration": quote.get("expiration", ""),
        "source": quote.get("source", ""),
    })

    # Download raw
    raw_json = json.dumps(quote, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "Download raw JSON",
        data=raw_json,
        file_name=f"{symbol}_{option_code}_quote.json",
        mime="application/json",
        use_container_width=True,
    )

    with st.expander("Raw node"):
        st.json(quote.get("_raw", {}))
