"""
Zoho Commerce -> categories.json + products.json generator
==============================================================

Produces two files in docs/, matching the schema required by your
placement widget:

  docs/categories.json  -> { "groups": [ { parent_id, parent_name, categories: [...] } ] }
  docs/products.json    -> { "products": [ { id, name, sku, image, url, category_ids, ... } ] }

Data sources:
  - Bulk lists (all products, all categories) come from the Zoho Commerce
    ADMIN API (OAuth-authenticated, paginated) -- this is the only place
    that reliably returns the *entire* catalog.
  - Per-item enrichment (real image URL, page URL) comes from the Zoho
    Commerce STOREFRONT API, which is public/unauthenticated and returns
    fields the admin bulk endpoints don't (confirmed real image URLs,
    canonical page URLs). One extra request per product/category, run
    with a small thread pool.

Read the "KNOWN LIMITATIONS" section further down before trusting the
output blindly -- a couple of fields in the target schema can't be
reliably auto-filled from Zoho and are explained there.

All Zoho credentials are read from environment variables (GitHub Secrets
in the workflow -- see the top-level README.md for setup).
"""

import os
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

# ----------------------- CONFIG -----------------------
DATA_CENTER = os.environ.get("ZOHO_DC", "eu")  # com | eu | in | com.au | jp
CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET", "")
REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN", "")
ORG_ID = os.environ.get("ZOHO_ORG_ID", "")

STORE_DOMAIN = os.environ.get("STORE_DOMAIN", "https://www.strandroadireland.com")
# The domain your storefront is actually published under. Usually the same
# host as STORE_DOMAIN, but override with STOREFRONT_DOMAIN if your store
# is published under a different domain (e.g. a *.zohostore.com address)
# than your custom domain.
STOREFRONT_DOMAIN = os.environ.get("STOREFRONT_DOMAIN") or urlparse(STORE_DOMAIN).netloc or STORE_DOMAIN

# Set to "true" to also fetch a "dimensions" value per product from Zoho
# custom fields/specifications. Off by default because it costs one extra
# ADMIN API call per product (slower, more API usage) on top of the
# storefront enrichment call every product already gets.
INCLUDE_DIMENSIONS = os.environ.get("INCLUDE_DIMENSIONS", "false").lower() == "true"
DIMENSIONS_FIELD_NAME = os.environ.get("DIMENSIONS_FIELD_NAME", "dimensions")

ACCOUNTS_BASE = f"https://accounts.zoho.{DATA_CENTER}"
ADMIN_API_BASE = f"https://commerce.zoho.{DATA_CENTER}/store/api/v1"
STOREFRONT_API_BASE = f"https://commerce.zoho.{DATA_CENTER}/storefront/api/v1"

DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
CATEGORIES_OUT = os.path.join(DOCS_DIR, "categories.json")
PRODUCTS_OUT = os.path.join(DOCS_DIR, "products.json")

ENRICH_WORKERS = 5  # concurrent storefront requests -- keep modest, this hits your live store
# --------------------------------------------------------


# ----------------------- Admin API (OAuth) -----------------------

def get_access_token():
    resp = requests.post(
        f"{ACCOUNTS_BASE}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Token refresh failed: {data}")
    return data["access_token"]


def admin_headers(access_token):
    return {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "X-com-zoho-store-organizationid": ORG_ID,
    }


