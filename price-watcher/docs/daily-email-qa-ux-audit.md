# Daily deals email: QA and UX audit

Date: 2026-11-15. Scope: the combined daily email ("דוח יומי") built by
`build_daily_payload` in `price-watcher/src/price_watcher/email_sender.py`,
on `main` @ `a158dfd`. This replaces the audit of 2026-11-14 (commit `65ffea8`
on branch `ccr-9ce5ad42-rltlel`, never merged; `main` still held the
2026-11-12 version). Section 6 lists what happened to each of yesterday's
tickets.

Changes on `main` since yesterday's audit: beef bones and marrow in the meat
scan (#64). Open and not merged: online butchers in the meat tables (#65),
previewed in section 5.

## How this audit was done, and its limits

- **No email was attached to the audit request.** The audit is of the code
  that generates the email.
- **No live data.** The email was rendered from fixtures pushed through the
  real pipeline:
  - Meat: PriceFull / PromoFull XML for 7 chains (Carrefour included) and 12
    branches in Herzliya and Ramat HaSharon, 38 items per branch, through
    `_scan_chain`, `_scan_store` and `scan_and_report`. Item names and
    promotion texts carry the tags named in the request (`מוג 3`, `ע.מלאי`,
    `ת.קצר`, `ס.בודדים`, `2 ב110`, `מעל 99 ש"ח`). One chain fails and one has
    no branch in the area.
  - Vacations: 280 deals from 7 of the 8 sources (the eighth fails) through
    `vacations.scan_and_report`, giving 22 packages and 30 flights.
  - Site promotions and creatine: the repository's test fixtures through
    `extract_promos` and `_parse_cells`. Sims 4: 22 packs on sale.
- **The fixture is invented data.** It tests behaviour. It says nothing about
  which real deals are good or how varied a real day is.
- **Today's fixture is not yesterday's.** Earlier fixtures were not kept, so
  size and height figures are not comparable day to day. Today's is saved in
  `docs/audit-fixture/` (`render.py`, `measure.js`) so the next audit can
  reuse it.
- Layout was measured in Chromium at 360, 375 and 800 px. Mail clients differ.
- Findings are marked **Confirmed** (reproduced with the fixture or measured)
  or **To verify** (read from the code, or depends on what a real feed
  publishes).
- The test suite passes (1,319 tests on `main`; 1,329 on the #65 branch).
- Per the constraint, nothing here touches the sending schedule or frequency.

## 1. Executive summary

**Verdict: unchanged from yesterday. The arithmetic is right, but a phone
reader gets a page wider than the screen, 27 screens long, over Gmail's
clipping size, with nothing that says what is worth opening.** No ticket from
yesterday was fixed; the only change on `main` was the bones category.

Critical failure points today:

1. **The email is wider than a phone.** At 360 and 375 px the page is 483 px
   wide. The packages table is the main cause (437 px without it), the flights
   table the second (385 px without both), and the creatine card alone keeps
   the page at 385 px.
2. **The email is over Gmail's clipping size.** 111,702 bytes against a limit
   of about 102 KB. 72,327 bytes come before the vacation heading, so most of
   the vacation section and the footer are what Gmail hides.
3. **Conditional prices lead the tables.** In 18 of 29 meat tables the
   highlighted cheapest row has a condition (basket minimum, quantity limit,
   multi-buy, short expiry, selected branches). 17 of them show it only as raw
   feed text.
4. **A real "1+1" deal can vanish** when the feed gives no price and no rate.
   Nothing counts it.
5. **No summary.** 223 table rows (145 meat, 52 vacation, 22 Sims, 4
   creatine), 21,655 px tall on a phone. Meat starts 2,603 px down and
   vacations 12,882 px down. 100 of the 145 meat rows are regular prices, not
   deals. Nothing marks what is new since yesterday.

New today:

- **Soup bones are filtered out of the new bones table.** `עצמות בקר למרק
  קפוא` is rejected by the meat filter because of the word "מרק", so a deal on
  it never reaches "עצמות בקר / מח עצם".
- **"הכי ארוך" and "הכי קצר" mark one of several equal rows.** With trips held
  to 3–5 nights, 6 of 15 Europe packages are 5 nights and 6 are 3 nights; one
  of each gets the badge.
- **The creatine card does not fit a 360 or 375 px screen.**
- **A "buy 2 kg or more at ₪19.90" promotion is shown as ₪9.95 per kg**,
  highlighted, −62%. Whether a real feed publishes that shape is to verify.
- **If #65 merges as it is**, the fixture email grows to 132,074 bytes and 33
  phone screens, and Meat Point is listed every day in red as a chain "without
  deals".

## 2. QA and bug findings

### 2.1 Pricing and calculation

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q1 | A price with a spend threshold or a quantity limit ranks and is highlighted as a plain price. | `מעל 99 ש"ח פרגיות 39.90 לקג מוג 2` → row 1, ₪39.90, −39%, above an unconditional ₪49.90. 18 of 29 highlighted rows carry a condition | Confirmed |
| Q2 | Weighed multi-buy deals lose the "buy N" line. `deal_text` is set only for packaged items. | `בשר בקר טחון טרי 2 קג ב 100` → `₪50.00  ₪57.90 −14%`; `כרעיים עוף טרי 2 קג ב 40` → ₪20.00 in four chains; `כנפיים עוף טרי 3 קג ב 40` → ₪13.33. No "N ק״ג ב-₪…" line | Confirmed |
| Q3 | "1+1" with no `DiscountedPrice` and no `DiscountRate` is dropped without a count. | `אסאדו 1+1`, `MinQty` 2 → no row; `unpriced_by_category` is empty | Confirmed |
| Q4 | "1+1" published as `DiscountRate` 50 shows −50% per kg with no sign that two units are needed. | `אסאדו 1+1` → `₪43.45  ₪86.90 −50%`, highlighted | Confirmed |
| Q5 | One item can take two rows of a five-row table. | "פילה מדומה בקר טרי מס 6", one chain: ₪72.90 (own promotion) and ₪77.31 (10% department promotion), rows 1 and 2 | Confirmed |
| Q6 | On equal price the highlighted row is the one with the largest percentage, which is the chain with the highest regular price. | `כרעיים 2 קג ב 40` at ₪20.00 in four chains: row 1 is the chain whose regular price is ₪25.90 (−23%); the chain at ₪22.90 regular is row 4 | Confirmed (new) |
| Q7 | A promotion that needs `MinQty` 2 and states a per-kg price is divided by the quantity. | `בקניית 2 קג ומעלה שוקיים 19.90`, `DiscountedPrice` 19.90, `MinQty` 2 → `₪9.95  ₪25.90 −62%`, highlighted. The note line under it says 19.90 | Confirmed with the fixture; whether a chain publishes this shape is to verify (new) |
| Q8 | The packages table is sorted by package total while the first price column is per night. | Rows 1–7: `$165, $140, $156, $280, $185, $326, $365` per night under "בכל יבשת מהזול ליקר" | Confirmed |
| Q9 | Flight prices of 1,000 and more have no thousands separator; package prices do. | `$1184 לאדם`, `$1350 לאדם` (flights) / `$2,670 לזוג` (package) | Confirmed |
| Q10 | A `DiscountRate` of 100 or more is ignored without a log line. The item is then listed at its regular price. | `DiscountRate` 2310 on "שייטל בקר טרי" → row "מחיר רגיל" ₪86.90 | Confirmed with the fixture; whether any chain publishes this scale is to verify |
| Q11 | Vacation deals removed by the trip rules are counted but not reported. | `dropped_length` 95, `dropped_party` 36, `dropped_low_star` 9; `email_sender.py` reads none of them | Confirmed |
| Q12 | "הכי ארוך" / "הכי קצר" go to the first of several equal rows, and a two-row table carries four badges. | Europe packages: 6 rows of 5 nights, 6 rows of 3 nights, one badge each. North America packages: 2 rows, 4 badges ("הכי ארוך" on a 4-night trip) | Confirmed (new) |
| Q13 | "הכי משתלם" can sit on a row that costs twice the cheapest per night. It means "cheapest per night among rows with luggage"; the label does not say so. | Europe: badge on `$280 ללילה`; the table also shows `$140`, `$156`, `$165` (no luggage or not stated) | Confirmed; working as designed (new) |

### 2.2 Parsing artifacts and classification

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q14 | Condition tags are printed as they arrive. No code looks for them. | 23 of 145 meat rows (23 of the 45 deal rows): `סינטה בקר טרי מוג 2 קג ס.בודדים ע.מלאי ת.קצר`, `אנטריקוט טרי 2 ב110 ס.בודדים`, `ת.קצר בשר טחון בקר טרי 500 גרם`, `עוף שלם טרי מוג 2 יח ע.מלאי` | Confirmed |
| Q15 | The note line repeats the item name unless the two strings are identical. | `פילה מדומה בקר טרי מס 6` / `פילה מדומה מס 6 טרי`; `גרון הודו קפוא` / `גרון הודו קפוא ס.בודדים`; `פיקניה בקר טרי` / `פיקניה בקר טרי מבצע` | Confirmed |
| Q16 | Text is cut mid-word with no ellipsis: meat name and note at 60 characters, site promo condition at 300 (200 in text), creatine name at 70, package item at 80, flight details at 90. | `…ללא מוצרי הום, משקפי` (Terminal X); `…Unflavored, 1` (creatine). The meat slices are read from the code | Confirmed |
| Q17 | Raw soup bones are rejected by the meat filter added in #64. | `_is_meat_product("עצמות בקר למרק קפוא")` and `("עצמות עוף למרק")` → False. `עצמות בקר קפוא` and `עצמות מוח בקר קפוא` → True. The fixture promotion `עצמות בקר למרק קפוא` at ₪9.90 is in no table | Confirmed with the classifier; how the chains name soup bones is to verify (new) |
| Q18 | Oxtail has no category. It lands in "בקר – כללי", which is counted and not shown. | `זנב בקר מח. טרי` on promotion → "1 מבצעים בקבוצות כלליות … לא מוצגים" | Confirmed (new, minor) |
| Q19 | A site promotion's own expiry text is not read. | Terminal X fixture: `בתוקף עד 11.11 בחצות`, listed as active on 15.11 | To verify on a live page |
| Q20 | A site with only an "up to N%" sale shows "לא זוהה מבצע כלל-אתרי". | Correct by the current rule, but reads as "no sale" | Confirmed |
| Q21 | The creatine list is titled "5 הזולים ביותר" whatever its length, and its first row repeats the product in the card above it. | Four rows under "5 הזולים ביותר ל-100 גרם" | Confirmed |

### 2.3 Layout and rendering

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q22 | HTML is over Gmail's clipping limit. | 111,702 bytes: meat tables 48,114 (29 tables, 1,659 each), vacation tables 35,433 (681 per row), Sims 10,176, site promotions 5,173, creatine 2,820. 72,327 bytes come before the vacation heading | Size confirmed; the limit is Gmail's documented ~102 KB, to verify in Gmail |
| Q23 | The vacation tables force the email wider than a phone. | 360 and 375 px viewport → page 483 px wide; still 483 px at 412 px. Package columns 19/105/92/66/59/62 px | Confirmed in Chromium |
| Q24 | The creatine card is wider than a phone. | With both vacation tables hidden the page is 385 px at 360 and 375 px; hiding the creatine card brings it to 360 px | Confirmed in Chromium (new) |
| Q25 | Package rows are tall on a phone. | 22 rows = 5,095 px, average 232 px, tallest 347 px. The "מה כלול" column is 66 px and wraps one word per line. Flights average 80 px | Confirmed |
| Q26 | English promotion text is reordered in the right-to-left card. The condition `<div>` has no `dir="auto"`. | `30% OFF EVERYTHING` → `OFF EVERYTHING 30%` | Confirmed in Chromium |
| Q27 | Small text and small tap targets. | 791 of 1,676 text elements are below 12 px, smallest 10 px. 83 of 85 links are under 44 px tall. The "לצפייה בדיל" button is 93 × 22 px | Confirmed |
| Q28 | The body has no date. | Header is "דוח יומי" only | Confirmed |
| Q29 | The subject is too long for an inbox list. | 156 characters; a phone shows about 40 | Confirmed |
| Q30 | The plain-text part repeats every row. | 892 lines, 57,500 bytes; 257 lines for vacations, each deal with a full tracking URL | Confirmed |
| Q31 | No `color-scheme` declaration; light colours are hard-coded. | `<head>` has the viewport tag and two `<style>` blocks | To verify in dark-mode clients |
| Q32 | Empty states differ. | Sims, meat, vacations not scanned: grey "(לא נסרק)". Creatine not scanned: red failure box | Confirmed |
| Q33 | Summary counts do not match the rows shown. | "126 דילים" above 52 rows; "227 מוצרי בשר" above 145 rows, 100 of them at "מחיר רגיל" | Confirmed |

Not a defect: every table sets RTL, prices are wrapped in LTR spans, the
packaged multi-buy line reads in the right order (`מבצע 2 ב-₪60.00`), failed
and empty chains are named, club-only promotions are filtered out, pet bones
and bone stock are kept out of the bones table, a bare "מח." is not read as
marrow, and the desktop layout at 800 px has no overflow.

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
5. **Tell deals from regular prices at a glance.** 100 of 145 meat rows are
   regular prices that are in the table because they are cheap. In 16 of the
   29 tables four of the five rows are regular prices. Keep the ranking, but
   give deal rows a mark so the eye finds them, and cut a table to its deals
   plus the one cheapest regular price when space is short.
6. **Move the explanations out of the way.** The meat card carries a 60-word
   ranking note and a paragraph listing all 29 category names. The vacation
   card carries three paragraphs, and the badge legend with full definitions
   is printed above both vacation tables. Put one "איך לקרוא את הדוח" block at
   the bottom.
7. **Bridge the five topics.** A status strip under the header, one chip per
   section ("בשר: 12 חדשים", "חופשות: מ-$660 לזוג", "סימס: ללא שינוי"). Order
   the sections by what changed today instead of a fixed order that puts meat
   three screens down and vacations sixteen.
8. **One row per item per chain.** Keep the cheapest price a shopper can get
   for an item in a chain, and say "בסניף X בלבד" when it is not chain-wide.
9. **Give a badge only when it separates rows.** No "longest" or "shortest"
   when several rows tie or the table has fewer than four rows. Name "הכי
   משתלם" for what it measures ("הכי זול ללילה עם כבודה").

## 4. Backlog

Priority order: BUG-18, BUG-02, BUG-19, BUG-05, BUG-03, UX-01, UX-10, then the
rest. Keys carried over keep their number; new keys start at BUG-28.

---

**Issue Key / ID:** BUG-18
**Title:** Vacation tables make the whole email wider than a phone screen
**Type:** Bug Fix
**Priority:** P0 - Blocker
**Context & Current Behavior:** The six-column `.vt` packages table has `white-space:nowrap` on the dates, prices, badges and the deal button. At 360, 375 and 412 px the document is 483 px wide (437 px with the packages table hidden, 385 px with both vacation tables hidden), so every section is cut off or scaled down. Package columns are 19/105/92/66/59/62 px. The "מה כלול" cell wraps `• מלון / Hilton / Garden / Inn City / Centre / (4.5★)`; a row averages 232 px and the tallest is 347 px.
**Expected Behavior:** The email is no wider than the screen at 360 px and up. A package row is at most about 110 px tall on a phone.
**Acceptance Criteria (AC):**
- At 360 px and 375 px, `document.documentElement.scrollWidth` equals the viewport width, with the vacation tables present.
- No cell renders narrower than 90 px with wrapped text.
- Average package row height at 375 px is 110 px or less.
- The desktop layout at 800 px shows the same information.

---

**Issue Key / ID:** BUG-02
**Title:** Daily email is over Gmail's size limit and is clipped
**Type:** Bug Fix
**Priority:** P0 - Blocker
**Context & Current Behavior:** The fixture email is 111,702 bytes with 29 meat tables, 22 packages, 30 flights and 22 Sims packs. A meat table averages 1,659 bytes and a vacation row 681 bytes. 72,327 bytes come before the vacation heading, so the vacation section and the footer are what Gmail hides behind "View entire message". With #65 the same fixture is 132,074 bytes.
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
**Context & Current Behavior:** `_price_rank_key` sorts by price per kg, then by the larger percentage. `מעל 99 ש"ח פרגיות 39.90 לקג מוג 2` takes row 1 with the green highlight, above an unconditional ₪49.90. For weighed items `MeatPromo.deal_text` is empty, so `בשר בקר טחון טרי 2 קג ב 100` shows `₪50.00` with no "2 ק״ג" line. In the fixture 18 of 29 highlighted rows have a condition. On a tie (`כרעיים 2 קג ב 40`, ₪20.00 in four chains) the highlight goes to the chain with the highest regular price.
**Expected Behavior:** A price that needs a basket total, a minimum quantity or a club card shows the condition beside the price. On equal price, the unconditional row comes first. The highlight says when the price is conditional.
**Acceptance Criteria (AC):**
- `כרעיים 2 קג ב 40` (weighed) renders "2 ק״ג ב-₪40" under ₪20.00.
- A promotion whose text has `מעל N ש"ח` / `בקנייה מעל` renders "בקנייה מעל ₪N" beside the price.
- A promotion with `בקניית 2 קג ומעלה` renders "מ-2 ק״ג".
- With a conditional ₪39.90 and an unconditional ₪39.90 in one table, the unconditional row is first.
- Rows equal in price and in conditions are ordered by chain name, not by percentage.
- The plain-text part shows the same conditions.

---

**Issue Key / ID:** BUG-05
**Title:** "1+1" promotions are dropped or shown without the "buy two" condition
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** `אסאדו 1+1` with `MinQty` 2 and no `DiscountedPrice` or `DiscountRate`: `_per_unit_promo_price` returns `None`, the row is dropped and no counter records it. The same deal published as `DiscountRate` 50 renders `₪43.45  ₪86.90 −50%`, highlighted, with nothing saying two units are needed.
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
**Context & Current Behavior:** No code recognises the tags. 23 of the 45 deal rows in the fixture show them: `חזה עוף טרי מוג 3`, `כרעיים עוף טרי 2 קג ב 40 ע.מלאי`, `אנטריקוט טרי 2 ב110 ס.בודדים`, `ת.קצר בשר טחון בקר טרי 500 גרם`, `סינטה בקר טרי מוג 2 קג ס.בודדים ע.מלאי ת.קצר`.
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
**Context & Current Behavior:** Conditions appear only inside the grey note line, mixed with the repeated item name: `אנטריקוט טרי 2 ב110 ס.בודדים · עד 19/11`.
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
**Context & Current Behavior:** The email opens with the site promotions list. Meat starts 2,603 px down on a phone and vacations 12,882 px down. There are 223 table rows and no ranking across sections.
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
**Context & Current Behavior:** Packages use six columns; flights use five. On a phone the "מה כלול" bullets wrap one word per line in a 66 px column, the luggage line is bold in every row, and the 22 package rows are 5,095 px tall. The price cell reads `$165 / ללילה לזוג` beside `$660 / לזוג`.
**Expected Behavior:** Two lines per deal. Line 1: destination, dates, nights. Line 2: price per night, package total, and a short "included" strip (flight · hotel name and stars · board · transfers · luggage). The site name is the link.
**Acceptance Criteria (AC):**
- 22 package rows are 2,500 px or less at 375 px.
- The hotel name and its stars stay on one line when they fit in 30 characters; a longer name is cut at a word with "…".
- Luggage is bold only when it is missing or unknown.
- The link target is at least 44 px tall.
- HTML per vacation row is 450 bytes or less (681 today).

---

**Issue Key / ID:** UX-04
**Title:** Collapse sections that did not change; mark what is new
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** The Sims 4 list (22 rows, 10,176 bytes) and the creatine list are printed in full every day even when the section says "ללא שינוי". `supermarkets.persist` writes `supermarkets-state.json` and nothing reads it. Vacations keep no state, so a deal listed for a week looks the same as one that appeared today.
**Expected Behavior:** Unchanged Sims and creatine sections are one line each. Meat rows and vacation deals that are new or cheaper than yesterday carry a "חדש" or "ירד" mark; vacation deals unchanged since yesterday render as a compact line.
**Acceptance Criteria (AC):**
- Sims with no new, ended or changed discounts renders one line with the count.
- Creatine with `changed == False` renders one line with the product, price and link.
- A meat row whose (chain, item, price per kg) was not in yesterday's state has a "חדש" mark; a lower price has "ירד".
- Vacation deals are stored per run (source, destination, dates, price) and compared the same way.
- On the first run, with no state, nothing is marked.

---

**Issue Key / ID:** BUG-28
**Title:** Raw soup bones are filtered out of the bones table
**Type:** Parser Refactor
**Priority:** P2 - Medium
**Context & Current Behavior:** #64 added "עצמות" as a meat word and relies on the exclusion words "מרק", "ציר" and "כלב" to keep soup mixes, stock and pet bones out. The same exclusion rejects raw bones sold for soup: `_is_meat_product("עצמות בקר למרק קפוא")` and `("עצמות עוף למרק")` return False, while `עצמות בקר קפוא` passes. The fixture promotion `עצמות בקר למרק קפוא` at ₪9.90 per kg appears in no table and in no counter. How each chain names these items is not confirmed from a real feed.
**Expected Behavior:** A weighed or frozen item named "עצמות … למרק" is a bones item. Soup powder, stock and pet products stay out.
**Acceptance Criteria (AC):**
- `עצמות בקר למרק קפוא` (weighed) is classified as meat, in "עצמות בקר / מח עצם – קפוא".
- `עצמות עוף למרק` is classified as meat, in a chicken category or a counted catch-all, not dropped.
- `מרק עצמות`, `ציר עצמות בקר 500 מל`, `אבקת מרק בטעם בקר` and `עצם לעיסה לכלב` are still rejected.
- `classifier_cases.json` gains these cases.
- A PriceFull file from two chains is checked for how bones are named, and the result is noted in the test file.

---

**Issue Key / ID:** BUG-31
**Title:** A "buy N or more at X per kg" promotion is divided by N
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** `_per_unit_promo_price` always divides `DiscountedPrice` by `MinQty`. For `בקניית 2 קג ומעלה שוקיים 19.90` with `DiscountedPrice` 19.90 and `MinQty` 2 the row reads `₪9.95  ₪25.90 −62%`, highlighted as the cheapest, with "19.90" in the note line below it. `_desc_unit_price` catches a contradiction only for the "N ב X" form. Confirmed with the fixture; whether a chain publishes a per-kg `DiscountedPrice` with `MinQty` above 1 is to verify on real PromoFull files.
**Expected Behavior:** When the description states a per-unit price that equals `DiscountedPrice`, that price is used as it is and the minimum quantity is shown as a condition.
**Acceptance Criteria (AC):**
- The fixture row above renders ₪19.90 per kg, −23%, with "מ-2 ק״ג".
- `2 קג ב 40` with `DiscountedPrice` 40 and `MinQty` 2 still renders ₪20.00.
- A promotion whose description price matches neither reading is dropped and counted (BUG-05 counter).
- The check on real files is recorded next to BUG-07's.

---

**Issue Key / ID:** BUG-30
**Title:** Creatine card is wider than a phone screen
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** With both vacation tables hidden the page is still 385 px wide at 360 and 375 px. Hiding the creatine card brings it to 360 px. The card's "5 הזולים ביותר" table does not shrink below 307 px of content inside 78 px of padding.
**Expected Behavior:** The creatine card fits a 360 px screen.
**Acceptance Criteria (AC):**
- At 360 px, with the vacation tables hidden, `scrollWidth` is 360.
- Product names wrap; the price stays on one line.
- The 800 px layout is unchanged.

---

**Issue Key / ID:** BUG-25
**Title:** Packages are sorted by a price that is not the first price shown
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** The first price column is "מחיר ללילה". `_rank` sorts by price per person, which orders the "מחיר החבילה" column. The card says "בכל יבשת מהזול ליקר", and rows 1–7 read `$165, $140, $156, $280, $185, $326, $365` per night. Row 2 at $140 a night sits under row 1 at $165.
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
**Context & Current Behavior:** The site promotion condition is written into a `<div>` that inherits `dir="rtl"`. `30% OFF EVERYTHING` is displayed as `OFF EVERYTHING 30%`. The creatine names already use `dir="auto"`; these lines do not.
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
**Context & Current Behavior:** `_finalize_report` dedupes on (chain, promotion description, price per kg). "פילה מדומה בקר טרי מס 6" in one chain appears at ₪72.90 (its own promotion) and at ₪77.31 (10% department promotion), rows 1 and 2 of the table. An item on promotion in one branch can also be listed again at the regular price of the chain's other branches.
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
**Context & Current Behavior:** The note is hidden only when `desc == p.item_name`. `פילה מדומה בקר טרי מס 6` is followed by `פילה מדומה מס 6 טרי`; `גרון הודו קפוא` by `גרון הודו קפוא ס.בודדים`; `פיקניה בקר טרי` by `פיקניה בקר טרי מבצע`.
**Expected Behavior:** After BUG-03 removes the tags, a description that adds no words beyond the item name is not shown.
**Acceptance Criteria (AC):**
- A description whose words are all in the item name (ignoring "מבצע") renders no note text.
- `10% הנחה על כל מחלקת הבשר` still renders.
- The end date still renders when the description is hidden.

---

**Issue Key / ID:** BUG-09
**Title:** Text is cut mid-word with no ellipsis
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** Slices `[:60]` (meat name and note), `[:300]` / `[:200]` (site promo condition), `[:70]` (creatine name), `[:80]` (package item), `[:90]` (flight details and the meat note in the text part). A creatine name ends `…Unflavored, 1`; a promo condition ends `…ללא מוצרי הום, משקפי`.
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
**Context & Current Behavior:** The fixture subject is 156 characters: `דוח יומי: עד 30% הנחה ב-MyProtein · 22 חבילות סימס 4 במבצע · מבצעי בשר ב-7 רשתות (הרצליה, רמת השרון) · חבילות לזוג מ-$660 · טיסות מ-$94 · סריקה נכשלה: אתרים`. The failure note is last, where an inbox list cuts it off. There is no preheader, and the header reads "דוח יומי" with no date.
**Expected Behavior:** The subject names the one or two most important items in 60 characters or fewer; the rest goes into a hidden preheader. The header shows the date.
**Acceptance Criteria (AC):**
- Subject length is 60 characters or fewer for the full fixture.
- A failed section is named within the first 60 characters.
- A hidden preheader of 90 characters or fewer follows `<body>`.
- The header shows the day and date in Hebrew ("יום ראשון, 15.11.2026"); the text part's first line does too.

---

**Issue Key / ID:** UX-06
**Title:** Status strip under the header; order sections by what changed
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** Five unrelated topics run in a fixed order: site promotions, creatine, Sims 4, meat, vacations. Each has its own card style. The reader cannot see which sections have news without scrolling 27 screens.
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
**Context & Current Behavior:** The meat summary card holds a 60-word ranking note, followed by a paragraph that lists all 29 category names. The vacation card holds three paragraphs (windows per continent, price definitions, "מה כלול" definition), and the badge legend with full definitions is printed above both the packages and the flights table.
**Expected Behavior:** Each section keeps one short line of scope. Definitions and ranking rules sit once in a block at the bottom.
**Acceptance Criteria (AC):**
- No section intro is longer than 25 words.
- The badge legend appears once.
- The category list is removed, or each name links to its table.
- Notes that report a problem today (failed chain, unavailable site, unpriced deals) stay in their section.

---

**Issue Key / ID:** UX-13
**Title:** Mark deal rows apart from regular-price rows in the meat tables
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** Regular prices compete with deals by design. In the fixture 100 of 145 rows are "מחיר רגיל"; in 16 of 29 tables the only deal is row 1 and the other four rows are regular prices. The two kinds differ only by a small grey "מחיר רגיל" against a small red percentage.
**Expected Behavior:** A deal row is recognisable without reading the small print, and the table header says how many of its rows are deals.
**Acceptance Criteria (AC):**
- Deal rows carry one visual mark (for example a "מבצע" chip beside the price) that regular rows do not.
- The table header reads "5 הזולים מתוך 14 · 2 במבצע".
- The ranking by price per kg is unchanged.
- The mark adds no more than 40 bytes per row.

---

**Issue Key / ID:** BUG-29
**Title:** "הכי ארוך" and "הכי קצר" badges mark one of several equal rows
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** `_deal_badges` gives each badge to the first row with the extreme value. Since trips are held to 3–5 nights, ties are the rule: in Europe packages 6 of 15 rows are 5 nights and 6 are 3 nights, and one of each is badged. In a two-row table (North America packages) four badges are given, with "הכי ארוך" on a 4-night trip. "הכי משתלם" sits on `$280 ללילה` in a table that also shows `$140`, because it only compares rows with luggage.
**Expected Behavior:** A badge appears only when it singles out one row.
**Acceptance Criteria (AC):**
- No length badge when two or more rows share the longest (or shortest) length.
- No length badge in a table of fewer than four rows.
- "הכי משתלם" is renamed to say what it compares ("הכי זול ללילה עם כבודה"), in the badge and in the legend.
- A test covers a table with three 5-night rows and asserts no "הכי ארוך".

---

**Issue Key / ID:** BUG-23
**Title:** Summary counts do not match the rows shown, and filtered deals are not reported
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** The vacation card says "126 דילים" (`total_found`, before the continent and top-N cuts) above 52 rows. The meat card says "227 מוצרי בשר" above 145 rows. `VacationReport.dropped_length` (95), `dropped_party` (36) and `dropped_low_star` (9) are set by the scan and never rendered.
**Expected Behavior:** The headline number is the number shown, with the scanned total beside it: "52 דילים מוצגים מתוך 126". One line reports what the rules removed.
**Acceptance Criteria (AC):**
- The bold count equals the number of rows rendered in that section.
- The scanned total appears as "מתוך N" only when it is larger.
- When any `dropped_*` counter is above zero, one line lists them ("לא הוצגו: 95 בגלל אורך החופשה, 36 שאינן לזוג, 9 בגלל דירוג המלון").

---

**Issue Key / ID:** BUG-10
**Title:** Flight prices have no thousands separator
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** `_deal_price` prints `$1184 לאדם`. `_deal_total` and `_package_night_price` print `$2,670`. The subject's "טיסות מ-" uses the first form.
**Expected Behavior:** All vacation amounts use a thousands separator and no decimals.
**Acceptance Criteria (AC):**
- 1390 renders `$1,390` in the HTML, the text part and the subject.
- 289 renders `$289`.

---

**Issue Key / ID:** BUG-07
**Title:** Confirm the scale of `DiscountRate` in each chain's feed, Carrefour included
**Type:** Parser Refactor
**Priority:** P3 - Low
**Context & Current Behavior:** A `DiscountRate` of 100 or more is ignored (`0 < discount_rate < 100`) with no log line. With `DiscountRate` 2310 the fixture item is listed as "מחיר רגיל" ₪86.90. Carrefour's scale has not been recorded either.
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
**Context & Current Behavior:** The title "5 הזולים ביותר ל-100 גרם" is a fixed string above `creatine.products[:5]`. With four powders found it sits above four rows. Row 1 is the same product, price and link as the card directly above it.
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
**Context & Current Behavior:** 892 lines and 57,500 bytes for the fixture. Each vacation deal takes four or five lines, one of them a full URL with tracking parameters. `₪` and digits sit inside RTL lines with no direction marks.
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

## 5. Preview of #65 (online butchers), not merged

The same fixture was rendered on `feat/butchers` @ `6ad918c`, with three
invented butcher catalogues added through `butchers._promo`. Every category
table gains a "משלוח עד הבית" group of up to three rows.

| Measure | `main` | #65 |
|---|---|---|
| HTML size | 111,702 bytes | 132,074 bytes |
| Meat rows (regular-price rows) | 145 (100) | 212 (160) |
| Height at 375 px | 21,655 px, 27 screens | 26,290 px, 33 screens |
| Vacations start at | 12,882 px | 17,517 px |
| Plain-text lines | 892 | 1,154 |

The two tickets below apply only if #65 merges. BUG-02 should be settled
first or with it, because #65 moves the email a further 20 KB over the limit.

---

**Issue Key / ID:** BUG-32
**Title:** Butchers are counted as supermarket chains, and Meat Point is listed in red every day
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** On #65 the section is still titled "מבצעי בשר בסופרמרקטים", the summary reads "10 רשתות · 12 סניפים" and the subject "מבצעי בשר ב-10 רשתות (הרצליה, רמת השרון)", counting three delivery butchers as chains in those cities. Meat Point shows no old prices by design, so `scan_and_report` files it under `chains_empty` and the card prints, in red, "רשתות ללא מבצעים: מיט פוינט (אין היום מבצעי בשר)" on every run.
**Expected Behavior:** Supermarkets and butchers are counted apart. A source that never publishes sale prices is not reported as a chain without deals.
**Acceptance Criteria (AC):**
- The summary reads "7 רשתות · 12 סניפים · 3 קצביות במשלוח"; the subject counts supermarkets only.
- The section title covers both ("בשר: סופרמרקטים וקצביות").
- Meat Point does not appear in the "ללא מבצעים" line when its scan succeeds.
- A butcher whose scan fails still appears in the failure line.

---

**Issue Key / ID:** UX-14
**Title:** Delivery rows do not show the minimum order or the delivery fee
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** A butcher row shows the chain, "משלוח עד הבית" and a price per kg, in the same table as walk-in supermarket prices. `butchers.py` notes that Meatman delivers to Herzliya with a ₪400 minimum order and a ₪50 fee; the email shows neither. This is the same problem as BUG-19: a price that holds only above a basket total reads as a plain price.
**Expected Behavior:** Each butcher's delivery terms are shown once, where its rows are read.
**Acceptance Criteria (AC):**
- Each butcher has its minimum order and delivery fee in the code, with the date they were checked.
- The "משלוח עד הבית" group header, or the row, shows them ("מיטמן: מינימום ₪400, משלוח ₪50").
- A butcher with unknown terms shows "תנאי משלוח: לא ידוע".
- The terms are in the plain-text part.

## 6. Status of the 2026-11-14 tickets

No ticket was fixed. #64 touched only the meat classifier and the category
icons. Figures are from today's fixture, which is larger than yesterday's (29
meat tables against 25), so a change in a figure is not a change in the code.

| Key | Title (short) | Status on `main` @ `a158dfd` |
|---|---|---|
| BUG-18 | Vacation tables wider than a phone | Open: 483 px at 375 px |
| BUG-02 | Gmail clipping | Open: 111,702 bytes |
| BUG-19 | Conditional prices ranked as plain | Open; tie-break rule added to the ticket |
| BUG-05 | "1+1" dropped or unmarked | Open |
| BUG-03 | Raw condition tags | Open |
| UX-03 | Condition badges | Open |
| UX-01 | Highlights block | Open |
| UX-10 | Vacation row for phones | Open: average package row 232 px |
| UX-04 | Collapse unchanged, mark new | Open |
| BUG-25 | Packages sorted by a price not shown first | Open |
| BUG-26 | English text reordered | Open |
| BUG-20 | One item in several rows | Open |
| BUG-21 | Note repeats the item name | Open |
| BUG-09 | Text cut mid-word | Open |
| UX-05 | Subject, preheader, date | Open (subject 156 characters) |
| UX-06 | Status strip, section order | Open |
| UX-12 | One "how to read" block | Open; category list added to the ticket |
| UX-13 | Mark deal rows | Open (100 of 145 rows are regular prices) |
| BUG-23 | Counts do not match rows | Open |
| BUG-10 | Flight thousands separator | Open |
| BUG-07 | `DiscountRate` scale | Open |
| BUG-24 | Expired site promotions | Open |
| BUG-27 | Creatine list title | Open |
| BUG-15 | Empty states differ | Open |
| BUG-16 | Plain-text dump | Open (892 lines) |
| BUG-17 | No dark-mode declaration | Open |

New today: BUG-28, BUG-29, BUG-30, BUG-31, and BUG-32 and UX-14 for #65.
