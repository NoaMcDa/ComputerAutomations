# Daily deals email: QA and UX audit

Date: 2026-11-13. Scope: the combined daily email ("דוח יומי") built by
`build_daily_payload` in `price-watcher/src/price_watcher/email_sender.py`,
on `main` @ `033caa6`. This replaces the audit of 2026-11-12 (`3f1f860`);
section 5 lists what happened to each of that audit's tickets.

## How this audit was done, and its limits

- **No email was attached to the audit request.** The audit is of the code
  that generates the email.
- **No live data.** The sandbox blocks the price feeds, iHerb, EA and the
  travel sites (HTTP 403 from the network policy). The email was rendered from
  fixtures pushed through the real pipeline:
  - Meat: PriceFull / PromoFull XML for 5 chains and 9 branches (52 items per
    branch) through `_scan_store` and `_finalize_report`. Item names and
    promotion texts carry the tags named in the request (`מוג 3`, `ע.מלאי`,
    `ת.קצר`, `ס.בודדים`, `2 ב110`, `מעל 99 ש"ח`).
  - Vacations: 134 deals through `_classify` and `_continent_groups`, giving
    30 packages and 30 flights (the default settings allow up to 35 of each).
  - Site promotions, creatine and Sims 4: the repository's own test fixtures
    through `extract_promos`, `_parse_cells` and `parse_packs`.
- **The fixture repeats the same promotions in every chain**, so it does not
  show how varied a real day is. It was used to test behaviour, not to judge
  which deals are good.
- Layout was measured in Chromium at 375 px and 800 px. Mail clients differ:
  some shrink a wide email instead of scrolling it.
- Findings are marked **Confirmed** (reproduced with the fixture or measured)
  or **To verify** (read from the code; needs a real feed or a real client).
- The existing test suite passes (1,302 tests).
- Per the constraint, nothing here touches the sending schedule or frequency.

## 1. Executive summary

**Verdict: the meat section is now correct and readable. The email as a whole
is still too long, is still clipped by Gmail, and is broken on a phone by the
new vacation table.**

What improved since 2026-11-12: multi-buy prices are per unit, prices are per
kg, meat is grouped by cut with the five cheapest rows, failed chains are
named, payment-method "deals" are filtered out, and the meat table fits a
phone (three columns).

Critical failure points today:

1. **Gmail clips the message.** The fixture email is 103,355 bytes of HTML;
   Gmail cuts messages above about 102 KB. The vacation section is last, so
   its end and the footer are the part that is cut. A real day with more meat
   cuts on sale is larger.
2. **The vacation packages table is wider than a phone screen.** At 375 px the
   whole email becomes 500 px wide, so every section is cut off or shrunk. The
   "מה כלול" column is 69 px wide and wraps one word per line; a package row
   averages 203 px.
3. **Conditional prices are ranked as plain prices.** A price that needs a ₪99
   basket, or a 2 kg purchase, takes the highlighted first row above a price
   with no condition. The condition is visible only as raw feed text
   (`מעל 99 ש"ח פרגיות 39.90 לקג מוג 2`).
4. **A real "1+1" deal can vanish.** When the feed gives no price and no rate,
   the row is dropped and counted nowhere.
5. **No summary of what matters.** The email is 182 table rows (95 meat, 60
   vacation, 22 Sims, 5 creatine) and 19,651 px tall on a phone, about 24
   screens. Meat starts 2,259 px down and vacations 9,707 px down. Nothing
   marks what is new since yesterday.

## 2. QA and bug findings

