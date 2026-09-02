# Strand Road Ireland — automatic Zoho Commerce feed

This repo regenerates two files every day (06:00 UTC) from live Zoho
Commerce data, and publishes them as public URLs via GitHub Pages:

- `docs/categories.json`
- `docs/products.json`

Both match the schema your placement widget expects. Hand the two URLs
to whoever configures the widget — they'll always reflect current
products/categories.

## One-time setup (about 10 minutes)

1. **Create a free GitHub account** at https://github.com/join if you don't
   already have one.

2. **Create a new repository.**
   - Click the "+" in the top right → "New repository".
   - Name it anything, e.g. `strandroad-feed`.
   - Set it to **Public** (required for free GitHub Pages).
   - Click "Create repository".

3. **Upload these files.**
   - On the new repo's page, click "Add file" → "Upload files".
   - Drag in this entire folder's contents, keeping the folder structure
     intact (`.github/workflows/generate-feed.yml`, `scripts/generate_feed.py`,
     `requirements.txt`, `docs/categories.json`, `docs/products.json`,
     `docs/.nojekyll`).
   - Commit directly to the `main` branch.

4. **Add your Zoho credentials as secrets** (these stay encrypted — no one,
   including me, can read them back once saved).
   - Go to the repo's **Settings** tab → **Secrets and variables** →
     **Actions** → **New repository secret**.
   - Add each of the following, one at a time:
     - `ZOHO_CLIENT_ID`
     - `ZOHO_CLIENT_SECRET`
     - `ZOHO_REFRESH_TOKEN`
     - `ZOHO_ORG_ID`
     - `ZOHO_DC` — your Zoho data center, e.g. `eu` (just the letters
       after the dot — `com`, `eu`, `in`, `com.au`, or `jp`)

5. **Enable GitHub Pages.**
   - Still in **Settings**, go to **Pages**.
   - Under "Build and deployment" → "Source", choose **Deploy from a branch**.
   - Branch: `main`, folder: `/docs`. Save.
   - GitHub will show you the live URL (something like
     `https://yourusername.github.io/strandroad-feed/`). Your two files
     will be at `.../categories.json` and `.../products.json`. This can
     take a minute or two to go live the first time.

6. **Run it once manually to test.**
   - Go to the **Actions** tab → **Generate Zoho Commerce Feed** (left
     sidebar) → **Run workflow** → **Run workflow** button.
   - This run takes longer than a typical script — it makes one extra
     request per product and per category to fetch real image/page URLs,
     so a catalog of a few hundred products may take a few minutes.
   - Once it succeeds, visit both URLs in your browser to confirm real
     data is showing instead of the placeholder `_note`.

That's it from there — it runs on its own every day.

## Known limitations (read before handing this to your widget vendor)

**Only one category per product.** Zoho Commerce assigns each product to a
single category (confirmed across every Zoho Commerce product API). So
`category_ids` in `products.json` will always contain exactly one id, not
several, even though the schema supports an array. If products genuinely
need to appear under multiple categories in the widget, that grouping
would have to happen on the widget/agency side, or by maintaining a manual
mapping outside of Zoho — Zoho itself has no multi-category field to pull
from.

**IDs are output as strings, including for categories.** Your example
showed category `id` as a plain number (`12`), but real Zoho category and
product IDs are large numbers (often 15+ digits) that can lose precision
if treated as JSON numbers in JavaScript. Every id in both files is
output as a string to guarantee `category_ids` entries in `products.json`
match `id` values in `categories.json` exactly. If the widget requires
`categories[].id` to be a raw JSON number rather than a string, tell me
and I'll adjust — but flag that risk to whoever built the widget, since
large numeric IDs are a common source of silent mismatches.

**`add_to_cart_url` is left blank.** Zoho Commerce doesn't offer a plain
link that adds an item to the cart — cart actions go through the
storefront's JavaScript/session flow, not a URL you can construct ahead
of time. If your widget truly needs a working add-to-cart link per
product, that likely needs to be built as a custom storefront script
(e.g. a small JS snippet the widget can call), which is a separate task
from feed generation.

**`dimensions` is blank by default.** Zoho Commerce has no standard
"dimensions" product field — it would have to live in a custom field or
the specifications list, and each store sets those up differently. Set
the secret `INCLUDE_DIMENSIONS=true` and, if needed, `DIMENSIONS_FIELD_NAME`
(defaults to matching any field/spec whose label contains "dimensions")
to turn this on. It costs one extra Zoho API call per product, so it
will slow down each run further on larger catalogs.

**Category images/URLs and product images/URLs** come from Zoho's public
Storefront API rather than the bulk admin lists (which don't return real
image URLs). This means the script also needs to know your storefront's
published domain — by default it derives this from `STORE_DOMAIN`, but if
your store is actually published under a different address (e.g. a
`*.zohostore.com` domain rather than your custom domain), add a
`STOREFRONT_DOMAIN` secret with that exact host.

## Changing the schedule

Edit the `cron:` line in `.github/workflows/generate-feed.yml`. Cron times
are in UTC. For example, `0 6,18 * * *` runs twice a day (6am and 6pm UTC).

## If something looks wrong

Check the failed/succeeded run's log under the **Actions** tab — every
step prints what it did, and Python errors show exactly which API call
failed and why (most often a typo in a secret, the wrong `ZOHO_DC`, or a
storefront domain that doesn't match what's published).
