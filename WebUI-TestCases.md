# WebUI — Manual Browser Test Cases

Unit tests (`tests/`) cover the service/retrieval/config layers. They **cannot** exercise
the browser interaction, so these are the human-run checks for the two WebUI stages
(`/ingest`, `/chat`) on the running container:

```bash
# http://localhost:9001/  ->  /ingest
# http://localhost:9001/chat
```

Run every step in a **fresh browser session** (or a new tab) so the `lang` cookie and
`localStorage` start clean.

---

## 1. Page load & navigation

| # | Step | Expected |
|---|---|---|
| 1.1 | Open `http://localhost:9001/` | HTTP 302 → lands on `/ingest` |
| 1.2 | Click the **Troubleshooting** nav link (and back to **Submit**) | Page swaps, nav active state follows the current stage |
| 1.3 | All three cascade `<select>` fields on both pages show only `——` (empty default) | `category`, `product`, `article_number` are blank until populated |

## 2. Category select population

| # | Step | Expected |
|---|---|---|
| 2.1 | After load, open the **Category** dropdown | It is **populated** with categories from `config/taxonomy.yaml` (e.g. *Hydraulik / Hydraulics*). **This was the chat bug — a blank category dropdown was the failure.** |
| 2.2 | Select a category | **Product** dropdown repopulates with only that category's products. **Article number** resets to `——`. |
| 2.3 | Select a different category | Product resets then repopulates with the new set; article number resets to `——`. |
| 2.4 | Select a category that has **no products** | Product dropdown returns to `——` (empty, not an error). |

## 3. Product → article_number cascade

| # | Step | Expected |
|---|---|---|
| 3.1 | Select a category, then a product | **Article number** dropdown repopulates with only that product's article numbers. |
| 3.2 | Change the product (keeping category) | Article number resets then repopulates for the new product. |
| 3.3 | Article options show the **product's chosen-language label** (not the raw article number) | `id` = raw value (e.g. `200-010`); label = product label in `sv`/`en`. |

## 4. Language toggle (`sv` ⇄ `en`)

| # | Step | Expected |
|---|---|---|
| 4.1 | Click the **EN** / **SV** button in the top-right | Page labels re-render in the new language (i18n table swap, no full reload needed for the cascade labels). |
| 4.2 | After switching, re-open the Category dropdown | Still populated; labels now in the new language. |
| 4.3 | Repeat 3.1–3.2 in the new language | Cascade still works; product labels reflect the active language. |

## 5. Submit form (`/ingest`) validation & palette

| # | Step | Expected |
|---|---|---|
| 5.1 | Click **Submit** with nothing filled | A **pale-red box** with **bold red text**, **no HTML tags**: `Vänligen fyll i: Kategori, Produkt, Kundens felbeskrivning, Lösningsbeskrivning`. (SV) / same in EN. |
| 5.2 | Submit with only category + product filled | Error omits `failure_description` / `solution_description` from the list (only the truly-empty required fields). |
| 5.3 | Fill all required fields + submit | Palette is the **pale-blue** scheme (not dark blue); no raw `<span class="req">` markup leaks into any label. |
| 5.4 | Submit a duplicate | Re-renders with a `?edit=duplicate` flag and the duplicate message. |

## 6. Chat form (`/chat`) validation & conversation

| # | Step | Expected |
|---|---|---|
| 6.1 | Click **Send** with the failure description empty | **Pale-red box / bold red text, no HTML tags**: `Vänligen fyll i: Felbeskrivning`. |
| 6.2 | Enter a valid failure description + scope | Agent responds inside the conversation bubble; a **busy spinner** ("Agenten tänker…" / "Agent is thinking…") shows during the request. |
| 6.3 | Send with no text (following a valid turn) | Error: `Vänligen fyll i: [Skicka placeholder]`. |
| 6.4 | **Sources** render (RAG hit shows source file + section; record hit shows product/article/failure) | A "Källor / Sources" list appears under the answer. |
| 6.5 | **Clear** button | Conversation empties, empty-hint returns, server history resets. |
| 6.6 | Ask a follow-up (no scope change) | Uses the chat input, not the failure description; conversation continues. |

## 7. Record list (bottom of `/ingest`)

| # | Step | Expected |
|---|---|---|
| 7.1 | Page loads | The filtered list partial renders below the form. |
| 7.2 | Change a filter (category / product / q / status) and submit | List re-renders; `status=archived` shows archived rows too. |
| 7.3 | **Edit / Archive / Restore** on a card | Edit re-renders with `?edit=ok|invalid|...`; Archive shows `?archive=ok`; Restore shows `?restore=ok` or `?restore=collided`. |

## 8. End-to-end happy path (both stages)

1. Open `/ingest`. Category populated. → select category → product populated → select product → article populated.
2. Fill `failure_description` + `solution_description`. Submit → success acknowledgment.
3. Open `/chat`. Category populated → product → article. Type a question referencing the record you just added. Agent returns an answer with at least one source (the record you created).

---

### Notes
- The taxonomy values come from `config/taxonomy.yaml` — adding a category/product there is
  enough to see it in the dropdowns on next load (no code change).
- If the category dropdown is ever **blank on load**, the page is broken (that was bug fix #1 this
  session — `troubleshooting.html` init now fetches `/api/taxonomy/categories`).
- If any error message contains raw `<span class="req">` markup, the i18n-key fix is regressed.