### 2.1 Pricing and calculation

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q1 | A price with a spend threshold or a minimum quantity ranks and is highlighted as a plain price. | `מעל 99 ש"ח פרגיות 39.90 לקג מוג 2` → row 1, ₪39.90, −39%, above an unconditional ₪52.90 | Confirmed |
| Q2 | Weighed multi-buy deals lose the "buy N" line. `deal_text` is set only for packaged items. | `כרעיים 2 קג ב 40` → price cell `₪20.00  ₪24.90 −20%`, no "2 ק״ג ב-₪40" | Confirmed |
| Q3 | "1+1" with no `DiscountedPrice` and no `DiscountRate` is dropped without a count. Its category is then hidden when it has no other deal. | `אסאדו 1+1`, `MinQty` 2 → no row, not in `unpriced_by_category` | Confirmed |
| Q4 | "1+1" published as `DiscountRate` 50 shows −50% per kg with no sign that two units are needed. | `אסאדו 1+1` → `₪39.95  ₪79.90 −50%` | Confirmed |
| Q5 | One item can take several rows of a five-row table: once per promotion, and once more at the regular price when another branch of the same chain has no promotion. | `כתף בקר טרי`, one chain: ₪59.90 (own promo) and ₪67.41 (10% department promo). `חזה עוף טרי`: ₪29.90 at branch A and ₪39.90 "מחיר רגיל" at branch B | Confirmed |
| Q6 | "החבילה הכי זולה" compares totals for different party sizes. | A $327 package for one adult wins over $578 for a couple; the column shows `למבוגר אחד` next to `לזוג` rows | Confirmed on `main`; branch `feat/couples-per-night` (not merged) changes this |
| Q7 | Per-person vacation prices have no thousands separator; package totals do. | `$1390 לאדם` / `מחיר החבילה: $2,780` | Confirmed |
| Q8 | A `DiscountRate` of 100 or more is ignored. If a chain publishes hundredths of a percent (2310 = 23.10%), its rate-only promotions are dropped. | `0 < discount_rate < 100` in `_per_unit_promo_price` | To verify against a real feed |

### 2.2 Parsing artifacts

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q9 | Condition tags are printed as they arrive, in item names and in the note line. No code looks for them. | `חזה עוף טרי מוג 3`, `כרעיים עוף טרי 2 קג ב 40 ע.מלאי`, `אנטריקוט טרי 2 ב110 ס.בודדים`, `ת.קצר בשר טחון בקר טרי 500 גרם` | Confirmed |
| Q10 | The note line repeats the item name unless the two strings are identical. | `פילה מדומה בקר טרי מס 6` / `פילה מדומה מס 6 טרי` | Confirmed |
| Q11 | Text is cut mid-word with no ellipsis: item name and note at 60 characters, site promo condition at 300 (200 in text), creatine name at 70, package "includes" item at 80, flight details at 90. | 61-character name → `…על האש פרימיו`; `…ללא מוצרי הום, משקפי` | Confirmed |
| Q12 | A site promotion's own expiry text is not read, so an expired offer can be listed. | Terminal X fixture: `בתוקף עד 11.11 בחצות` | To verify on a live page |
| Q13 | A site with only an "up to N%" sale shows "לא זוהה מבצע כלל-אתרי". Correct by the current rule, but it reads as "no sale". | `עד 70% הנחה על הסייל` → no promo | Confirmed |

### 2.3 Layout and rendering

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q14 | HTML is over Gmail's clipping limit. | 103,355 bytes: meat 42,799, vacations 40,285, Sims 10,056, creatine 2,335, CSS 2,296 | Confirmed (size measured; the limit is Gmail's documented ~102 KB) |
| Q15 | The packages table forces the email wider than a phone. | 375 px viewport → document 500 px wide; 384 px with the vacation tables hidden. Columns 19/105/94/69/70/63 px | Confirmed in Chromium |
| Q16 | Package rows are very tall on a phone. | 30 rows = 6,486 px, average 203 px. The vacation section is 9,944 px, 51% of the email | Confirmed |
| Q17 | Small text and small tap targets. | 602 text elements below 12 px, smallest 10 px. The "לצפייה בדיל" button is 93 × 22 px | Confirmed |
| Q18 | The body has no date. | Header is "דוח יומי" only | Confirmed |
| Q19 | The subject is too long to read in an inbox list. | 130 characters; a phone shows about 40 | Confirmed |
| Q20 | The plain-text part repeats every row. | 784 lines, 52,561 bytes | Confirmed |
| Q21 | No `color-scheme` declaration; light colours are hard-coded. | `<head>` has only the viewport tag and two `<style>` blocks | To verify in dark-mode clients |
| Q22 | Empty states differ. | Sims, meat, vacations not scanned: grey "(לא נסרק)". Creatine not scanned: red failure box | Confirmed |
| Q23 | Summary counts do not match what is shown. | "134 דילים" above 60 rows; "127 מוצרי בשר" above 95 rows | Confirmed |

