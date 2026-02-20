# appstore_streamlit_reviews.py
# --------------------------------------------------------
# Streamlit app to fetch Apple App Store (US) customer reviews and download CSV.
# Scraping/parsing uses ONLY Python standard library.
#
# Public endpoint (US storefront):
#   https://itunes.apple.com/us/rss/customerreviews/page={PAGE}/id={APP_ID}/sortby=mostrecent/xml
# Pagination: page is 1-based.

import re
import csv
import io
import urllib.request
import urllib.error
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

import streamlit as st

STOREFRONT = "us"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "im": "http://itunes.apple.com/rss",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "thr": "http://purl.org/syndication/thread/1.0",
    "gd": "http://schemas.google.com/g/2005",
}


def extract_app_id(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL is empty")
    m = re.search(r"id(\d+)", url)
    if not m:
        raise ValueError("Could not extract app_id: expected 'id123456789' in the URL")
    return m.group(1)


def http_get(url: str, timeout: int = 20) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; StreamlitApp/1.0)",
        "Accept": "application/xml,text/xml,application/atom+xml,*/*",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def fetch_feed(app_id: str, page: int) -> str:
    feed_url = (
        f"https://itunes.apple.com/{STOREFRONT}/rss/customerreviews/"
        f"page={page}/id={app_id}/sortby=mostrecent/xml"
    )
    return http_get(feed_url)


def find_text(elem, path, default=""):
    found = elem.find(path, NS)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def parse_reviews(xml_text: str, app_id: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    rows = []

    for entry in root.findall("atom:entry", NS):
        rating = find_text(entry, "im:rating", "")
        body = find_text(entry, "atom:content", "")

        # First entry is often app metadata; skip non-review entries
        if not rating and not body:
            continue

        row = {
            "app_id": app_id,
            "storefront": STOREFRONT,
            "review_id": find_text(entry, "atom:id", ""),
            "author_name": find_text(entry, "atom:author/atom:name", ""),
            "author_uri": find_text(entry, "atom:author/atom:uri", ""),
            "title": find_text(entry, "atom:title", ""),
            "body": body,
            "rating": rating,
            "version": find_text(entry, "im:version", ""),
            "updated_at": find_text(entry, "atom:updated", ""),
            "vote_sum": find_text(entry, "im:voteSum", ""),
            "vote_count": find_text(entry, "im:voteCount", ""),
            # Device type is typically NOT present in this public feed
            "device_type": "",
        }

        rows.append(row)

    return rows


def collect_reviews(app_id: str, max_reviews: int, log_fn=None) -> list[dict]:
    collected = []
    page = 1

    while len(collected) < max_reviews:
        try:
            xml_text = fetch_feed(app_id, page)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(f"Failed to fetch page {page}: {e}")

        if log_fn:
            log_fn(f"page {page} fetched")

        rows = parse_reviews(xml_text, app_id)

        if not rows:
            if log_fn:
                log_fn("No more reviews in feed; stopping.")
            break

        for r in rows:
            if len(collected) >= max_reviews:
                break
            collected.append(r)

        if log_fn:
            log_fn(f"{len(collected)} reviews collected")

        page += 1
        if page > 50:  # safety guard
            if log_fn:
                log_fn("Reached page limit guard (50); stopping.")
            break

    return collected


def rows_to_csv_bytes(rows: list[dict]) -> tuple[bytes, list[str]]:
    # stable columns first
    core = [
        "app_id", "storefront", "review_id",
        "author_name", "author_uri",
        "title", "body", "rating", "version",
        "updated_at", "vote_sum", "vote_count", "device_type",
    ]
    # union for any extra keys (future-proof)
    all_keys = list(core)
    seen = set(all_keys)

    extras = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                extras.add(k)
    all_keys.extend(sorted(extras))

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=all_keys, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in all_keys})

    return buf.getvalue().encode("utf-8"), all_keys


# ---------------------------
# Streamlit UI
# ---------------------------
st.set_page_config(page_title="App Store Reviews → CSV", layout="wide")
st.title("Apple App Store Reviews (US) → CSV")
st.caption("Public Apple Customer Reviews RSS/Atom feed. No keys, no login.")

app_url = st.text_input(
    "Apple App Store URL",
    placeholder="https://apps.apple.com/us/app/some-app/id123456789",
)
max_reviews = st.number_input("MAX_REVIEWS", min_value=1, max_value=500, value=100, step=10)

log_area = st.empty()

def log(msg: str):
    st.session_state.setdefault("log", [])
    st.session_state["log"].append(msg)
    log_area.code("\n".join(st.session_state["log"][-20:]))

col1, col2 = st.columns([1, 3])
with col1:
    run = st.button("Fetch reviews", type="primary")

if run:
    st.session_state["log"] = []

    try:
        app_id = extract_app_id(app_url)
    except Exception as e:
        st.error(str(e))
        st.stop()

    st.info(f"App ID: {app_id} | Storefront: {STOREFRONT} | Target: {int(max_reviews)}")

    try:
        rows = collect_reviews(app_id, int(max_reviews), log_fn=log)
    except Exception as e:
        st.error(str(e))
        st.stop()

    if not rows:
        st.warning("No reviews collected.")
        st.stop()

    csv_bytes, cols = rows_to_csv_bytes(rows)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_UTC")
    filename = f"appstore_reviews_{app_id}_{ts}.csv"

    st.success(f"Collected {len(rows)} reviews.")

    st.subheader("Preview (first 5)")
    preview_cols = ["review_id", "author_name", "rating", "version", "updated_at", "title"]
    st.table([{k: r.get(k, "") for k in preview_cols} for r in rows[:5]])

    st.download_button(
        label="Download CSV",
        data=csv_bytes,
        file_name=filename,
        mime="text/csv",
        use_container_width=True,
    )

    with st.expander("All columns"):
        st.write(cols)
else:
    st.write("Введите ссылку на приложение и нажмите **Fetch reviews**.")