def fetch_all_categories(access_token):
    items, page = [], 1
    while True:
        resp = requests.get(
            f"{ADMIN_API_BASE}/categories",
            headers=admin_headers(access_token),
            params={"page": page, "per_page": 200},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        items.extend(payload.get("categories", []))
        if not payload.get("page_context", {}).get("has_more_page"):
            break
        page += 1
        time.sleep(0.2)
    return items


def fetch_all_products(access_token):
    items, page = [], 1
    while True:
        resp = requests.get(
            f"{ADMIN_API_BASE}/products",
            headers=admin_headers(access_token),
            params={
                "filter_by": "Status.Active",
                "page_start_from": page,
                "per_page": 200,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        items.extend(payload.get("products", []))
        if not payload.get("page_context", {}).get("has_more_page"):
            break
        page += 1
        time.sleep(0.2)
    return items


def fetch_product_custom_fields(access_token, product_id):
    """Only called when INCLUDE_DIMENSIONS=true. One admin call per product."""
    resp = requests.get(
        f"{ADMIN_API_BASE}/products/editpage",
        headers=admin_headers(access_token),
        params={"product_id": product_id},
        timeout=30,
    )
    if not resp.ok:
        return ""
    data = resp.json().get("product", {}) or resp.json()
    for cf in data.get("custom_fields", []) or []:
        label = (cf.get("label") or cf.get("field_name") or "").strip().lower()
        if DIMENSIONS_FIELD_NAME.lower() in label:
            return str(cf.get("value", "") or "")
    for spec in data.get("specifications", []) or []:
        label = (spec.get("name") or "").strip().lower()
        if DIMENSIONS_FIELD_NAME.lower() in label:
            return str(spec.get("value", "") or "")
    return ""


# ----------------------- Storefront API (public) -----------------------

def storefront_headers():
    return {"domain-name": STOREFRONT_DOMAIN}


def enrich_product(product_id):
    """Fetch real image URL + page URL for one product from the public storefront API."""
    try:
        resp = requests.get(
            f"{STOREFRONT_API_BASE}/products/{product_id}",
            headers=storefront_headers(),
            timeout=20,
        )
        if not resp.ok:
            return product_id, {"image": "", "url": ""}
        data = resp.json().get("product", {}) or {}
        images = data.get("images", []) or []
        image_url = ""
        if images:
            raw = images[0].get("url", "")
            image_url = raw if raw.startswith("http") else f"https://{STOREFRONT_DOMAIN}{raw}"
        page_url = data.get("url", "")
        full_url = page_url if page_url.startswith("http") else f"https://{STOREFRONT_DOMAIN}{page_url}"
        return product_id, {"image": image_url, "url": full_url}
    except requests.RequestException:
        return product_id, {"image": "", "url": ""}


def enrich_category(category_id):
    """Fetch real image URL + page URL for one category from the public storefront API."""
    try:
        resp = requests.get(
            f"{STOREFRONT_API_BASE}/categories/{category_id}",
            headers=storefront_headers(),
            timeout=20,
        )
        if not resp.ok:
            return category_id, {"image": "", "url": ""}
        data = resp.json().get("category", {}) or {}
        images = data.get("images", []) or []
        image_url = ""
        if images:
            raw = images[0].get("url", "")
            image_url = raw if raw.startswith("http") else f"https://{STOREFRONT_DOMAIN}{raw}"
        page_url = data.get("url", "")
        full_url = page_url if page_url.startswith("http") else f"https://{STOREFRONT_DOMAIN}{page_url}"
        return category_id, {"image": image_url, "url": full_url}
    except requests.RequestException:
        return category_id, {"image": "", "url": ""}


def enrich_all(ids, fn):
    results = {}
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        futures = [pool.submit(fn, i) for i in ids]
        for f in as_completed(futures):
            item_id, data = f.result()
            results[item_id] = data
    return results


# ----------------------- Building output JSON -----------------------

def is_root(category):
    pid = category.get("parent_category_id")
    return pid in (None, "", "0", 0, "-1", -1)


def build_categories_json(categories, enrichment):
    by_id = {c["category_id"]: c for c in categories}

    def top_ancestor(cat):
        seen = set()
        cur = cat
        while not is_root(cur):
            pid = cur.get("parent_category_id")
            if pid in by_id and pid not in seen:
                seen.add(pid)
                cur = by_id[pid]
            else:
                break
        return cur

    groups = {}
    for c in categories:
        if is_root(c):
            groups.setdefault(str(c["category_id"]), {
                "parent_id": str(c["category_id"]),
                "parent_name": c.get("name", ""),
                "categories": [],
            })

    for c in categories:
        if is_root(c):
            continue
        root = top_ancestor(c)
        key = str(root["category_id"])
        group = groups.setdefault(key, {
            "parent_id": key,
            "parent_name": root.get("name", ""),
            "categories": [],
        })
        info = enrichment.get(c["category_id"], {"image": "", "url": ""})
        group["categories"].append({
            "id": str(c["category_id"]),
            "name": c.get("name", ""),
            "url": info.get("url", ""),
            "image": info.get("image", ""),
            "parent_id": str(c.get("parent_category_id", "")),
        })

    return {"groups": [g for g in groups.values() if g["categories"]]}


def build_products_json(products, enrichment):
    out = []
    for p in products:
        pid = str(p.get("product_id"))
        info = enrichment.get(p.get("product_id"), {"image": "", "url": ""})
        cat_id = p.get("category_id")

        entry = {
            "id": pid,
            "name": p.get("name", ""),
            "sku": p.get("sku", ""),
            "image": info.get("image", ""),
            "url": info.get("url", ""),
            # Zoho Commerce does not expose an "add to cart via link" endpoint --
            # cart actions go through the storefront's own JS/session flow, not a
            # plain URL. Left blank on purpose; see README "Known limitations".
            "add_to_cart_url": "",
            "dimensions": p.get("_dimensions", ""),
            # Zoho Commerce products only support ONE category each (confirmed
            # across the admin and storefront product APIs), so this is a
            # single-item array rather than a true multi-category list. See
            # README "Known limitations".
            "category_ids": [str(cat_id)] if cat_id not in (None, "", "0", 0) else [],
        }
        out.append(entry)
    return {"products": out}


def main():
    missing = [n for n, v in [
        ("ZOHO_CLIENT_ID", CLIENT_ID),
        ("ZOHO_CLIENT_SECRET", CLIENT_SECRET),
        ("ZOHO_REFRESH_TOKEN", REFRESH_TOKEN),
        ("ZOHO_ORG_ID", ORG_ID),
    ] if not v]
    if missing:
        raise SystemExit(f"Missing required secrets/env vars: {', '.join(missing)}")

    access_token = get_access_token()

    categories = fetch_all_categories(access_token)
    products = fetch_all_products(access_token)

    category_enrichment = enrich_all([c["category_id"] for c in categories], enrich_category)
    product_enrichment = enrich_all([p["product_id"] for p in products], enrich_product)

    if INCLUDE_DIMENSIONS:
        for p in products:
            p["_dimensions"] = fetch_product_custom_fields(access_token, p["product_id"])
            time.sleep(0.1)

    categories_json = build_categories_json(categories, category_enrichment)
    products_json = build_products_json(products, product_enrichment)

    os.makedirs(DOCS_DIR, exist_ok=True)

    import json
    with open(CATEGORIES_OUT, "w", encoding="utf-8") as f:
        json.dump(categories_json, f, indent=2, ensure_ascii=False)
    with open(PRODUCTS_OUT, "w", encoding="utf-8") as f:
        json.dump(products_json, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(categories_json['groups'])} category groups to {CATEGORIES_OUT}")
    print(f"Wrote {len(products_json['products'])} products to {PRODUCTS_OUT}")


if __name__ == "__main__":
    main()