Not a defect: RTL direction is set on every table, prices are wrapped in LTR
spans, meat tables fit a phone on their own (columns 143/193/84 px, rows 64 px
on average), and the desktop layout at 800 px has no overflow.

## 3. UX and content curation critique

**The email still answers "what did the scanners find", not "what should I
look at today".**

1. **Open with "היום שווה לדעת": at most five lines.** One line each: what,
   where, price, why it qualifies (new today, cheaper than yesterday, ends
   tomorrow). Pick across all sections. Everything below is reference.
2. **Make conditions part of the price.** Parse the tags once and show them as
   badges beside the price. A conditional price never takes the highlighted
   row from an unconditional one unless it is marked.

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
   every day and never read; vacations keep no state at all.
4. **Rebuild the vacation row for a phone.** Two lines per deal instead of a
   six-column table: destination, dates and nights on the first; price per
   person, package total and a short "included" strip on the second
   (flight · hotel 4★ · breakfast · luggage). The site name becomes the link.
5. **Move the explanations out of the way.** The meat card carries a
   60-word ranking note; the vacation card carries four paragraphs and a
   five-item legend, printed twice. Put one "איך לקרוא את הדוח" block at the
   bottom.
6. **Bridge the five topics.** A status strip under the header, one chip per
   section ("בשר: 12 חדשים", "חופשות: מ-$289", "סימס: ללא שינוי"). Order the
   sections by what changed today instead of a fixed order that puts meat
   three screens down.
7. **One row per item per chain.** Keep the cheapest price a shopper can get
   for an item in a chain, and say "בסניף X בלבד" when it is not chain-wide.

## 4. Backlog

Priority order: BUG-02, BUG-18, BUG-19, BUG-05, BUG-03, UX-01, UX-10, then the
rest. Keys carried over from 2026-11-12 keep their number; new keys start at
BUG-18 and UX-10.

---

**Issue Key / ID:** BUG-02
**Title:** Daily email exceeds Gmail's size limit and is clipped
**Type:** Bug Fix
**Priority:** P0 - Blocker
**Context & Current Behavior:** The fixture email is 103,355 bytes (19 meat tables, 30 packages, 30 flights, 22 Sims packs). Moving styles into `<style>` blocks helped, but the vacation section added 40,285 bytes. The vacation section and the footer are last and are what Gmail hides behind "View entire message".
**Expected Behavior:** The HTML part stays under 90 KB on a full day, so nothing is hidden.
**Acceptance Criteria (AC):**
- A test renders a full-size report (40 meat tables of 5 rows, 35 packages, 35 flights, 30 Sims packs on sale) and asserts `len(html_body.encode()) < 90_000`.
- When the content would exceed the budget, the lowest-priority rows are cut and a line says how many were left out.
- The footer is present in the first 90 KB.

---

**Issue Key / ID:** BUG-18
**Title:** Vacation packages table makes the whole email wider than a phone screen
**Type:** Bug Fix
**Priority:** P0 - Blocker
**Context & Current Behavior:** The six-column `.vt` packages table has `white-space:nowrap` on the dates, prices, badges and the deal button. At a 375 px viewport the document is 500 px wide (384 px with the vacation tables hidden), so every section is cut off or scaled down. Columns are 19/105/94/69/70/63 px; the "מה כלול" cell wraps one word per line and a row averages 203 px.
**Expected Behavior:** The email is no wider than the screen at 360 px and up. A package row is at most about 110 px tall on a phone.
**Acceptance Criteria (AC):**
- At 360 px and 375 px, `document.documentElement.scrollWidth` equals the viewport width.
- No cell renders narrower than 90 px with wrapped text.
- Average package row height at 375 px is 110 px or less.
- The desktop layout at 800 px shows the same information.

