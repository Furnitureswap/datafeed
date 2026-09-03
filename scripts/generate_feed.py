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

ENRICH_WORKERS = 10  # concurrent storefront requests -- keep modest, this hits your live store
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
    items, seen_ids, page = [], set(), 1
    max_pages = 100  # safety cap so a pagination quirk can't loop forever
    while page <= max_pages:
        resp = requests.get(
            f"{ADMIN_API_BASE}/categories",
            headers=admin_headers(access_token),
            params={"page": page, "per_page": 200},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        batch = payload.get("categories", [])
        page_context = payload.get("page_context", {})

        batch_ids = [c.get("category_id") for c in batch]
        new_ids = [cid for cid in batch_ids if cid not in seen_ids]
        duplicate_ratio = 1 - (len(new_ids) / len(batch_ids)) if batch_ids else 0

        print(
            f"  category page {page}: got {len(batch)} items ({len(new_ids)} new), "
            f"raw page_context={page_context}"
        )

        if not batch:
            break
        if duplicate_ratio > 0.5:
            print(f"STOPPING: category page {page} was {duplicate_ratio:.0%} duplicates -- pagination looping")
            break

        for c, cid in zip(batch, batch_ids):
            if cid not in seen_ids:
                seen_ids.add(cid)
                items.append(c)

        if not page_context.get("has_more_page"):
            break
        page += 1
        time.sleep(0.2)
    else:
        print(f"WARNING: stopped after the {max_pages}-page safety cap for categories")
    return items


def fetch_all_products(access_token):
    """
    Zoho's exact pagination semantics for `page_start_from` turned out not to
    match either guess tried so far (plain page number, or record offset
    stepped by per_page) -- both produced runaway/duplicate results against
    the real API. Rather than guess a third time, this version trusts
    nothing: it prints the raw page_context Zoho actually returns (so the
    real shape is visible in the log), and -- most importantly -- stops the
    moment a page comes back mostly full of product_ids already seen, since
    that's unambiguous proof of looping over the same data, regardless of
    what has_more_page claims.
    """
    per_page = 200
    items, seen_ids, offset, page_num = [], set(), 1, 1
    max_pages = 100
    hard_item_cap = 5000  # well above any plausible real catalog size here
    while page_num <= max_pages and len(items) < hard_item_cap:
        resp = requests.get(
            f"{ADMIN_API_BASE}/products",
            headers=admin_headers(access_token),
            params={
                "filter_by": "Status.Active",
                "page_start_from": offset,
                "per_page": per_page,
            },
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        batch = payload.get("products", [])
        page_context = payload.get("page_context", {})

        batch_ids = [p.get("product_id") for p in batch]
        new_ids = [pid for pid in batch_ids if pid not in seen_ids]
        duplicate_ratio = 1 - (len(new_ids) / len(batch_ids)) if batch_ids else 0

        print(
            f"  product page {page_num}: requested page_start_from={offset}, got {len(batch)} items "
            f"({len(new_ids)} new, {len(batch) - len(new_ids)} duplicate), raw page_context={page_context}"
        )

        if not batch:
            break

        if duplicate_ratio > 0.5:
            print(
                f"STOPPING: page {page_num} was {duplicate_ratio:.0%} duplicates of already-fetched products -- "
                f"this confirms page_start_from={offset} is not advancing correctly. Send Claude this log "
                f"(the 'raw page_context' lines above) so the real pagination parameter can be figured out."
            )
            break

        for p, pid in zip(batch, batch_ids):
            if pid not in seen_ids:
                seen_ids.add(pid)
                items.append(p)

        if not page_context.get("has_more_page"):
            break
        offset += per_page
        page_num += 1
        time.sleep(0.2)
    else:
        print(f"WARNING: stopped after hitting a safety cap (page {page_num}, {len(items)} items)")

    return _filter_online_only(items)


def _is_online(product):
    """
    'Status.Active' (filtered above) is Zoho's inventory status, not the same
    as the storefront "Show in Store: Online" flag you saw in the product
    list -- a product can be Active but still Draft/hidden from customers.
    Zoho's docs name this field `show_in_storefront`; check a couple of
    plausible spellings/value formats defensively so a naming mismatch
    doesn't silently keep this filter from working.
    """
    for key in ("show_in_storefront", "show_in_store", "is_online", "storefront_visibility"):
        if key in product:
            val = product.get(key)
            return val in (True, "true", "True", 1, "1", "Online", "online")
    return None  # field not found at all


def _filter_online_only(products):
    flagged = [_is_online(p) for p in products]
    if all(v is None for v in flagged):
        # None of the expected fields showed up in the response at all --
        # rather than silently returning zero products (or silently
        # including hidden ones), fall back to including everything and
        # print a loud warning so this doesn't go unnoticed.
        print(
            "WARNING: could not find an online/storefront-visibility field on "
            "products from the API -- shipping ALL Status.Active products "
            "(including any not shown in your store). Open one raw product "
            "from the API response and tell Claude its exact field names so "
            "this filter can be fixed."
        )
        return products
    online = [p for p, flag in zip(products, flagged) if flag]
    print(f"Filtered {len(products)} active products down to {len(online)} shown in store")
    return online


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


_failure_count = 0
_failure_examples_printed = 0


def _log_failure(kind, item_id, reason):
    global _failure_count, _failure_examples_printed
    _failure_count += 1
    if _failure_examples_printed < 5:
        print(f"  [storefront {kind} FAILED] id={item_id} domain-name={STOREFRONT_DOMAIN!r} -> {reason}")
        _failure_examples_printed += 1


def verify_storefront_access(sample_category_id=None, sample_product_id=None):
    """
    One quick sanity check before doing 1000+ requests. If this fails, every
    subsequent enrichment call would fail too (usually a wrong STOREFRONT_DOMAIN)
    -- better to stop immediately with a clear message than burn ~20-30 minutes
    silently timing out on every single product/category.
    """
    test_id = sample_category_id or sample_product_id
    kind = "categories" if sample_category_id else "products"
    try:
        resp = requests.get(
            f"{STOREFRONT_API_BASE}/{kind}/{test_id}",
            headers=storefront_headers(),
            timeout=10,
        )
    except requests.RequestException as e:
        raise SystemExit(
            f"Storefront API is unreachable using domain-name={STOREFRONT_DOMAIN!r}: {e}\n"
            f"Set the STOREFRONT_DOMAIN secret to your store's actual published domain "
            f"(check Settings -> Online Store -> Domain and SSL in Zoho Commerce)."
        )
    if not resp.ok:
        raise SystemExit(
            f"Storefront API test call failed (HTTP {resp.status_code}) using "
            f"domain-name={STOREFRONT_DOMAIN!r}. Response: {resp.text[:300]}\n"
            f"This almost always means STOREFRONT_DOMAIN doesn't match what Zoho has "
            f"registered as your store's live domain -- check Settings -> Online Store -> "
            f"Domain and SSL in Zoho Commerce and set the STOREFRONT_DOMAIN secret to match "
            f"exactly (no https://, no trailing slash)."
        )
    print(f"Storefront access check passed (domain-name={STOREFRONT_DOMAIN!r})")


def enrich_product(product_id):
    """Fetch real image URL + page URL for one product from the public storefront API."""
    try:
        resp = requests.get(
            f"{STOREFRONT_API_BASE}/products/{product_id}",
            headers=storefront_headers(),
            timeout=10,
        )
        if not resp.ok:
            _log_failure("product", product_id, f"HTTP {resp.status_code}")
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
    except requests.RequestException as e:
        _log_failure("product", product_id, str(e))
        return product_id, {"image": "", "url": ""}


def enrich_category(category_id):
    """Fetch real image URL + page URL for one category from the public storefront API."""
    try:
        resp = requests.get(
            f"{STOREFRONT_API_BASE}/categories/{category_id}",
            headers=storefront_headers(),
            timeout=10,
        )
        if not resp.ok:
            _log_failure("category", category_id, f"HTTP {resp.status_code}")
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
    except requests.RequestException as e:
        _log_failure("category", category_id, str(e))
        return category_id, {"image": "", "url": ""}


def enrich_all(ids, fn, label="items"):
    ids = list(ids)
    total = len(ids)
    results = {}
    done = 0
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        futures = [pool.submit(fn, i) for i in ids]
        for f in as_completed(futures):
            item_id, data = f.result()
            results[item_id] = data
            done += 1
            if done % 50 == 0 or done == total:
                print(f"  enriched {done}/{total} {label}")
    return results


# ----------------------- Building output JSON -----------------------

def is_root(category):
    pid = category.get("parent_category_id")
    return pid in (None, "", "0", 0, "-1", -1)


def _is_category_online(category):
    """Same 'Show in Online Store: Online/Offline' flag you saw in the admin
    Categories list. Field name is a best guess (Zoho's docs call it
    `visibility`) -- checked defensively alongside other plausible names."""
    for key in ("visibility", "show_in_storefront", "show_in_store", "is_online"):
        if key in category:
            val = category.get(key)
            return val in (True, "true", "True", 1, "1", "Online", "online", "visible", "Visible", "shown", "Shown")
    return None


def build_categories_json(categories, enrichment):
    online_flags = [_is_category_online(c) for c in categories]
    if all(v is None for v in online_flags):
        print(
            "WARNING: could not find an online/offline visibility field on "
            "categories from the API -- including ALL categories (even ones "
            "marked Offline in your admin). Open one raw category from the "
            "API response and tell Claude its exact field names so this "
            "filter can be fixed."
        )
        online_by_id = {c["category_id"]: True for c in categories}
    else:
        online_by_id = {c["category_id"]: (flag is not False) for c, flag in zip(categories, online_flags)}

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

    skipped_offline = 0
    for c in categories:
        if is_root(c):
            continue
        if not online_by_id.get(c["category_id"], True):
            skipped_offline += 1
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

    if skipped_offline:
        print(f"Skipped {skipped_offline} offline categories")

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
    print("Got Zoho access token")

    categories = fetch_all_categories(access_token)
    products = fetch_all_products(access_token)
    print(f"Fetched {len(categories)} categories, {len(products)} online products.")

    # Fail fast and loudly if STOREFRONT_DOMAIN is wrong, instead of silently
    # timing out on every single one of 1000+ items (which is what happened
    # before this check existed -- looked "stuck" for ~35 minutes).
    if categories:
        verify_storefront_access(sample_category_id=categories[0]["category_id"])
    elif products:
        verify_storefront_access(sample_product_id=products[0]["product_id"])

    print("Enriching with real image/page URLs (this is the slow part)...")
    category_enrichment = enrich_all([c["category_id"] for c in categories], enrich_category, label="categories")
    product_enrichment = enrich_all([p["product_id"] for p in products], enrich_product, label="products")
    if _failure_count:
        print(f"NOTE: {_failure_count} storefront enrichment calls failed (image/url left blank for those items)")

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
