# Daily deals email: QA and UX audit

Date: 2026-11-14. Scope: the combined daily email ("דוח יומי") built by
`build_daily_payload` in `price-watcher/src/price_watcher/email_sender.py`,
on `main` @ `7f94e8e`. This replaces the audit of 2026-11-13 (commit `7a98727`
on branch `ccr-9ce5ad42-v8sgto`, never merged; `main` still held the
2026-11-12 version). Section 5 lists what happened to each of yesterday's
tickets.

Changes on `main` since yesterday's audit: couples-only packages with a
per-night package price (#62) and Carrefour added to the meat scan (#63).

## How this audit was done, and its limits

- **No email was attached to the audit request.** The audit is of the code
  that generates the email.
- **No live data.** The sandbox refuses connections to the price feeds, iHerb,
  EA and the travel sites. The email was rendered from fixtures pushed through
  the real pipeline:
  - Meat: PriceFull / PromoFull XML for 7 chains (Carrefour included) and 12
    branches, 36 items per branch, through `_scan_chain`, `_scan_store` and
    `scan_and_report`. Item names and promotion texts carry the tags named in
    the request (`מוג 3`, `ע.מלאי`, `ת.קצר`, `ס.בודדים`, `2 ב110`,
    `מעל 99 ש"ח`). One chain fails and one has no branch in the area.
  - Vacations: 280 deals from 7 of the 8 sources (the eighth fails) through
    `vacations.scan_and_report`, giving 27 packages and 27 flights.
  - Site promotions and creatine: the repository's test fixtures through
    `extract_promos` and `_parse_cells`. Sims 4: 22 packs on sale.
- **The fixture is invented data.** It tests behaviour. It says nothing about
  which real deals are good or how varied a real day is.
- Layout was measured in Chromium at 360, 375 and 800 px. Mail clients differ.
- Findings are marked **Confirmed** (reproduced with the fixture or measured)
  or **To verify** (read from the code; needs a real feed or a real client).
- The test suite passes (1,313 tests).
- Per the constraint, nothing here touches the sending schedule or frequency.

## 1. Executive summary

**Verdict: the numbers in the email are right, but a phone reader still gets a
page wider than the screen, about 23 screens long, with no indication of what
is worth opening.** Nothing that was open yesterday at P0 or P1 has been fixed
except the party-size comparison in vacation packages.

What improved since 2026-11-13: packages are for couples only, so "החבילה הכי
זולה" no longer compares different party sizes, and package prices use a
thousands separator.

Critical failure points today:

1. **The packages table is wider than a phone.** At 375 px the page is 507 px
   wide (432 px with the packages table hidden, 384 px with both vacation
   tables hidden). The "מה כלול" column is 88 px; a hotel name wraps over six
   lines and a package row averages 155 px.
2. **The email is at Gmail's clipping size.** The fixture email is 102,956
   bytes; Gmail cuts messages at about 102 KB. Each extra meat table adds
   about 1,740 bytes and each vacation row about 590. The vacation section and
   the footer come last, so they are what is cut.
3. **Conditional prices lead the tables.** In 15 of 25 meat tables the
   highlighted cheapest row has a condition (basket minimum, quantity limit,
   multi-buy, selected branches) shown only as raw feed text.
4. **A real "1+1" deal can vanish** when the feed gives no price and no rate.
   Nothing counts it.
5. **No summary.** 204 table rows (125 meat, 54 vacation, 22 Sims, 3
   creatine), 18,315 px tall on a phone. Meat starts 2,259 px down and
   vacations 10,705 px down. Nothing marks what is new since yesterday.

New today:

- The packages table is sorted by package total, but the first price column is
  now "מחיר ללילה", so it reads as unsorted (`$166, $104, $130, $130, $185,
  $188, $113`).
- English promotion text is reordered inside the right-to-left card:
  `30% OFF EVERYTHING` is displayed as `OFF EVERYTHING 30%`.
- Deals removed by the vacation rules (55 for length, 37 for party size, 7 for
  hotel rating in the fixture) are counted in the report but not shown.

## 2. QA and bug findings

### 2.1 Pricing and calculation

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q1 | A price with a spend threshold or a quantity limit ranks and is highlighted as a plain price. | `מעל 99 ש"ח פרגיות 39.90 לקג מוג 2` → row 1, ₪39.90, −39%, above an unconditional ₪59.90. 15 of 25 highlighted rows carry a condition | Confirmed |
| Q2 | Weighed multi-buy deals lose the "buy N" line. `deal_text` is set only for packaged items. | `בשר בקר טחון טרי 2 קג ב 100` → `₪50.00  ₪61.90 −19%`; `כרעיים עוף טרי 2 קג ב 40` → ₪20.00 in four chains. No "2 ק״ג ב-₪…" line | Confirmed |
| Q3 | "1+1" with no `DiscountedPrice` and no `DiscountRate` is dropped without a count. | `אסאדו 1+1`, `MinQty` 2 → no row; `unpriced_by_category` is empty | Confirmed |
| Q4 | "1+1" published as `DiscountRate` 50 shows −50% per kg with no sign that two units are needed. | `אסאדו 1+1` → `₪41.45  ₪82.90 −50%`, highlighted | Confirmed |
| Q5 | One item can take two rows of a five-row table: once per promotion, and once more at the regular price when another branch of the chain has no promotion. | "פילה מדומה", one chain: ₪72.90 (own promotion) and ₪79.11 (10% department promotion). `חזה עוף טרי`, one chain: ₪29.90 at one branch and ₪39.90 "מחיר רגיל" at two others (both rows show when the table holds more than five) | Confirmed |
| Q6 | The packages table is sorted by package total while the first price column is per night. | Rows 1–7: `$166, $104, $130, $130, $185, $188, $113` per night under "בכל יבשת מהזול ליקר" | Confirmed (new) |
| Q7 | Flight prices of 1,000 and more have no thousands separator; package prices do. | `$1021 לאדם` (flight) / `$2,316 לזוג` (package) | Confirmed |
| Q8 | A `DiscountRate` of 100 or more is ignored without a log line. The item is then listed at its regular price. | `DiscountRate` 2310 on "שייטל בקר טרי" → row "מחיר רגיל" ₪92.90 | Confirmed with the fixture; whether any chain publishes this scale is to verify |
| Q9 | Vacation deals removed by the trip rules are counted but not reported. | `dropped_length` 55, `dropped_party` 37, `dropped_low_star` 7; `email_sender.py` reads none of them | Confirmed (new) |

### 2.2 Parsing artifacts

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q10 | Condition tags are printed as they arrive. No code looks for them. | 38 of 125 meat rows: `סינטה בקר טרי מוג 2 קג ס.בודדים ע.מלאי ת.קצר`, `אנטריקוט טרי 2 ב110 ס.בודדים`, `ת.קצר בשר טחון בקר טרי 500 גרם`, `עוף שלם טרי מוג 2 יח ע.מלאי` | Confirmed |
| Q11 | The note line repeats the item name unless the two strings are identical. | `פילה מדומה בקר טרי מס 6` / `פילה מדומה מס 6 טרי`; `גרון הודו קפוא` / `גרון הודו קפוא ס.בודדים` | Confirmed |
| Q12 | Text is cut mid-word with no ellipsis: meat name and note at 60 characters, site promo condition at 300 (200 in text), creatine name at 70, package item at 80, flight details at 90. | 72-character name → `…מהדרין בד״ץ משק יש`; `…Unflavored, 1`; `…ללא מוצרי הום, משקפי` | Confirmed |
| Q13 | A site promotion's own expiry text is not read. | Terminal X fixture: `בתוקף עד 11.11 בחצות`, listed as active | To verify on a live page |
| Q14 | A site with only an "up to N%" sale shows "לא זוהה מבצע כלל-אתרי". | Correct by the current rule, but reads as "no sale" | Confirmed |
| Q15 | The creatine list is titled "5 הזולים ביותר" whatever its length, and its first row repeats the product in the card above it. | Three rows under "5 הזולים ביותר ל-100 גרם" | Confirmed (new) |

### 2.3 Layout and rendering

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q16 | HTML is at Gmail's clipping limit. | 102,956 bytes: meat 45,732, vacations 37,013, Sims 9,996, creatine 2,335, CSS 2,296. 65,675 bytes come before the vacation heading | Size confirmed; the limit is Gmail's documented ~102 KB, to verify in Gmail |
| Q17 | The packages table forces the email wider than a phone. | 360 and 375 px viewport → page 507 px wide. Columns 19/105/93/88/59/63 px | Confirmed in Chromium |
| Q18 | Package rows are tall on a phone. | 27 rows = 4,190 px, average 155 px, tallest 215 px. Flights average 86 px | Confirmed |
| Q19 | English promotion text is reordered in the right-to-left card. The condition `<div>` has no `dir="auto"`. | `30% OFF EVERYTHING` → `OFF EVERYTHING 30%`; `EXTRA 11% על כל האתר` → `% EXTRA 11 על כל האתר` | Confirmed in Chromium (new) |
| Q20 | Small text and small tap targets. | 816 of 1,631 text elements are below 12 px, smallest 10 px. The "לצפייה בדיל" button is 93 × 22 px | Confirmed |
| Q21 | The body has no date. | Header is "דוח יומי" only | Confirmed |
| Q22 | The subject is too long for an inbox list. | 135 characters; a phone shows about 40 | Confirmed |
| Q23 | The plain-text part repeats every row. | 848 lines, 52,071 bytes; 265 lines for vacations, each deal with a full tracking URL | Confirmed |
| Q24 | No `color-scheme` declaration; light colours are hard-coded. | `<head>` has the viewport tag and two `<style>` blocks | To verify in dark-mode clients |
| Q25 | Empty states differ. | Sims, meat, vacations not scanned: grey "(לא נסרק)". Creatine not scanned: red failure box | Confirmed |
| Q26 | Summary counts do not match the rows shown. | "181 דילים" above 54 rows; "215 מוצרי בשר" above 125 rows, 52 of them at "מחיר רגיל" | Confirmed |

Not a defect: every table sets RTL, prices are wrapped in LTR spans, the
multi-buy line reads in the right order (`מבצע 2 ב-₪110.00`), failed and
empty chains are named, payment-card and club-only promotions are filtered
out, and the desktop layout at 800 px has no overflow.

## 3. UX and content curation critique

**The email answers "what did the scanners find", not "what should I look at
today".**

1. **Open with "היום שווה לדעת": at most five lines.** One line each: what,
   where, price with its unit, and why it qualifies (new today, cheaper than
   yesterday, ends tomorrow). Pick across all sections. Everything below is
   reference.
2. **Make conditions part of the price.** Parse the tags once and show them as
   badges beside the price. A conditional price does not take the highlighted
   row from an unconditional one without a mark.

   | Raw | Badge |
   |---|---|
   | `מוג 3` | מוגבל ל-3 |
   | `ע.מלאי` | עד גמר המלאי |
   | `ת.קצר` | תוקף קצר |
   | `ס.בודדים` | בסניפים נבחרים |
   | `מעל 99 ש"ח` | בקנייה מעל ₪99 |
   | `2 ב110`, `2 קג ב 40` | 2 ב-₪110, 2 ק״ג ב-₪40 |
   | `1+1` | 1+1 |

3. **Show what changed, collapse what did not.** Sims 4 with no change is one
   line ("22 חבילות במבצע, ללא שינוי"). Creatine with no change is one line.
   A vacation deal seen yesterday at the same price is a compact line; a new
   or cheaper one gets the full row. `supermarkets-state.json` is written
   every day and never read; vacations keep no state.
4. **Rebuild the vacation row for a phone.** Two lines per deal instead of a
   six-column table: destination, dates and nights on the first; price per
   night, package total and a short "included" strip on the second
   (flight · hotel 4★ · breakfast · luggage). The site name becomes the link.
   Sort by the price that is shown first.
5. **Tell deals from regular prices at a glance.** 52 of 125 meat rows are
   regular prices that are in the table because they are cheap. In "שייטל"
   four of five rows are regular prices and the one deal is third. Keep the
   ranking, but give deal rows a mark so the eye finds them.
6. **Move the explanations out of the way.** The meat card carries a 60-word
   ranking note. The vacation card carries three paragraphs, and the badge
   legend with full definitions is printed above both vacation tables. Put
   one "איך לקרוא את הדוח" block at the bottom.
7. **Bridge the five topics.** A status strip under the header, one chip per
   section ("בשר: 12 חדשים", "חופשות: מ-$498 לזוג", "סימס: ללא שינוי"). Order
   the sections by what changed today instead of a fixed order that puts meat
   three screens down and vacations thirteen.
8. **One row per item per chain.** Keep the cheapest price a shopper can get
   for an item in a chain, and say "בסניף X בלבד" when it is not chain-wide.

## 4. Backlog

Priority order: BUG-18, BUG-02, BUG-19, BUG-05, BUG-03, UX-01, UX-10, then the
rest. Keys carried over keep their number; new keys start at BUG-25. BUG-22 is
closed (section 5).

---

**Issue Key / ID:** BUG-18
**Title:** Vacation packages table makes the whole email wider than a phone screen
**Type:** Bug Fix
**Priority:** P0 - Blocker
**Context & Current Behavior:** The six-column `.vt` packages table has `white-space:nowrap` on the dates, prices, badges and the deal button. At 360 and 375 px the document is 507 px wide (432 px with the packages table hidden, 384 px with both vacation tables hidden), so every section is cut off or scaled down. Columns are 19/105/93/88/59/63 px. The "מה כלול" cell wraps `• מלון The Old / Town / Residence / Luxury / Collection by / Example / Hotels (4★)`; a row averages 155 px and the tallest is 215 px.
**Expected Behavior:** The email is no wider than the screen at 360 px and up. A package row is at most about 110 px tall on a phone.
**Acceptance Criteria (AC):**
- At 360 px and 375 px, `document.documentElement.scrollWidth` equals the viewport width, with the vacation tables present.
- No cell renders narrower than 90 px with wrapped text.
- Average package row height at 375 px is 110 px or less.
- The desktop layout at 800 px shows the same information.

---

**Issue Key / ID:** BUG-02
**Title:** Daily email is at Gmail's size limit and is clipped on a busy day
**Type:** Bug Fix
**Priority:** P0 - Blocker
**Context & Current Behavior:** The fixture email is 102,956 bytes with 25 meat tables, 27 packages, 27 flights and 22 Sims packs. A meat table averages 1,736 bytes and a vacation row 586 bytes, so five more meat tables pass any reading of the ~102 KB limit. The vacation section and the footer are last and are what Gmail hides behind "View entire message".
**Expected Behavior:** The HTML part stays under 90 KB on a full day, so nothing is hidden.
**Acceptance Criteria (AC):**
- A test renders a full-size report (40 meat tables of 5 rows, 35 packages, 35 flights, 30 Sims packs on sale) and asserts `len(html_body.encode()) < 90_000`.
- When the content would exceed the budget, the lowest-priority rows are cut and a line says how many were left out.
- The footer is present in the first 90 KB.

---

**Issue Key / ID:** BUG-19
**Title:** Conditional meat prices are ranked and highlighted as plain prices
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** `_price_rank_key` sorts by price per kg only. `מעל 99 ש"ח פרגיות 39.90 לקג מוג 2` takes row 1 with the green highlight, above an unconditional ₪59.90. For weighed items `MeatPromo.deal_text` is empty, so `בשר בקר טחון טרי 2 קג ב 100` shows `₪50.00` with no "2 ק״ג" line. In the fixture 15 of 25 highlighted rows have a condition.
**Expected Behavior:** A price that needs a basket total, a minimum quantity or a club card shows the condition beside the price. On equal price, the unconditional row comes first. The highlight says when the price is conditional.
**Acceptance Criteria (AC):**
- `כרעיים 2 קג ב 40` (weighed) renders "2 ק״ג ב-₪40" under ₪20.00.
- A promotion whose text has `מעל N ש"ח` / `בקנייה מעל` renders "בקנייה מעל ₪N" beside the price.
- A promotion with `בקניית 2 קג ומעלה` renders "מ-2 ק״ג".
- With a conditional ₪39.90 and an unconditional ₪39.90 in one table, the unconditional row is first.
- The plain-text part shows the same conditions.

---

**Issue Key / ID:** BUG-05
**Title:** "1+1" promotions are dropped or shown without the "buy two" condition
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** `אסאדו 1+1` with `MinQty` 2 and no `DiscountedPrice` or `DiscountRate`: `_per_unit_promo_price` returns `None`, the row is dropped and no counter records it. The same deal published as `DiscountRate` 50 renders `₪41.45  ₪82.90 −50%`, highlighted, with nothing saying two units are needed.
**Expected Behavior:** A "1+1" (or "N+M") description is read as a multi-buy: unit price = regular × N / (N+M), shown with a "1+1" mark. A promotion that cannot be priced is counted.
**Acceptance Criteria (AC):**
- `אסאדו 1+1` on ₪79.90 with no price fields renders ₪39.95 per kg, −50%, with "1+1".
- `2+1` on ₪30.00 renders ₪20.00, −33%, with "2+1".
- A meat promotion dropped for having no usable price adds one to a counter that the ranking note reports.
- Existing multi-buy tests still pass.

---

**Issue Key / ID:** BUG-03
**Title:** Raw feed condition tags are printed in item names and notes
**Type:** Parser Refactor
**Priority:** P1 - High
**Context & Current Behavior:** No code recognises the tags. 38 of 125 fixture rows show them: `חזה עוף טרי מוג 3`, `כרעיים עוף טרי 2 קג ב 40 ע.מלאי`, `אנטריקוט טרי 2 ב110 ס.בודדים`, `ת.קצר בשר טחון בקר טרי 500 גרם`, `סינטה בקר טרי מוג 2 קג ס.בודדים ע.מלאי ת.קצר`.
**Expected Behavior:** One function takes the item name and the promotion description and returns a clean name plus a list of conditions (limit, while stocks last, short expiry, selected branches, minimum basket, multi-buy). The tags are removed from the displayed text.
**Acceptance Criteria (AC):**
- `מוג 3` / `מוגבל ל 3` → limit 3; `מוג 4 קג` → limit 4 kg; `מוג 2 יח` → limit 2 units.
- `ע.מלאי` / `עד גמר המלאי` → while stocks last. `ת.קצר` → short expiry. `ס.בודדים` → selected branches.
- `מעל 99 ש"ח` → minimum basket ₪99.
- None of these strings appears in the rendered HTML or text for the fixture rows above.
- A name with no tag is returned unchanged.
- Unit tests cover each tag at the start, middle and end of a string, and four tags in one string.

---

**Issue Key / ID:** UX-03
**Title:** Show promotion conditions as badges
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** Conditions appear only inside the grey note line, mixed with the repeated item name: `אנטריקוט טרי 2 ב110 ס.בודדים · עד 18/11`.
**Expected Behavior:** Each condition from BUG-03 renders as a short Hebrew badge under the price or the item: "מוגבל ל-3", "עד גמר המלאי", "תוקף קצר", "בסניפים נבחרים", "בקנייה מעל ₪99", "2 ב-₪110", "1+1".
**Acceptance Criteria (AC):**
- Every condition type has one fixed label; the mapping is in one table in the code.
- A row with four conditions fits two lines at 375 px.
- Badges add no more than 60 bytes per row to the HTML (class-based, no inline styles).
- The plain-text part lists the same labels in brackets.

---

**Issue Key / ID:** UX-01
**Title:** Add a "היום שווה לדעת" highlights block at the top
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** The email opens with the site promotions list. Meat starts 2,259 px down on a phone and vacations 10,705 px down. There are 204 table rows and no ranking across sections.
**Expected Behavior:** Up to five one-line highlights directly under the header, chosen across all sections: new today, cheaper than yesterday, ends within two days, or the cheapest unconditional price in its group.
**Acceptance Criteria (AC):**
- The block is the first content after the header and holds five lines or fewer.
- Each line states the item, the place, the price with its unit, and the reason.
- No conditional price is listed without its condition.
- With nothing new, the block reads "אין חדש היום" and the sections below collapse (UX-04).
- The block is in the plain-text part as well.

---

**Issue Key / ID:** UX-10
**Title:** Redesign the vacation deal row for phones
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** Packages use six columns; flights use five. On a phone the "מה כלול" bullets wrap one or two words per line, the luggage line is bold in every row, and the 27 package rows are 4,190 px tall. The price cell reads `$166 / ללילה לזוג` beside `$498 / לזוג`.
**Expected Behavior:** Two lines per deal. Line 1: destination, dates, nights. Line 2: price per night, package total, and a short "included" strip (flight · hotel name and stars · board · transfers · luggage). The site name is the link.
**Acceptance Criteria (AC):**
- 27 package rows are 3,000 px or less at 375 px.
- The hotel name and its stars stay on one line when they fit in 30 characters; a longer name is cut at a word with "…".
- Luggage is bold only when it is missing or unknown.
- The link target is at least 44 px tall.
- HTML per vacation row is 450 bytes or less (586 today).

---

**Issue Key / ID:** UX-04
**Title:** Collapse sections that did not change; mark what is new
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** The Sims 4 list (22 rows, 9,996 bytes) and the creatine list are printed in full every day even when the section says "ללא שינוי". `supermarkets.persist` writes `supermarkets-state.json` and nothing reads it. Vacations keep no state, so a deal listed for a week looks the same as one that appeared today.
**Expected Behavior:** Unchanged Sims and creatine sections are one line each. Meat rows and vacation deals that are new or cheaper than yesterday carry a "חדש" or "ירד" mark; vacation deals unchanged since yesterday render as a compact line.
**Acceptance Criteria (AC):**
- Sims with no new, ended or changed discounts renders one line with the count.
- Creatine with `changed == False` renders one line with the product, price and link.
- A meat row whose (chain, item, price per kg) was not in yesterday's state has a "חדש" mark; a lower price has "ירד".
- Vacation deals are stored per run (source, destination, dates, price) and compared the same way.
- On the first run, with no state, nothing is marked.

---

**Issue Key / ID:** BUG-25
**Title:** Packages are sorted by a price that is not the first price shown
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** Since #62 the first price column is "מחיר ללילה". `_rank` still sorts by price per person, which orders the "מחיר החבילה" column. The card says "בכל יבשת מהזול ליקר", and rows 1–7 read `$166, $104, $130, $130, $185, $188, $113` per night. Row 2 at $104 a night sits under row 1 at $166.
**Expected Behavior:** The sort key and the most prominent price are the same, and the caption names it.
**Acceptance Criteria (AC):**
- Either the table is sorted by price per night, or "מחיר החבילה" is the first and larger price column; the choice is stated in the caption ("ממוין לפי מחיר החבילה").
- A test builds three packages with totals 498 / 518 / 520 and nights 3 / 5 / 4 and asserts the rendered order matches the stated key.
- The text part uses the same order and leads each line with the same price.

---

**Issue Key / ID:** BUG-26
**Title:** English promotion text is reordered in the right-to-left layout
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** The site promotion condition is written into a `<div>` that inherits `dir="rtl"`. `30% OFF EVERYTHING` is displayed as `OFF EVERYTHING 30%`, and `EXTRA 11% על כל האתר` as `% EXTRA 11 על כל האתר`. The creatine names already use `dir="auto"`; these lines do not.
**Expected Behavior:** Each condition line takes its direction from its own text.
**Acceptance Criteria (AC):**
- The condition `<div>` has `dir="auto"` (and `text-align:right` is kept).
- In Chromium, `30% OFF EVERYTHING` renders in that order.
- A Hebrew condition still renders right-to-left with its percent sign after the number.
- The plain-text part is unchanged.

---

**Issue Key / ID:** BUG-20
**Title:** One item can fill two rows of a five-row cut table
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** `_finalize_report` dedupes on (chain, promotion description, price per kg). "פילה מדומה בקר טרי מס 6" in one chain appears at ₪72.90 (its own promotion) and at ₪79.11 (10% department promotion). `חזה עוף טרי` is listed at ₪29.90 for one branch and again at ₪39.90 "מחיר רגיל" for the chain's other two branches.
**Expected Behavior:** One row per (chain, item): the lowest price per kg, with the branches where it applies. A second price for the same item is not listed.
**Acceptance Criteria (AC):**
- Two promotions on one item in one chain render one row, at the lower price.
- A promotion in branch A only renders one row naming branch A; the regular price at branch B is not a separate row.
- Rows freed this way are filled by the next cheapest items, up to `PW_TOP_PER_CATEGORY`.
- The "N הזולים מתוך M" count uses the deduplicated total.

---

**Issue Key / ID:** BUG-21
**Title:** The note line repeats the item name
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** The note is hidden only when `desc == p.item_name`. `פילה מדומה בקר טרי מס 6` is followed by `פילה מדומה מס 6 טרי`; `גרון הודו קפוא` by `גרון הודו קפוא ס.בודדים`.
**Expected Behavior:** After BUG-03 removes the tags, a description that adds no words beyond the item name is not shown.
**Acceptance Criteria (AC):**
- A description whose words are all in the item name renders no note text.
- `10% הנחה על כל מחלקת הבשר` still renders.
- The end date still renders when the description is hidden.

---

**Issue Key / ID:** BUG-09
**Title:** Text is cut mid-word with no ellipsis
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** Slices `[:60]` (meat name and note), `[:300]` / `[:200]` (site promo condition), `[:70]` (creatine name), `[:80]` (package item), `[:90]` (flight details). A 72-character name ends `…מהדרין בד״ץ משק יש`; a creatine name ends `…Unflavored, 1`; a promo condition ends `…ללא מוצרי הום, משקפי`.
**Expected Behavior:** One helper cuts at a word boundary and appends "…".
**Acceptance Criteria (AC):**
- No rendered string ends in a partial word.
- Every cut string ends with "…".
- A string at or under the limit is unchanged.
- All the slices above use the helper.

---

**Issue Key / ID:** UX-05
**Title:** Shorter subject, preheader text and a date in the header
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** The fixture subject is 135 characters: `דוח יומי: עד 30% הנחה ב-MyProtein · 22 חבילות סימס 4 במבצע · מבצעי בשר ב-7 רשתות (הרצליה, רמת השרון) · חבילות לזוג מ-$498 · טיסות מ-$82`. There is no preheader, and the header reads "דוח יומי" with no date.
**Expected Behavior:** The subject names the one or two most important items in 60 characters or fewer; the rest goes into a hidden preheader. The header shows the date.
**Acceptance Criteria (AC):**
- Subject length is 60 characters or fewer for the full fixture.
- A failed section is still named in the subject.
- A hidden preheader of 90 characters or fewer follows `<body>`.
- The header shows the day and date in Hebrew ("יום שבת, 14.11.2026"); the text part's first line does too.

---

**Issue Key / ID:** UX-06
**Title:** Status strip under the header; order sections by what changed
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** Five unrelated topics run in a fixed order: site promotions, creatine, Sims 4, meat, vacations. Each has its own card style. The reader cannot see which sections have news without scrolling about 23 screens.
**Expected Behavior:** One chip per section under the header with its state ("בשר: 12 חדשים", "סימס: ללא שינוי", "חופשות: נכשל"). Sections with changes come first; unchanged ones follow in collapsed form.
**Acceptance Criteria (AC):**
- Every section, including a failed or disabled one, has a chip.
- A failed section's chip is red and matches the subject's failure note.
- With changes in meat only, meat is the first section.
- Section order is stable when nothing changed.

---

**Issue Key / ID:** UX-12
**Title:** Move the explanations to one "how to read this" block
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** The meat summary card holds a 60-word ranking note. The vacation card holds three paragraphs (windows per continent, price definitions, "מה כלול" definition), and the badge legend with full definitions is printed above both the packages and the flights table.
**Expected Behavior:** Each section keeps one short line of scope. Definitions and ranking rules sit once in a block at the bottom.
**Acceptance Criteria (AC):**
- No section intro is longer than 25 words.
- The badge legend appears once.
- Notes that report a problem today (failed chain, unavailable site, unpriced deals) stay in their section.

---

**Issue Key / ID:** UX-13
**Title:** Mark deal rows apart from regular-price rows in the meat tables
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** Regular prices compete with deals by design. In the fixture 52 of 125 rows are "מחיר רגיל"; in "שייטל" four of five rows are regular prices and the only deal is third. The two kinds differ only by a small grey "מחיר רגיל" against a small red percentage.
**Expected Behavior:** A deal row is recognisable without reading the small print, and the table header says how many of its rows are deals.
**Acceptance Criteria (AC):**
- Deal rows carry one visual mark (for example a "מבצע" chip beside the price) that regular rows do not.
- The table header reads "5 הזולים מתוך 14 · 2 במבצע".
- The ranking by price per kg is unchanged.
- The mark adds no more than 40 bytes per row.

---

**Issue Key / ID:** BUG-23
**Title:** Summary counts do not match the rows shown, and filtered deals are not reported
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** The vacation card says "181 דילים" (`total_found`, before the continent and top-N cuts) above 54 rows. The meat card says "215 מוצרי בשר" above 125 rows. `VacationReport.dropped_length` (55), `dropped_party` (37) and `dropped_low_star` (7) are set by the scan and never rendered.
**Expected Behavior:** The headline number is the number shown, with the scanned total beside it: "54 דילים מוצגים מתוך 181". One line reports what the rules removed.
**Acceptance Criteria (AC):**
- The bold count equals the number of rows rendered in that section.
- The scanned total appears as "מתוך N" only when it is larger.
- When any `dropped_*` counter is above zero, one line lists them ("לא הוצגו: 55 בגלל אורך החופשה, 37 שאינן לזוג, 7 בגלל דירוג המלון").

---

**Issue Key / ID:** BUG-10
**Title:** Flight prices have no thousands separator
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** `_deal_price` prints `$1021 לאדם`. `_deal_total` and `_package_night_price` print `$2,316`. The subject's "טיסות מ-" uses the first form.
**Expected Behavior:** All vacation amounts use a thousands separator and no decimals.
**Acceptance Criteria (AC):**
- 1390 renders `$1,390` in the HTML, the text part and the subject.
- 289 renders `$289`.

---

**Issue Key / ID:** BUG-07
**Title:** Confirm the scale of `DiscountRate` in each chain's feed, Carrefour included
**Type:** Parser Refactor
**Priority:** P3 - Low
**Context & Current Behavior:** A `DiscountRate` of 100 or more is ignored (`0 < discount_rate < 100`) with no log line. With `DiscountRate` 2310 the fixture item is listed as "מחיר רגיל" ₪92.90. Carrefour's feed was added in #63 and its scale has not been recorded either.
**Expected Behavior:** The scale per chain is known and recorded; an out-of-range rate is logged with the chain name.
**Acceptance Criteria (AC):**
- A real PromoFull file from each scanned chain is checked and the result noted in the README.
- A rate of 100 or more writes a warning with chain, store and item code.
- If a chain uses hundredths, the value is divided by 100 for that chain and a test covers it.

---

**Issue Key / ID:** BUG-24
**Title:** Site promotions past their stated end date are still listed
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** The promo condition text can carry its own expiry (`בתוקף עד 11.11 בחצות` in the Terminal X fixture). Nothing reads it. Not confirmed on a live page.
**Expected Behavior:** A date in "בתוקף עד DD.MM" / "עד ה-DD/MM" form is parsed; a promotion past that date is dropped or marked "פג תוקף".
**Acceptance Criteria (AC):**
- A condition with yesterday's date is not listed as active.
- A condition with today's date is listed.
- A condition with no date is unchanged.

---

**Issue Key / ID:** BUG-27
**Title:** Creatine list title says five whatever the list length, and repeats the top product
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** The title "5 הזולים ביותר ל-100 גרם" is a fixed string above `creatine.products[:5]`. With three powders found it sits above three rows. Row 1 is the same product, price and link as the card directly above it.
**Expected Behavior:** The title states the number of rows shown. The list starts at the second cheapest product, or the card is dropped in favour of the list.
**Acceptance Criteria (AC):**
- With three powders the title reads "3 הזולים ביותר".
- With one powder no list is rendered.
- The cheapest product appears once in the section, in both the HTML and the text part.

---

**Issue Key / ID:** BUG-15
**Title:** Empty and failed states differ between sections
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** Sims, meat and vacations that were not scanned show an unstyled grey "(לא נסרק)". Creatine in the same state shows a red failure box.
**Expected Behavior:** One "not scanned" component and one "failed" component, used by every section.
**Acceptance Criteria (AC):**
- A section set to `None` renders the same grey block in all five sections.
- A failed section renders `_failure_box_html` in all five.
- The text part uses the same two wordings.

---

**Issue Key / ID:** BUG-16
**Title:** Plain-text part repeats the full dump
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** 848 lines and 52,071 bytes for the fixture. Each vacation deal takes four or five lines, one of them a full URL with tracking parameters. `₪` and digits sit inside RTL lines with no direction marks.
**Expected Behavior:** The text part holds the highlights, one summary line per section and the top three rows of each table.
**Acceptance Criteria (AC):**
- The text part is 150 lines or fewer for the full fixture.
- Amounts are wrapped in LRM marks so `₪49.90` reads in order.
- The signature stays the last line.

---

**Issue Key / ID:** BUG-17
**Title:** No dark-mode declaration
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** The `<head>` has no `color-scheme` meta tag. Light backgrounds and dark text are hard-coded, so clients that auto-invert may produce low-contrast rows. Not checked in a real client.
**Expected Behavior:** The email declares `color-scheme: light dark` and is checked in Gmail and Apple Mail dark mode.
**Acceptance Criteria (AC):**
- `<meta name="color-scheme" content="light dark">` and the matching CSS property are present.
- Screenshots from Gmail (Android or iOS) and Apple Mail in dark mode show readable prices, badges and the deal button.

## 5. Status of the 2026-11-13 tickets

| Key | Title (short) | Status on `main` @ `7f94e8e` |
|---|---|---|
| BUG-02 | Gmail clipping | Open: 102,956 bytes |
| BUG-18 | Packages table wider than a phone | Open: 507 px at 375 px |
| BUG-19 | Conditional prices ranked as plain | Open |
| BUG-05 | "1+1" dropped or unmarked | Open |
| BUG-03 | Raw condition tags | Open |
| UX-03 | Condition badges | Open |
| UX-01 | Highlights block | Open |
| UX-10 | Vacation row for phones | Open: average package row 155 px in today's fixture (203 px in yesterday's; the fixtures differ) |
| UX-04 | Collapse unchanged, mark new | Open |
| BUG-22 | Cheapest package across party sizes | **Fixed** by #62: only packages for two adults are listed, and a test covers it |
| BUG-20 | One item in several rows | Open |
| BUG-21 | Note repeats the item name | Open |
| BUG-09 | Text cut mid-word | Open |
| UX-05 | Subject, preheader, date | Open (subject 135 characters) |
| UX-06 | Status strip, section order | Open |
| UX-12 | One "how to read" block | Open |
| BUG-23 | Counts do not match rows | Open; extended with the unreported `dropped_*` counters |
| BUG-10 | Vacation number formatting | Partly fixed: packages have separators, flights do not |
| BUG-07 | `DiscountRate` scale | Open; Carrefour added to the chains to check |
| BUG-24 | Expired site promotions | Open |
| BUG-15 | Empty states differ | Open |
| BUG-16 | Plain-text dump | Open (848 lines) |
| BUG-17 | No dark-mode declaration | Open |

New today: BUG-25, BUG-26, BUG-27, UX-13.
