# Price Watcher – test pages

Real product pages used as reference while building and testing the price
extractor. One per shop. See the feature description in
[issue #1](https://github.com/NoaMcDa/ComputerAutomations/issues/1) and the
extractor work in
[issue #4](https://github.com/NoaMcDa/ComputerAutomations/issues/4).

## Shop pages (Phase 1)

| Shop | Item | Link |
|---|---|---|
| Seestarz | `25-610navy`, variant `62803155976563` | <https://seestarz.com/products/25-610navy?variant=62803155976563> |
| Castro | סווטשירט אוף שולדר אספרסו, item `7310573` | <https://www.castro.com/סייל/נשים/סווטשירט-אוף-שולדר-אספרסו-7310573> |
| MyProtein (Israel) | MP Women's Tempo Triangle Vest – Black, product `17669270` | <https://www.myprotein.co.il/p/sports-clothing/mp-women-s-tempo-triangle-vest-black/17669270/> |

## What each link tells us

These are read from the links alone. None of the pages has been fetched yet;
the survey in issue #4 confirms or corrects each point.

### Seestarz
- The `/products/<handle>?variant=<id>` shape is the one Shopify stores use.
  If the shop is on Shopify, the same product is also available as data at
  `/products/25-610navy.js`, which lists every variant with its price and
  availability, so no page parsing would be needed.
- The link pins one variant (`variant=62803155976563`). The watcher must read
  the price of that variant, not the product's default one.

### Castro
- The path is in Hebrew. The script must handle non-ASCII URLs (percent-encode
  when fetching, show the readable form in emails and in `list`).
- The item sits under `/סייל/` (sale). The path may change when the sale ends,
  so the watcher should follow redirects and, if the link dies, report
  "product page moved or removed" instead of a price change. The trailing
  number `7310573` looks like the stable item identifier.
- A sale item is likely to show both a regular and a sale price; the watcher
  tracks the price actually charged.
- No size or colour is pinned in the link.

### MyProtein (Israel)
- Israeli site (`myprotein.co.il`), prices in ₪.
- Clothing item, so it has sizes; the link does not pin one. Price is expected
  to be the same across sizes, availability is not.
- Checkout discount codes apply here: see
  [issue #11](https://github.com/NoaMcDa/ComputerAutomations/issues/11)
  (promotion banner and estimated final price).

## Survey checklist

To fill in from the home computer, per shop:

| Shop | Price in structured data? | Needs CSS selector? | Needs headless browser? | Blocks plain requests? | Notes |
|---|---|---|---|---|---|
| Seestarz | | | | | |
| Castro | | | | | |
| MyProtein | | | | | |

## Flights (Phase 2)

No example searches recorded yet for El Al or Arkia. See
[issue #10](https://github.com/NoaMcDa/ComputerAutomations/issues/10).
