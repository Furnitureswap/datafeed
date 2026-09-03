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
import re
import time
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, quote

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

ENRICH_WORKERS = 6  # concurrent storefront requests -- keep modest, this hits your live store
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
    Confirmed against the real API log: `page_start_from` is silently
    ignored by this endpoint -- Zoho's own returned page_context kept
    reporting {"page": 1, ...} no matter what value was sent, which is why
    every "page" after the first came back 100% duplicate. The categories
    endpoint's plain `page` parameter, by contrast, worked correctly on the
    first try. So products now use the same `page` parameter.

    Kept the duplicate-detection safety net as a standing guard (not just a
    one-off diagnostic) -- if Zoho's pagination misbehaves again for any
    reason, this stops immediately instead of silently collecting garbage
    or looping for an hour.
    """
    per_page = 200
    items, seen_ids, page = [], set(), 1
    max_pages = 100
    hard_item_cap = 5000  # well above any plausible real catalog size here
    while page <= max_pages and len(items) < hard_item_cap:
        resp = requests.get(
            f"{ADMIN_API_BASE}/products",
            headers=admin_headers(access_token),
            params={
                "filter_by": "Status.Active",
                "page": page,
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
            f"  product page {page}: got {len(batch)} items ({len(new_ids)} new), "
            f"raw page_context={page_context}"
        )

        if not batch:
            break

        if duplicate_ratio > 0.5:
            print(
                f"STOPPING: page {page} was {duplicate_ratio:.0%} duplicates of already-fetched products -- "
                f"pagination is misbehaving again. Send Claude this log."
            )
            break

        for p, pid in zip(batch, batch_ids):
            if pid not in seen_ids:
                seen_ids.add(pid)
                items.append(p)

        if not page_context.get("has_more_page"):
            break
        page += 1
        time.sleep(0.2)
    else:
        print(f"WARNING: stopped after hitting a safety cap (page {page}, {len(items)} items)")

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


def _get_with_retry(url, attempts=2, timeout=15):
    """One retry with a short pause -- several of the earlier failures were
    plain timeouts against a live site under concurrent load, which a
    single retry usually clears up without masking a real, persistent
    problem (which would still fail on the retry too)."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return requests.get(url, headers=storefront_headers(), timeout=timeout)
        except requests.RequestException as e:
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(1)
    raise last_exc


_dumped_product_sample = False
_dumped_category_sample = False


def _resolve_url_and_image(data, fallback_domain):
    """
    Try every plausible field/shape for the page URL and image URL, since
    `url` alone has been coming back blank for some real categories. Falls
    back through: url -> handle (built as a root-relative path) -> seo.url,
    and for images: images[0].url -> images[0].image_url -> image_url.
    Whatever is still missing after this is a genuine gap in Zoho's data
    for that item, not a guessing failure on our side.
    """
    page_url = data.get("url") or ""
    if not page_url:
        handle = data.get("handle") or ""
        if handle:
            page_url = f"/{handle}"
    if not page_url:
        page_url = (data.get("seo") or {}).get("url") or ""
    full_url = page_url if page_url.startswith("http") else (f"https://{fallback_domain}{page_url}" if page_url else "")

    images = data.get("images") or []
    image_url = ""
    if images:
        img0 = images[0] if isinstance(images[0], dict) else {}
        raw = img0.get("url") or img0.get("image_url") or ""
        image_url = raw if raw.startswith("http") else (f"https://{fallback_domain}{raw}" if raw else "")
    if not image_url:
        raw = data.get("image_url") or ""
        image_url = raw if raw.startswith("http") else (f"https://{fallback_domain}{raw}" if raw else "")
    if not image_url:
        # CONFIRMED against a real working image URL from the live site:
        # https://cdn3.zohoecommerce.com/product-images/{filename}/{document_id}/{size}?storefront_domain={domain}
        # -- filename is documents[].name, document_id is documents[].document_id.
        documents = data.get("documents") or []
        if documents:
            featured = next((d for d in documents if d.get("is_featured")), documents[0])
            doc_id = featured.get("document_id") or ""
            doc_name = featured.get("name") or ""
            if doc_id and doc_name:
                image_url = (
                    f"https://cdn3.zohoecommerce.com/product-images/"
                    f"{quote(doc_name)}/{doc_id}/600x600?storefront_domain={fallback_domain}"
                )

    return full_url, image_url


def _extract_payload(raw, wrapper_key, sample_flag_name, id_for_log):
    """
    Print the TRUE raw response the first time (unmodified -- earlier
    versions of this dump printed the result of a wrong key guess instead
    of the real payload, which is why it showed up empty), then try several
    plausible wrapper shapes -- confirmed from real responses: both
    products and categories wrap everything one level deeper under
    "payload" (e.g. payload.product.*, and likely payload.category.*)
    before falling back to treating the top-level object itself as the
    data.
    """
    global _dumped_product_sample, _dumped_category_sample
    already_dumped = _dumped_product_sample if sample_flag_name == "product" else _dumped_category_sample
    if not already_dumped:
        if sample_flag_name == "product":
            globals()["_dumped_product_sample"] = True
        else:
            globals()["_dumped_category_sample"] = True
        # Pretty-printed (one field per line) and generous limit so nothing
        # gets cut off before we can see fields like the page url/handle --
        # earlier dumps were truncated at 3000 chars, hiding whatever came
        # after "documents".
        pretty = json.dumps(raw, indent=2)
        print(f"  SAMPLE raw {sample_flag_name} response (id={id_for_log}), full length={len(pretty)} chars:")
        print(pretty[:12000])
        if len(pretty) > 12000:
            print(f"  ...(truncated, {len(pretty) - 12000} more chars)")

    # Confirmed real shape: raw -> payload -> product/category
    payload = raw.get("payload")
    if isinstance(payload, dict):
        if isinstance(payload.get(wrapper_key), dict) and payload.get(wrapper_key):
            return payload[wrapper_key]
        if payload:  # payload itself might be the item's data (categories may not nest further)
            return payload

    for key in (wrapper_key, "data", "result"):
        if isinstance(raw.get(key), dict) and raw.get(key):
            return raw[key]
    # Not wrapped -- the top-level object itself looks like the item if it
    # has an identifying field.
    if any(k in raw for k in ("name", "category_id", "product_id", "url", "handle")):
        return raw
    return {}