---

**Issue Key / ID:** BUG-19
**Title:** Conditional meat prices are ranked and highlighted as plain prices
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** `_price_rank_key` sorts by price per kg only. `מעל 99 ש"ח פרגיות 39.90 לקג מוג 2` takes row 1 with the green highlight, above an unconditional ₪52.90. For weighed items `MeatPromo.deal_text` is empty, so `כרעיים 2 קג ב 40` shows `₪20.00` with no "2 ק״ג" line.
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
**Context & Current Behavior:** `אסאדו 1+1` with `MinQty` 2 and no `DiscountedPrice` or `DiscountRate`: `_per_unit_promo_price` returns `None`, the row is dropped and no counter records it. The same deal published as `DiscountRate` 50 renders `₪39.95  ₪79.90 −50%` with nothing saying two units are needed.
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
**Context & Current Behavior:** No code recognises the tags. The email shows `חזה עוף טרי מוג 3`, `כרעיים עוף טרי 2 קג ב 40 ע.מלאי`, `אנטריקוט טרי 2 ב110 ס.בודדים`, `ת.קצר בשר טחון בקר טרי 500 גרם`, `עוף שלם טרי מוג 2 יח ע.מלאי`.
**Expected Behavior:** One function takes the item name and the promotion description and returns a clean name plus a list of conditions (limit, while stocks last, short expiry, selected branches, minimum basket, multi-buy). The tags are removed from the displayed text.
**Acceptance Criteria (AC):**
- `מוג 3` / `מוגבל ל 3` → limit 3; `מוג 4 קג` → limit 4 kg; `מוג 2 יח` → limit 2 units.
- `ע.מלאי` / `עד גמר המלאי` → while stocks last. `ת.קצר` → short expiry. `ס.בודדים` → selected branches.
- `מעל 99 ש"ח` → minimum basket ₪99.
- None of these strings appears in the rendered HTML or text for the fixture rows above.
- A name with no tag is returned unchanged.
- Unit tests cover each tag at the start, middle and end of a string.

---

**Issue Key / ID:** UX-03
**Title:** Show promotion conditions as badges
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** Conditions appear only inside the grey note line, mixed with the repeated item name: `אנטריקוט טרי 2 ב110 ס.בודדים · עד 18/11`.
**Expected Behavior:** Each condition from BUG-03 renders as a short Hebrew badge under the price or the item: "מוגבל ל-3", "עד גמר המלאי", "תוקף קצר", "בסניפים נבחרים", "בקנייה מעל ₪99", "2 ב-₪110", "1+1".
**Acceptance Criteria (AC):**
- Every condition type has one fixed label; the mapping is in one table in the code.
- A row with three conditions fits two lines at 375 px.
- Badges add no more than 60 bytes per row to the HTML (class-based, no inline styles).
- The plain-text part lists the same labels in brackets.

---

**Issue Key / ID:** UX-01
**Title:** Add a "היום שווה לדעת" highlights block at the top
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** The email opens with the site promotions list. Meat starts 2,259 px down on a phone and vacations 9,707 px down. There are 182 table rows and no ranking across sections.
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
**Context & Current Behavior:** Packages use six columns; flights use five. On a phone the "מה כלול" bullets wrap one word per line (`• טיסה / הלוך ושוב / • Grand / Resort & / Spa / ★★★★ / • ארוחת / בוקר`), the luggage line is bold in every row, and the 30 package rows are 6,486 px tall.
**Expected Behavior:** Two lines per deal. Line 1: destination, dates, nights. Line 2: price per person, package total, and a short "included" strip (flight · hotel name and stars · board · transfers · luggage). The site name is the link.
**Acceptance Criteria (AC):**
- 30 package rows are 3,300 px or less at 375 px.
- The hotel name and its stars stay on one line when they fit in 30 characters.
- Luggage is bold only when it is missing or unknown.
- The link target is at least 44 px tall.
- HTML per vacation row is 450 bytes or less (it is about 670 today).

---

