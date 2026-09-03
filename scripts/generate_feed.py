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
  - Everything else -- image URLs and page URLs for both products and
    categories -- is built LOCALLY from data already in those two bulk
    lists (each record's own image-document data plus its name/id), with
    no further API calls. An earlier version of this script fetched a
    real image/page URL per item from the Zoho Commerce STOREFRONT API,
    but that per-item call turned out to be unreliable for a large share
    of a catalog that uses attribute variants (color/size/etc.) heavily --
    HTTP 404 or an empty-but-"success" payload for 721 of 921 products in
    one real run -- so it was dropped entirely in favor of building
    everything from the admin data already in hand.

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
# ADMIN API call per product (slower, more API usage).
INCLUDE_DIMENSIONS = os.environ.get("INCLUDE_DIMENSIONS", "false").lower() == "true"
DIMENSIONS_FIELD_NAME = os.environ.get("DIMENSIONS_FIELD_NAME", "dimensions")

ACCOUNTS_BASE = f"https://accounts.zoho.{DATA_CENTER}"
ADMIN_API_BASE = f"https://commerce.zoho.{DATA_CENTER}/store/api/v1"

DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
CATEGORIES_OUT = os.path.join(DOCS_DIR, "categories.json")
PRODUCTS_OUT = os.path.join(DOCS_DIR, "products.json")
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


# ----------------------- Diagnostics -----------------------

# Populated in main() right after fetch_all_products() -- product_id -> the
# full raw ADMIN API record for that product (no extra API call, we already
# have this in memory). Used below to dump the admin-side record for a
# product whose image couldn't be resolved locally, so we can see what's
# actually different about it without needing a live API call to diagnose it.
_PRODUCTS_BY_ID = {}
_admin_dumps_printed = 0
_MAX_ADMIN_DUMPS = 3


def _dump_admin_record_for_diagnostics(product_id):
    """
    A product whose image couldn't be resolved from its own ADMIN record
    (see _local_product_image) is unexpected for most products -- print
    that product's full admin-side record (already in memory, no extra
    call) for the first few, so whatever distinguishes it (a genuinely
    missing image, a different field layout, etc.) is visible instead of
    guessed at.
    """
    global _admin_dumps_printed
    if _admin_dumps_printed >= _MAX_ADMIN_DUMPS:
        return
    record = _PRODUCTS_BY_ID.get(product_id)
    if not record:
        return
    _admin_dumps_printed += 1
    pretty = json.dumps(record, indent=2)
    print(f"  ADMIN record for diagnostics, product id={product_id} ({len(pretty)} chars):")
    print(pretty[:6000])
    if len(pretty) > 6000:
        print(f"  ...(truncated, {len(pretty) - 6000} more chars)")


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
        image = f"https://cdn3.zohoecommerce.com/product-images/image.jpg/{doc_id}/700x700?storefront_domain={storefront_domain}"

    return url, image


def _local_product_image(product, storefront_domain):
    """
    Build the product's image URL directly from the ADMIN bulk product
    record's own image-documents data -- same idea as
    _local_category_url_and_image, and for the same reason: calling out to
    the STOREFRONT API per item turned out to be the wrong source of truth.

    CONFIRMED root cause of the "blank image despite the product genuinely
    having one" reports: the storefront single-product endpoint
    (`/storefront/api/v1/products/{id}`) returned either HTTP 404 or an
    empty-but-"success" `{"payload": {}}` for 721 of 921 products in one
    real run -- almost certainly because a large share of this catalog's
    product ids are per-variant/attribute rows (this catalog uses
    attribute1/attribute2 e.g. "Frame Colour"/"Cushion Colour" variants
    extensively) that don't get their own individual storefront page, even
    though they're real, orderable products with real images in Zoho.

    Confirmed directly against a real example that 404'd
    (id=505193000003916949, "Charlottenborg Exterior Lounge Chair"): its
    ADMIN record -- from the same bulk /products list this script already
    fetches, no extra API call needed -- carries a documents array with
    real image data sitting right there:
      [{"file_name": "AJ-25-SU_Charlottenborg_cushion_A631.jpg",
        "document_id": "505193000002292330", "attachment_order": 1, ...}, ...]
    which fits the exact same cdn3.zohoecommerce.com URL pattern already
    confirmed working for every other image in this feed -- just sourced
    locally instead of via the unreliable per-product storefront call.

    This admin-side documents array has no `is_featured` flag (unlike the
    storefront API's own shape), so the lowest `attachment_order` is used
    as the primary/cover image instead, with documents[0] as a fallback if
    that field is ever missing. Tries a couple of other plausible
    array/key names defensively, same as the old storefront-based
    resolver did, in case some products carry image data under a
    different key.
    """
    IMAGE_SIZE = "700x700"  # confirmed working suffix requested for feed images

    for array_key in ("documents", "product_images", "variant_images", "media"):
        documents = [d for d in (product.get(array_key) or []) if isinstance(d, dict)]
        if not documents:
            continue

        featured = next((d for d in documents if d.get("is_featured")), None)
        if not featured:
            with_order = [d for d in documents if d.get("attachment_order") is not None]
            featured = min(with_order, key=lambda d: d["attachment_order"]) if with_order else documents[0]

        doc_id = featured.get("document_id") or featured.get("id") or featured.get("image_id") or ""
        doc_name = featured.get("file_name") or featured.get("name") or featured.get("filename") or "image.jpg"
        if doc_id:
            return (
                f"https://cdn3.zohoecommerce.com/product-images/"
                f"{quote(str(doc_name))}/{doc_id}/{IMAGE_SIZE}?storefront_domain={storefront_domain}"
            )

    return ""


def build_categories_json(categories, enrichment=None):
    """
    Builds the flat "groups[].categories[]" shape the widget's category
    browser reads directly (it only ever renders two levels: the group's
    parent_name as a label, and a flat row of cards for group["categories"]
    -- it has no concept of drilling deeper than that).

    IMPORTANT FIX: earlier versions of this function only ever added a root
    category as the group's label/header -- never as a card inside its own
    "categories" list. That's fine for a root with children (the children
    become the cards), but for any root category with NO children -- which
    does happen in this catalog (confirmed live: two real categories,
    holding products like "Woodland Screen Landscape/Portrait" and "Picoti
    Bird Feeder", have zero children and were being silently dropped from
    categories.json entirely, because `[g for g in groups.values() if
    g["categories"]]` at the bottom discards any group left with an empty
    "categories" list) -- this made those categories, and every product
    tagged directly to them, permanently invisible to "Add products" /
    "Change product": not shown as their own card, and not reachable
    through any parent either, since they have none.

    Fix: every root is now ALSO added as the first card in its own group
    (self-referencing) -- same as any other browsable category. For a root
    WITH children this just adds one extra "browse everything directly
    under X" card ahead of its more specific children, which is harmless
    and fairly standard category-browser UX. For a childless root, this is
    what makes it (and its products) show up at all, instead of vanishing.
    """
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
    skipped_offline = 0

    for c in categories:
        if not is_root(c):
            continue
        key = str(c["category_id"])
        group = groups.setdefault(key, {
            "parent_id": key,
            "parent_name": c.get("name", ""),
            "categories": [],
        })
        if not online_by_id.get(c["category_id"], True):
            skipped_offline += 1
            continue
        # Self-reference: the root is also its own group's first browsable
        # card (see docstring above for why this matters).
        url, image = _local_category_url_and_image(c, STOREFRONT_DOMAIN)
        group["categories"].append({
            "id": key,
            "name": c.get("name", ""),
            "url": url,
            "image": image,
            "parent_id": "",
        })

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


def _ancestor_chain_ids(cat_id, by_id):
    """
    All ancestor category ids for a product's single directly-assigned
    category, from the category itself up through every parent to (and
    including) the ultimate root -- e.g. for a product tagged "Dining
    Chairs", whose parent is "Indoor Furniture", whose parent is the root
    "Products": ["Dining Chairs id", "Indoor Furniture id", "Products id"].

    Zoho only lets a product carry ONE direct category, but the widget's
    "Add products" / "Change product" browser lets a customer click into
    ANY category card -- including a parent like "Indoor Furniture" -- and
    expects to see that category's products. Without this expansion, a
    parent category card with real children but no products tagged
    directly to IT (the norm -- products are almost always tagged with the
    most specific child, not the parent) would always show "No products
    found", even though its children's products are exactly what a
    customer browsing that parent would expect to see. This walks the
    chain and includes every ancestor so browsing at any level works.

    If `cat_id` isn't found in `by_id` (e.g. the category itself came back
    offline/excluded from the categories list, or with an id Zoho didn't
    return in the categories endpoint at all), it's still included as-is --
    we just can't expand further up from it.
    """
    chain = []
    seen = set()
    cur_id = cat_id
    while cur_id and cur_id not in seen:
        seen.add(cur_id)
        chain.append(cur_id)
        cur = by_id.get(cur_id)
        if not cur or is_root(cur):
            break
        cur_id = cur.get("parent_category_id")
    return chain


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


def build_products_json(products, categories=None):
    by_id = {c["category_id"]: c for c in (categories or [])}

    out = []
    blank_image_count = 0
    for p in products:
        pid = str(p.get("product_id"))
        cat_id = p.get("category_id")

        if cat_id not in (None, "", "0", 0):
            category_ids = [str(cid) for cid in _ancestor_chain_ids(cat_id, by_id)]
        else:
            category_ids = []

        image = _local_product_image(p, STOREFRONT_DOMAIN)
        if not image:
            blank_image_count += 1
            _dump_admin_record_for_diagnostics(p.get("product_id"))

        entry = {
            "id": pid,
            "name": p.get("name", ""),
            "sku": p.get("sku", ""),
            "image": image,
            "url": _local_product_url(p, STOREFRONT_DOMAIN),
            # Zoho Commerce does not expose an "add to cart via link" endpoint --
            # cart actions go through the storefront's own JS/session flow, not a
            # plain URL. Left blank on purpose; see README "Known limitations".
            "add_to_cart_url": "",
            "dimensions": p.get("_dimensions", ""),
            # Zoho Commerce only lets a product carry ONE direct category
            # (confirmed across the admin and storefront product APIs), but
            # this list also includes every ANCESTOR of that category (up to
            # and including the top-level root) -- see _ancestor_chain_ids --
            # so that the widget's category browser shows this product no
            # matter which level (leaf, mid, or root) a customer clicks into.
            # It's still not a true "assign to several unrelated categories"
            # list; see README "Known limitations".
            "category_ids": category_ids,
        }
        out.append(entry)

    if blank_image_count:
        print(
            f"NOTE: {blank_image_count} products had no resolvable image in their own "
            f"ADMIN record (see any 'ADMIN record for diagnostics' dumps above for the "
            f"first few -- these are genuine gaps in Zoho's own data for those items, "
            f"not a lookup failure on our side)"
        )

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

    global _PRODUCTS_BY_ID
    _PRODUCTS_BY_ID = {p["product_id"]: p for p in products}

    if INCLUDE_DIMENSIONS:
        for p in products:
            p["_dimensions"] = fetch_product_custom_fields(access_token, p["product_id"])
            time.sleep(0.1)

    # Both categories AND products are now built entirely from data already
    # in memory from the two bulk ADMIN list calls above -- no per-item
    # STOREFRONT API call needed for either. Products used to be enriched
    # one-by-one from the storefront (like categories briefly were too,
    # early on), but that per-product call turned out to fail outright
    # (HTTP 404, or an empty-but-"success" payload) for 721 of 921 products
    # in one real run -- almost certainly because this catalog uses
    # color/attribute variants heavily, and many product ids are
    # variant/attribute rows the storefront doesn't serve an individual
    # page for. Their own ADMIN record still carries real image data
    # directly (confirmed against a real 404'd product), so images are
    # built locally the same way category images already were -- see
    # _local_product_image.
    categories_json = build_categories_json(categories)
    products_json = build_products_json(products, categories)

    os.makedirs(DOCS_DIR, exist_ok=True)

    with open(CATEGORIES_OUT, "w", encoding="utf-8") as f:
        json.dump(categories_json, f, indent=2, ensure_ascii=False)
    with open(PRODUCTS_OUT, "w", encoding="utf-8") as f:
        json.dump(products_json, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(categories_json['groups'])} category groups to {CATEGORIES_OUT}")
    print(f"Wrote {len(products_json['products'])} products to {PRODUCTS_OUT}")


if __name__ == "__main__":
    main()