def enrich_product(product_id):
    """Fetch real image URL + page URL for one product from the public storefront API."""
    try:
        resp = _get_with_retry(f"{STOREFRONT_API_BASE}/products/{product_id}")
        if not resp.ok:
            _log_failure("product", product_id, f"HTTP {resp.status_code}")
            return product_id, {"image": "", "url": ""}
        raw = resp.json()
        data = _extract_payload(raw, "product", "product", product_id)
        full_url, image_url = _resolve_url_and_image(data, STOREFRONT_DOMAIN)
        return product_id, {"image": image_url, "url": full_url}
    except requests.RequestException as e:
        _log_failure("product", product_id, str(e))
        return product_id, {"image": "", "url": ""}


def enrich_category(category_id):
    """Fetch real image URL + page URL for one category from the public storefront API."""
    try:
        resp = _get_with_retry(f"{STOREFRONT_API_BASE}/categories/{category_id}")
        if not resp.ok:
            _log_failure("category", category_id, f"HTTP {resp.status_code}")
            return category_id, {"image": "", "url": ""}
        raw = resp.json()
        data = _extract_payload(raw, "category", "category", category_id)
        full_url, image_url = _resolve_url_and_image(data, STOREFRONT_DOMAIN)
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


def _slugify(name):
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _local_category_url_and_image(category, storefront_domain):
    """
    The storefront category endpoint turned out to return an entire
    product-listing page (hundreds of KB per category) without a clean
    "category url" field, and is expensive/slow to call 50+ times for no
    real benefit. Instead, build these directly from data the admin
    category list already gave us (no extra API call needed):
      - url: CONFIRMED pattern from real examples on the live site --
        https://{domain}/categories/{slug}/{category_id}
        e.g. https://www.strandroadireland.com/categories/dining-chairs/505193000000079002
      - image: same confirmed cdn3.zohoecommerce.com pattern used for
        products, using the category's own `document_id` (present in the
        admin category list per Zoho's docs) if set. UNVERIFIED: the
        filename segment is a placeholder since the admin list doesn't
        include the original filename -- if this doesn't load a real
        image, the document_id itself is still correct and only the
        cosmetic filename portion would need adjusting.
    """
    name = category.get("name", "")
    cat_id = category.get("category_id", "")
    url = f"https://{storefront_domain}/categories/{_slugify(name)}/{cat_id}" if name else ""

    image = ""
    doc_id = category.get("document_id") or ""
    if doc_id:
        image = f"https://cdn3.zohoecommerce.com/product-images/image.jpg/{doc_id}/600x600?storefront_domain={storefront_domain}"

    return url, image


def build_categories_json(categories, enrichment=None):
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
        url, image = _local_category_url_and_image(c, STOREFRONT_DOMAIN)
        group["categories"].append({
            "id": str(c["category_id"]),
            "name": c.get("name", ""),
            "url": url,
            "image": image,
            "parent_id": str(c.get("parent_category_id", "")),
        })

    if skipped_offline:
        print(f"Skipped {skipped_offline} offline categories")

    return {"groups": [g for g in groups.values() if g["categories"]]}


def _local_product_url(product, storefront_domain):
    """CONFIRMED pattern from a real example on the live site:
    https://{domain}/products/{slug}/{product_id}
    e.g. https://www.strandroadireland.com/products/victor-dining-armchair/505193000000299381
    Built locally (no API call, no dependence on the storefront response's
    own `url` field, which wasn't reliably present)."""
    name = product.get("name", "")
    pid = product.get("product_id", "")
    if not name or not pid:
        return ""
    return f"https://{storefront_domain}/products/{_slugify(name)}/{pid}"


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
            "url": _local_product_url(p, STOREFRONT_DOMAIN),
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
    # before this check existed -- looked "stuck" for ~35 minutes). Only
    # products need this now -- categories are built locally, no storefront
    # call at all (see build_categories_json / _local_category_url_and_image).
    if products:
        verify_storefront_access(sample_product_id=products[0]["product_id"])

    print("Enriching products with real image/page URLs (this is the slow part; categories are built locally, no API calls needed)...")
    product_enrichment = enrich_all([p["product_id"] for p in products], enrich_product, label="products")
    if _failure_count:
        print(f"NOTE: {_failure_count} storefront enrichment calls failed (image/url left blank for those items)")

    if INCLUDE_DIMENSIONS:
        for p in products:
            p["_dimensions"] = fetch_product_custom_fields(access_token, p["product_id"])
            time.sleep(0.1)

    categories_json = build_categories_json(categories)
    products_json = build_products_json(products, product_enrichment)

    os.makedirs(DOCS_DIR, exist_ok=True)

    with open(CATEGORIES_OUT, "w", encoding="utf-8") as f:
        json.dump(categories_json, f, indent=2, ensure_ascii=False)
    with open(PRODUCTS_OUT, "w", encoding="utf-8") as f:
        json.dump(products_json, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(categories_json['groups'])} category groups to {CATEGORIES_OUT}")
    print(f"Wrote {len(products_json['products'])} products to {PRODUCTS_OUT}")


if __name__ == "__main__":
    main()
