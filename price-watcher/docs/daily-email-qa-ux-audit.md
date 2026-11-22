# Daily deals email: QA and UX audit

Date: 2026-11-12. Scope: the combined daily email ("דוח יומי") built by
`build_daily_payload` in `price-watcher/src/price_watcher/email_sender.py`.

## How this audit was done, and its limits

- **No email was attached to the audit request.** The audit is of the code
  that generates the email, on `main` @ `3f1f860`.
- **No live data.** The sandbox blocks the price feeds, iHerb and EA, so the
  email was rendered from fixture rows. The rows reuse real item names and
  prices quoted in issues #17 and #51, plus the condition tags named in the
  request (`מוג 3`, `ע.מלאי`, `ת.קצר`, `ס.בודדים`, `2 ב110`).
- **Fixture size:** 78 meat rows, 22 Sims packs, 3 sites. The default
  configuration produces up to 90 meat rows, so real emails are larger.
- **`main` may be behind your local copy.** Issues #12–#38 are closed and refer
  to a branch `fix/qa-issues-12-38` that is not on the remote. Findings marked
  "still on `main`" may already be fixed there.
- Findings are marked **Confirmed** (reproduced with the fixture or measured in
  a rendered page) or **To verify** (read from the code, needs a real feed).
- Per the constraint, nothing here touches the sending schedule or frequency.

## 1. Executive summary

**Verdict: the email is a data dump, and its biggest section cannot be trusted
or fully read.**