**Issue Key / ID:** UX-04
**Title:** Collapse sections that did not change; mark what is new
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** The Sims 4 list (22 rows, 10 KB) and the creatine top five are printed in full every day even when the section says "ללא שינוי". `supermarkets.persist` writes `supermarkets-state.json` and nothing reads it. Vacations keep no state, so a deal listed for a week looks the same as one that appeared today.
**Expected Behavior:** Unchanged Sims and creatine sections are one line each. Meat rows and vacation deals that are new or cheaper than yesterday carry a "חדש" or "ירד" mark; vacation deals unchanged since yesterday render as a compact line.
**Acceptance Criteria (AC):**
- Sims with no new, ended or changed discounts renders one line with the count.
- Creatine with `changed == False` renders one line with the product, price and link.
- A meat row whose (chain, item, price per kg) was not in yesterday's state has a "חדש" mark; a lower price has "ירד".
- Vacation deals are stored per run (source, destination, dates, price) and compared the same way.
- On the first run, with no state, nothing is marked.

---

**Issue Key / ID:** BUG-22
**Title:** "החבילה הכי זולה" compares packages priced for different party sizes
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** `_deal_badges` takes the lowest `d.total` in the table. A package priced for one adult ($327 `למבוגר אחד`) wins over $578 `לזוג`. The "מחיר החבילה" column mixes both. Branch `feat/couples-per-night` (commit `cb47cee`, not merged) restricts packages to couples.
**Expected Behavior:** Totals are compared only between packages for the same party. A package for another party size is labelled and cannot win the badge.
**Acceptance Criteria (AC):**
- With a one-adult $327 package and a couple's $578 package in one table, the badge goes to the couple's package, or no badge is given.
- If `feat/couples-per-night` is merged, a test confirms no non-couple package reaches the table.
- The legend text matches the rule.

---

**Issue Key / ID:** BUG-20
**Title:** One item can fill several rows of a five-row cut table
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** `_finalize_report` dedupes on (chain, promotion description, price per kg). `כתף בקר טרי` in one chain appears at ₪59.90 (its own promotion) and at ₪67.41 (10% department promotion). `חזה עוף טרי` appears at ₪29.90 for branch A and again at ₪39.90 "מחיר רגיל" for branch B of the same chain.
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
**Context & Current Behavior:** Slices `[:60]` (meat name and note), `[:300]` / `[:200]` (site promo condition), `[:70]` (creatine name), `[:80]` (package item), `[:90]` (flight details). A 61-character name ends `…על האש פרימיו`; a promo condition ends `…ללא מוצרי הום, משקפי`.
**Expected Behavior:** One helper cuts at a word boundary and appends "…". HTML rows carry the full text in a `title` attribute only where the size budget allows.
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
**Context & Current Behavior:** The fixture subject is 130 characters: `דוח יומי: עד 30% הנחה ב-MyProtein · 22 חבילות סימס 4 במבצע · מבצעי בשר ב-5 רשתות (הרצליה, רמת השרון) · חבילות מ-$289 · טיסות מ-$89`. There is no preheader, and the header reads "דוח יומי" with no date.
**Expected Behavior:** The subject names the one or two most important items in 60 characters or fewer; the rest goes into a hidden preheader. The header shows the date.
**Acceptance Criteria (AC):**
- Subject length is 60 characters or fewer for the full fixture.
- A failed section is still named in the subject.
- A hidden preheader of 90 characters or fewer follows `<body>`.
- The header shows the day and date in Hebrew ("יום שישי, 13.11.2026"); the text part's first line does too.

---

**Issue Key / ID:** UX-06
**Title:** Status strip under the header; order sections by what changed
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** Five unrelated topics run in a fixed order: site promotions, creatine, Sims 4, meat, vacations. Each has its own card style. The reader cannot see which sections have news without scrolling 24 screens.
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
**Context & Current Behavior:** The meat summary card holds a 60-word ranking note. The vacation card holds four paragraphs (windows per continent, price definitions, "מה כלול" definition, unmapped note), and the five-badge legend with full definitions is printed above both the packages and the flights table.
**Expected Behavior:** Each section keeps one short line of scope. Definitions and ranking rules sit once in a block at the bottom.
**Acceptance Criteria (AC):**
- No section intro is longer than 25 words.
- The badge legend appears once.
- Notes that report a problem today (failed chain, unavailable site, unpriced deals) stay in their section.

---

**Issue Key / ID:** BUG-23
**Title:** Summary counts do not match the rows shown
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** The vacation card says "134 דילים" (`total_found`, before the continent and top-N cuts) above 60 rows. The meat card says "127 מוצרי בשר" above 95 rows.
**Expected Behavior:** The headline number is the number shown, with the scanned total beside it: "60 דילים מוצגים מתוך 134".
**Acceptance Criteria (AC):**
- The bold count equals the number of rows rendered in that section.
- The scanned total appears as "מתוך N" only when it is larger.

---

**Issue Key / ID:** BUG-10
**Title:** Inconsistent number formatting in vacation prices
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** `_deal_price` prints `$1390`; `_deal_total` prints `$2,780`. The subject uses the first form.
**Expected Behavior:** All vacation amounts use a thousands separator and no decimals.
**Acceptance Criteria (AC):**
- 1390 renders `$1,390` in the HTML, the text part and the subject.
- 289 renders `$289`.

---

**Issue Key / ID:** BUG-07
**Title:** Confirm the scale of `DiscountRate` in each chain's feed
**Type:** Parser Refactor
**Priority:** P3 - Low
**Context & Current Behavior:** Promotion-level fields are now read. A `DiscountRate` of 100 or more is still ignored (`0 < discount_rate < 100`), so a chain that publishes 2310 for 23.10% would lose its rate-only promotions without a log line.
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
**Context & Current Behavior:** 784 lines and 52,561 bytes for the fixture; four or five lines per deal, with `₪` and digits inside RTL lines and no direction marks.
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

## 5. Status of the 2026-11-12 tickets

| Key | Title (short) | Status on `main` @ `033caa6` |
|---|---|---|
| BUG-01 | Multi-buy shows bundle total | Fixed: `MinQty` read, price per unit |
| BUG-02 | Gmail clipping | Open: 103,355 bytes after the vacation section was added |
| BUG-03 | Raw condition tags | Open |
| BUG-04 | Prices have no unit | Fixed: all meat prices per kg, stated in the table header |
| BUG-05 | "1+1" and percent-only promos | Partly fixed: percent-only now priced; "1+1" open |
| BUG-06 | Payment-method and legal credits listed | Fixed in code (`_NOT_A_PROMO_PAT`) |
| BUG-07 | Feed field scale and location | Partly fixed: promotion-level fields read; scale not verified |
| BUG-08 | Substring matching misfiles cuts | Fixed: word patterns per species |
| BUG-09 | Text cut mid-word | Open |
| BUG-10 | Inconsistent formatting | Meat fixed; vacation amounts open |
| BUG-11 | Page source in site promo details | Fixed: only visible text is scanned |
| BUG-12 | "Up to N%" shown as flat N% | Fixed: "up to" offers are not listed |
| BUG-13 | Meat table unreadable on a phone | Fixed for meat; the vacation table reintroduces it (BUG-18) |
| BUG-14 | Failed chains invisible | Fixed: failed and empty chains are named |
| BUG-15 | Empty states differ | Open |
| BUG-16 | Plain-text dump | Open (393 → 784 lines) |
| BUG-17 | No dark-mode declaration | Open |
| UX-01 | Highlights block | Open |
| UX-02 | Meat by cut, top rows | Done (five per cut); brand variants merged per promotion |
| UX-03 | Condition badges | Open |
| UX-04 | Collapse unchanged, mark new | Open |
| UX-05 | Subject, preheader, date | Open (subject now 130 characters) |
| UX-06 | Summary strip, common pattern | Open |
| UX-07 | End date and branch | Done |
| UX-08 | Badge on merit | Meat: done by price per kg, see BUG-19. Vacations: see BUG-22 |
| UX-09 | Per-row category line and icons | Done |