1. **Wrong prices and discounts in the meat table.** A "2 for ₪110" deal on a
   ₪149.90 item is shown as ₪149.90 → ₪110.00, −27%. The real figure is ₪55
   each, −63%. Deals whose bundle total exceeds the unit price ("3 ק"ג ב-40")
   compute a negative discount and are dropped without a trace.
2. **Gmail will clip the message.** The fixture email is 119,699 bytes of HTML.
   Gmail cuts messages above about 102 KB behind "View entire message". The
   meat section is last, so it is the part that gets cut.
3. **Raw feed text reaches the reader.** Tags such as `מוג 3`, `ע.מלאי`,
   `ת.קצר` and `ס.בודדים` are printed as they arrive, and usually twice,
   because the promotion description repeats the item name.
4. **Low signal.** Up to 30 rows per region, ranked by discount percentage
   only. One cut in several brands fills the top of the list. Sims 4 and
   creatine repeat their full tables every day even when nothing changed.
5. **Unreadable on a phone.** At 375 px the product column is 78 px wide, rows
   average 113 px tall, and the email is 11,868 px long (about 15 screens). The
   meat section starts 2,262 px down.

What works: RTL direction is set correctly throughout, prices are wrapped in
LTR spans so `₪` stays attached to the number, and each section has a failure
state.

## 2. QA and bug findings

### 2.1 Pricing and discount calculation

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q1 | Bundle total shown as the "now" price. `MinQty` is never read. | `אנטריקוט בקר טרי`, unit ₪149.90, "2 ב110" → shown ₪110.00, −27% | Confirmed, still on `main` (#17 is closed) |
| Q2 | Bundle deals with total > unit price vanish. | `כרעיים עוף 3 ק ג ב 40` → −61%, dropped by the `pct <= 0` filter | Confirmed |
| Q3 | Unit mismatch: per-kg regular price next to a per-100 g promo price. No unit is shown anywhere. | `פלמי טופ כבד אווז` ₪490.00 → ₪39.90 = "92%", then dropped by the 90% cap | Confirmed |
| Q4 | Rate-only promos show a discount with no promo price; "1+1" shows identical was/now. | `אסאדו 1+1`: ~~₪79.90~~ ₪79.90, −50%. Rate-only row: grey ₪32.90, −5% | Confirmed |
| Q5 | Non-deals rank as deals. A permanent payment-method discount passes; a class-action credit is only stopped by the 90% cap, not by its text. | `תו זהב 5% הנחה מותג שופרסל`; `הנחה בגין תביעה יצוגית` | Confirmed |
| Q6 | `DiscountRate` above 100 is dropped silently. Some feeds may publish hundredths of a percent (2310 = 23.10%). | `savings_pct = 2310`, row removed | To verify against a real feed |
| Q7 | Price fields are read only on `PromotionItem`. A feed that puts `DiscountedPrice` on `Promotion` yields zero rows with no warning. | Fixture: promotion-level fields → 0 of 18 rows | To verify |
| Q8 | Discount percent is truncated, not rounded (`int()`), and decimals are inconsistent. | `₪25`, `₪29.90`, `₪10.00` in one column | Confirmed |

### 2.2 Parsing artifacts

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q9 | Condition tags are printed raw, in both the name and the description line. | `חזה עוף טרי מותג 13 מוג 3` / `חזה עוף מבצע 13 ע.מלאי` | Confirmed |
| Q10 | The description line repeats the item name. | `גרון הודו קפוא` / `גרון הודו קפוא ס.בודדים` | Confirmed |
| Q11 | Names and descriptions are cut mid-word with no ellipsis (`[:60]`, `[:70]`, `[:300]`). | 76-character name cut at 60 | Confirmed |
| Q12 | Substring matching misfiles items. | `סטרצ'טלה` (cheese) → `כבש / טלה`; `כבד אווז` → `כבד עוף`; `פילה עוף` → `פילה בקר`; `כתף כבש` and `צלי כתף בקר` → `כתף`; `סטייק פרגית` → `סטייק` | Confirmed (partly tracked in #51) |
| Q13 | Site promo "condition" is a ±100-character slice of raw page HTML, so script and JSON fragments reach the email. | `OFF11 "},{"type":"banner","cls":"promo-strip__txt` | Confirmed by construction (`_text_sources` scans the whole HTML) |
| Q14 | "Up to N%" is reported as a flat N% when a code is near it, including in the subject. Three-digit numbers are misread (`150%` → 50). | `עד 55% הנחה … קוד: SINGLES` → "55% הנחה" | Confirmed in code |
| Q15 | Sims pack names are rebuilt from the URL slug, so punctuation and casing are lost. Prices are USD in an otherwise ₪ email, unlabelled. | `_slug_to_name` | Confirmed in code |

### 2.3 Layout and rendering

| # | Finding | Evidence | Status |
|---|---|---|---|
| Q16 | HTML exceeds Gmail's clipping limit. | 119,699 bytes with 78 meat rows | Confirmed (size measured; limit is Gmail's documented ~102 KB) |
| Q17 | Five-column table on a phone. | 375 px: columns 28/67/78/71/59 px, average row 113 px, 8 px horizontal overflow | Confirmed |
| Q18 | "סניפים נסרקו" counts only stores that returned a meat promo. A chain that failed looks the same as a chain with no deals: it is simply missing. | 8 of 11 chains absent in the fixture, no notice in the email | Confirmed |
| Q19 | Empty and failed states are inconsistent. | Creatine: red box. Sims / supermarkets not scanned: unstyled grey "(לא נסרק)". Site with no promo: red `no_promo: —` (#47) | Confirmed |
| Q20 | The body has no date. | Header is "דוח יומי" only | Confirmed |
| Q21 | Plain-text part puts `₪` and numbers inside RTL lines with no direction marks; 393 lines. | `daily.txt` | Confirmed |
| Q22 | Light colours are hard-coded with no `color-scheme` declaration, so dark-mode clients may invert them unevenly. | No `<meta name="color-scheme">` | To verify in clients |

### 2.4 Already tracked (not re-ticketed)

| Issue | Topic |
|---|---|
| #19 | Sims 4 links return 404; only 50 packs scanned |
| #45 | Creatine lists the same product twice; links go to `/iherb-brands` |
| #46 | Store-to-region matching by name is wrong |
| #47 | Sites without a promo show a red `no_promo: —` |
| #50 | Narrow the scan area, add Bareket, top 5 cheapest per category |
| #51 | Meat categories are too wide |

## 3. UX and content curation critique

**The email answers "what did the scraper find" instead of "what should I buy
today".** Recommendations, in order of value:

1. **Open with "היום שווה לדעת": at most five highlights across all sections.**
   Each is one line: what, where, price per unit, why it qualifies (new today,
   lowest seen, ends tomorrow). Everything below it is reference.
2. **Group meat by cut, not by region.** One compact block per cut (`חזה עוף`,
   `אנטריקוט`, `בשר טחון`), top 3 by price per kg, with chain and branch. Brand
   variants of one promotion collapse to one row ("4 מותגים"). This is the
   presentation side of #50 and #51.
3. **Replace raw tags with badges.** A fixed vocabulary:

   | Raw | Badge |
   |---|---|
   | `מוג 3` | מוגבל ל-3 |
   | `ע.מלאי` | עד גמר המלאי |
   | `ת.קצר` | תוקף קצר |
   | `ס.בודדים` | בסניפים נבחרים |
   | `מעל 99 ש״ח` | בקנייה מעל ₪99 |
   | `2 ב110` | 2 ב-₪110 (₪55 ליח') |

4. **Show what changed, collapse what did not.** Sims 4 with no change is one
   line ("22 חבילות במבצע, ללא שינוי"). Creatine with no change is one line
   with the price. `supermarkets-state.json` is already written every day and
   never read; use it to mark meat deals as "חדש היום".
5. **Make conditions part of the price.** A conditional price should never
   look like a plain price. Show the condition beside it, and the per-unit
   price when a quantity is required.
6. **Bridge the themes with a common frame.** Four unrelated topics need a
   shared summary strip at the top (one status chip per section) and the same
   card pattern in each. Order sections by what changed today, not in a fixed
   order that puts the longest section last.
7. **Earn the "הכי משתלם" badge.** Today it goes to row 1 of every region,
   even a frozen turkey neck. Award it per cut, on price per kg, and only when
   the price is real (no unmet condition).

## 4. Backlog

Priority order: BUG-01, BUG-02, BUG-03, UX-01, UX-02, then the rest.

---

**Issue Key / ID:** BUG-01
**Title:** Multi-buy promotions show the bundle total as the unit price and a wrong discount
**Type:** Bug Fix
**Priority:** P0 - Blocker
**Context & Current Behavior:** `_extract_meat_promos` reads `DiscountedPrice` and never reads `MinQty`. `אנטריקוט בקר טרי` at ₪149.90 with "2 ב110" renders `₪149.90 → ₪110.00, −27%`. `כרעיים עוף 3 ק ג ב 40` computes −61% and is dropped.
**Expected Behavior:** The price cell shows the per-unit promo price and the bundle rule: "₪55.00 ליח' · 2 ב-₪110". The discount is computed per unit (−63%).
**Acceptance Criteria (AC):**
- `MinQty` is read; per-unit price = `DiscountedPrice / MinQty`.
- The fixture row above renders ₪55.00 and −63%.
- "3 ק"ג ב-40" on a ₪24.90 item renders ₪13.33 per kg and is not dropped.
- No rendered row has a promo price higher than its regular price.
- Unit tests cover MinQty 1, 2, 3 and a missing `MinQty`.

---

**Issue Key / ID:** BUG-02
**Title:** Daily email exceeds Gmail's size limit and is clipped
**Type:** Bug Fix
**Priority:** P0 - Blocker
**Context & Current Behavior:** The fixture email is 119,699 bytes of HTML (78 meat rows, 22 Sims rows). Every cell repeats long inline styles and the font stack. Gmail hides everything past about 102 KB, which is the end of the meat section and the footer.
**Expected Behavior:** The HTML part stays under 90 KB on a full-size day.
**Acceptance Criteria (AC):**
- A test builds the payload with the maximum row counts and asserts the UTF-8 HTML size is below 90,000 bytes.
- The font stack is declared once per table, not per cell.
- If content would exceed the budget, the lowest-ranked rows are cut and a line states how many were omitted.

---

**Issue Key / ID:** BUG-03
**Title:** Raw feed condition tags are printed in item names and descriptions
**Type:** Parser Refactor
**Priority:** P1 - High
**Context & Current Behavior:** Names and descriptions are passed through untouched: `חזה עוף טרי מותג 13 מוג 3`, `חזה עוף מבצע 13 ע.מלאי`, `גרון הודו קפוא ס.בודדים`, `חזה עוף טרי ע.מלאי ת.קצר`, `אנטריקוט טרי 2 ב110 מוג 3`.
**Expected Behavior:** A normaliser strips known tags from the display name and returns them as structured conditions (limit per customer, while stocks last, short expiry, selected branches, minimum basket, multi-buy).
**Acceptance Criteria (AC):**
- A function `parse_conditions(text) -> (clean_name, conditions)` exists with a table-driven tag list.
- None of `מוג N`, `ע.מלאי`, `ת.קצר`, `ס.בודדים`, `N ב-M` appears in a rendered name.
- Variants with and without dots, spaces and hyphens are covered by tests (`ע. מלאי`, `מוג' 3`, `2 ב 110`, `2ב110`).
- Unknown trailing tokens are logged so the tag list can grow.

---

**Issue Key / ID:** BUG-04
**Title:** Prices have no unit, and per-kg prices are compared with per-100 g or per-pack prices
**Type:** Parser Refactor
**Priority:** P1 - High
**Context & Current Behavior:** `פלמי טופ כבד אווז` shows ₪490.00 (per kg) against ₪39.90 (per 100 g) as a 92% discount. The feed's unit fields (`UnitOfMeasure`, `Quantity`, `bIsWeighted`, `UnitOfMeasurePrice`) are not read. No row says whether a price is per kg or per pack.
**Expected Behavior:** Every price carries a unit. Weighed items are normalised to ₪ per kg; packaged items show the pack price and ₪ per kg when the weight is known.
**Acceptance Criteria (AC):**
- Each row shows "לק"ג" or "לאריזה (N גרם)".
- Regular and promo price in one row always share a unit; otherwise the row is dropped and logged.
- The goose-liver fixture row is either normalised correctly or excluded, never shown as 92%.

---

**Issue Key / ID:** BUG-05
**Title:** "1+1" and percent-only promotions render a discount with no promo price
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** `אסאדו 1+1` renders ~~₪79.90~~ **₪79.90** with −50%. A rate-only row renders a grey regular price and a red percentage, with no price to pay.
**Expected Behavior:** The promo price is derived from the rate when only the rate is published, and from the bundle rule for N+M deals.
**Acceptance Criteria (AC):**
- Rate-only: promo price = regular × (1 − rate), shown in bold.
- "1+1": per-unit price is half the regular price, with a "1+1" badge.
- No row shows identical struck-through and bold prices.

---

**Issue Key / ID:** BUG-06
**Title:** Payment-method discounts and legal credits are listed as meat deals
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** Only the word `פיצוי` is filtered. `תו זהב 5% הנחה מותג שופרסל` (a permanent payment-method discount) passes and attaches to every Shufersal-brand item. `הנחה בגין תביעה יצוגית` is removed only because it happens to be 100%.
**Expected Behavior:** Promotions that are not shelf deals are excluded by rule.
**Acceptance Criteria (AC):**
- A deny-list covers `תו זהב`, `תביעה יצוגית`, `פיצוי`, `שובר`, `כרטיס אשראי`.
- Promotions valid for more than 90 days are excluded.
- Both fixture rows are absent from the output, at any discount value.

---

**Issue Key / ID:** BUG-07
**Title:** Confirm the scale and location of discount fields in each chain's feed
**Type:** Parser Refactor
**Priority:** P1 - High
**Context & Current Behavior:** Two silent-loss paths, both unverified against a live feed. (a) `DiscountRate=2310` gives `savings_pct=2310` and the row is dropped by the 90% cap; some chains may publish hundredths of a percent. (b) `DiscountedPrice` / `DiscountRate` are read only from `PromotionItem`; with the fields on `Promotion`, 0 of 18 fixture rows survive.
**Expected Behavior:** Field location and scale are known per chain, and dropped rows are counted.
**Acceptance Criteria (AC):**
- One saved sample PromoFull file per chain is checked into test data.
- The parser reads price fields from `PromotionItem`, falling back to `Promotion`.
- Rates above 100 are handled per the confirmed scale, or logged with a count.
- The scan log reports rows dropped per reason and per chain.

---

**Issue Key / ID:** BUG-08
**Title:** Substring matching puts items in the wrong cut
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** `_categorize` uses plain `in`. `סטרצ'טלה 200 גרם` → `כבש / טלה`; `כבד אווז` → `כבד עוף`; `פילה עוף טרי` → `פילה בקר`; `כתף כבש טרי` → `כתף`; `סטייק פרגית` → `סטייק`. `רוסטביף` and `דנבר` are not recognised as meat. Extends #51.
**Expected Behavior:** Categorisation uses whole-word matching and resolves animal first, then cut.
**Acceptance Criteria (AC):**
- `_categorize` uses the same Hebrew word boundary as `_MEAT_PAT`.
- `סטרצ'טלה` is excluded from the meat list.
- A test table of at least 40 real item names asserts the expected category for each, including all examples above.

---

**Issue Key / ID:** BUG-09
**Title:** Text is cut mid-word with no ellipsis
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** `item_name[:60]`, `promotion_description[:70]`, `condition[:300]`, creatine `name[:70]`. A 76-character item name ends in the middle of a word.
**Expected Behavior:** Truncation happens at a word boundary and ends with "…".
**Acceptance Criteria (AC):**
- One shared helper truncates at the last space before the limit and appends "…".
- All four call sites use it.
- Strings at or under the limit are unchanged.

---

**Issue Key / ID:** BUG-10
**Title:** Inconsistent price and percentage formatting
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** One column shows `₪25`, `₪29.90` and `₪10.00`, because the feed's raw decimal string is printed. Percentages use `int()`, so 26.9% shows as 26%.
**Expected Behavior:** All ₪ prices show two decimals; percentages are rounded to the nearest whole number.
**Acceptance Criteria (AC):**
- A single `format_ils()` helper is used for every ₪ price in the HTML and text parts.
- `Decimal("25")` renders `₪25.00`; 26.9% renders `-27%`.
- Sims prices keep `$` and the section states "מחירים בדולר".

---

**Issue Key / ID:** BUG-11
**Title:** Site promo details show fragments of page source
**Type:** Parser Refactor
**Priority:** P1 - High
**Context & Current Behavior:** `_text_sources` scans the full raw HTML, and `_condition_around` returns a slice of 50 characters before and 100 after the match. When the banner lives in a script block, the email shows text such as `OFF11 "},{"type":"banner","cls":"promo-strip__txt`.
**Expected Behavior:** The detail line is a clean sentence, or absent.
**Acceptance Criteria (AC):**
- The detail text contains no `{`, `}`, `<`, `>`, `\"` or `":"`.
- Text taken from script blocks is JSON-decoded and stripped of markup before slicing.
- The slice is trimmed to sentence boundaries.
- If no clean sentence is found, only the percentage and code are shown.

---

**Issue Key / ID:** BUG-12
**Title:** "Up to N%" promotions are reported as a flat N% discount
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** The code patterns do not exclude `עד` / `up to`. "עד 55% הנחה … קוד: SINGLES" is listed as "55% הנחה" and can lead the subject line. `(\d{1,2})` also reads `150%` as 50.
**Expected Behavior:** Tiered promotions are labelled "עד N%" and never outrank a flat discount of the same value.
**Acceptance Criteria (AC):**
- `PromoInfo` carries an `is_up_to` flag; the email prints "עד 55%".
- Flat discounts sort above "up to" discounts.
- A percentage preceded by a digit (three-digit numbers) is not matched.

---

**Issue Key / ID:** BUG-13
**Title:** Meat table is unreadable at phone width
**Type:** Bug Fix
**Priority:** P1 - High
**Context & Current Behavior:** At 375 px the five columns measure 28/67/78/71/59 px. Product names wrap to four lines, rows average 113 px, the page scrolls 8 px sideways and is 11,868 px long.
**Expected Behavior:** Each deal is a two-line block on narrow screens: name and price on the first line, chain, place and badges on the second.
**Acceptance Criteria (AC):**
- At 375 px there is no horizontal scroll (`scrollWidth == clientWidth`).
- No deal block is taller than 64 px with a 40-character name.
- The rank column is removed.
- Verified in Gmail (Android, iOS) and Apple Mail.

---

**Issue Key / ID:** BUG-14
**Title:** Scan coverage in the summary is misleading, and failed chains are invisible
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** "N סניפים" counts only stores that returned at least one meat promo. A chain whose login or download failed is left out of the "רשתות" line, exactly like a chain with no deals. In the fixture, 8 of 11 chains were missing with no notice.
**Expected Behavior:** The summary separates scanned, empty and failed.
**Acceptance Criteria (AC):**
- The summary reads "N רשתות נסרקו · M ללא מבצעים · K נכשלו".
- Failed chains are named in one muted line.
- Store counts report stores scanned, not stores with deals.

---

**Issue Key / ID:** BUG-15
**Title:** Empty and failed states differ between sections
**Type:** Bug Fix
**Priority:** P2 - Medium
**Context & Current Behavior:** Creatine failure is a red box. A Sims or supermarket stage that raised an exception shows an unstyled grey "(לא נסרק)". A site with no promo shows red `no_promo: —` (#47). A status with no Hebrew label prints its English key.
**Expected Behavior:** Three shared states: failed (red box with reason), nothing found (muted one-liner), not run (muted one-liner).
**Acceptance Criteria (AC):**
- All four sections render the three states through one helper.
- Every status value has a Hebrew label; a test fails on an unmapped status.
- No English status key appears in the email.

---

**Issue Key / ID:** BUG-16
**Title:** Plain-text part mixes directions and repeats the full dump
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** Lines such as `מחיר רגיל ₪39.90 · מחיר מבצע ₪29.90` have no direction marks, so clients may reorder numbers and symbols. The text part is 393 lines.
**Expected Behavior:** Numbers and prices are wrapped in LRM marks; the text part carries the highlights and section summaries only.
**Acceptance Criteria (AC):**
- Every price in the text part is preceded and followed by U+200E.
- The text part is under 80 lines on a full-size day.

---

**Issue Key / ID:** BUG-17
**Title:** No dark-mode declaration
**Type:** Bug Fix
**Priority:** P3 - Low
**Context & Current Behavior:** Colours are hard-coded for a light theme and there is no `color-scheme` meta. Clients that auto-invert may leave the green "best" row and red discount text at low contrast. Not yet checked in real clients.
**Expected Behavior:** The email declares its supported schemes and stays legible in both.
**Acceptance Criteria (AC):**
- `<meta name="color-scheme" content="light dark">` and `supported-color-schemes` are present.
- Screenshots in Gmail and Apple Mail dark mode show body text at a contrast ratio of at least 4.5:1.

---

**Issue Key / ID:** UX-01
**Title:** Add a "היום שווה לדעת" highlights block at the top
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** The email opens with site promo cards, then creatine, then 22 Sims rows. The first meat deal is 2,262 px down on a phone. Nothing tells the reader which items matter today.
**Expected Behavior:** A block of at most five one-line highlights, chosen across all sections, each linking to its section.
**Acceptance Criteria (AC):**
- The block is the first content after the header and shows 1 to 5 items.
- Each item states what, where, price with unit, and a reason ("חדש היום", "הנמוך ביותר שנצפה", "מסתיים מחר").
- Selection rules are documented and tested: new or changed items first, then the lowest per-kg price per cut.
- When nothing qualifies the block reads "אין חדש היום".

---

**Issue Key / ID:** UX-02
**Title:** Present meat deals by cut, top three each, with brand variants merged
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** Each region lists up to 30 rows ranked by discount percentage. The dedupe key includes the promotion text, so one cut in several brands takes many rows: in the fixture, `חזה עוף טרי מותג N` fills 6 of the top 10. Presentation side of #50 and #51.
**Expected Behavior:** One compact block per cut, ordered by the reader's priority cuts, each with the three lowest per-kg prices.
**Acceptance Criteria (AC):**
- No cut shows more than 3 rows; the email shows at most 30 meat rows in total.
- Promotions that differ only by brand merge into one row with a brand count.
- Each row shows chain, branch or city, per-kg price, regular price and badges.
- Catch-all categories are not ranked and are omitted by default.

---

**Issue Key / ID:** UX-03
**Title:** Show promotion conditions as badges
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** Conditions appear only as raw text inside a grey description line (`מעל 99 ש"ח`, `מוג 3`). A conditional price looks the same as an unconditional one. Depends on BUG-03.
**Expected Behavior:** Each condition is a small badge next to the price, from a fixed vocabulary: מוגבל ל-N, עד גמר המלאי, תוקף קצר, בסניפים נבחרים, בקנייה מעל ₪N, מועדון, N ב-₪M.
**Acceptance Criteria (AC):**
- Badges come from the structured conditions of BUG-03, never from free text.
- A row with a minimum-basket or club condition shows that badge before the price.
- At most 3 badges per row; the rest collapse to "+N".
- The grey description line is removed when it adds nothing beyond name and badges.

---

**Issue Key / ID:** UX-04
**Title:** Collapse sections that did not change; mark what is new
**Type:** UX Enhancement
**Priority:** P1 - High
**Context & Current Behavior:** Sims 4 prints every pack on sale each day (22 rows in the fixture) and creatine prints its top five, even when nothing moved. Meat deals have no "new" marker, although `supermarkets-state.json` is written daily and never read.
**Expected Behavior:** Unchanged sections are one line. Changed items are listed and marked.
**Acceptance Criteria (AC):**
- Sims with no change: "22 חבילות במבצע · ללא שינוי מאתמול". Only new, ended and changed packs are listed otherwise.
- Creatine with no change: one line with the current price per 100 g.
- Meat deals absent from yesterday's state carry a "חדש" badge.
- First run (no state) shows the full view without badges.

---

**Issue Key / ID:** UX-05
**Title:** Shorter subject, preheader text and a date in the header
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** The fixture subject is 78 characters: `דוח יומי: עד 55% הנחה ב-MyProtein (SINGLES) · מבצעי בשר ב-3 רשתות, עד 50% הנחה`. Phones show about 40. The body header is "דוח יומי" with no date, and there is no preheader.
**Expected Behavior:** The subject names the single best item; the preheader carries the next two; the header shows the date.
**Acceptance Criteria (AC):**
- Subject is at most 50 characters and names one concrete deal with its price.
- A hidden preheader of 60 to 90 characters lists the next highlights.
- The header shows the weekday and date in Hebrew.

---

**Issue Key / ID:** UX-06
**Title:** Summary strip and one card pattern for all four topics
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** Fashion promo codes, a supplement price, game packs and butchery deals follow each other in a fixed order, each with its own visual style (grey cards, a green box, coloured boxes, bordered tables). The subtitle lists the topics but gives no status.
**Expected Behavior:** A four-chip strip under the header (one per topic, with a status such as "2 חדשים" or "ללא שינוי"), then sections in one shared card style, ordered by what changed.
**Acceptance Criteria (AC):**
- Each chip shows the topic and its status and links to its section.
- Sections with changes come first; unchanged sections follow in collapsed form.
- All sections use the same heading, card and muted-line styles.

---

**Issue Key / ID:** UX-07
**Title:** Show when a deal ends and which branch has it
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** `end_date` is parsed and never shown. Location is up to three city names and a store count (`הרצליה, נתניה, רעננה · 3 סניפים`); the branch name is not shown, and `ס.בודדים` is left unexplained.
**Expected Behavior:** Each row shows the end date when it is within 7 days, and the branch name when the deal is in three stores or fewer.
**Acceptance Criteria (AC):**
- Deals ending today or tomorrow show "מסתיים היום" / "מסתיים מחר".
- Deals in 1 to 3 stores list the branch names; more show "N סניפים ב-M ערים".
- Dates use the `d.m` format.

---

**Issue Key / ID:** UX-08
**Title:** Award the "הכי משתלם" badge on merit
**Type:** UX Enhancement
**Priority:** P2 - Medium
**Context & Current Behavior:** Row 1 of every region gets the green badge and background. In the fixture it goes to `גרון הודו קפוא` at −50%, a ₪10 frozen item.
**Expected Behavior:** The badge marks the lowest per-kg price within a cut, and only for unconditional prices.
**Acceptance Criteria (AC):**
- At most one badge per cut.
- A row with a minimum-basket, club or selected-branches condition cannot receive it.
- No badge when a cut has a single row.

---

**Issue Key / ID:** UX-09
**Title:** Remove the per-row category line and icons
**Type:** UX Enhancement
**Priority:** P3 - Low
**Context & Current Behavior:** Every meat row ends with an icon and the category name (`🍗 חזה עוף`, `👅 לשון בקר`, `🧠 מוח`). It repeats what the name says and adds a line to each row.
**Expected Behavior:** The cut name appears once, as the block heading (UX-02).
**Acceptance Criteria (AC):**
- No per-row category line.
- Icons, if kept, appear only in block headings; the tongue and brain icons are removed.
