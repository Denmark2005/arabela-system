# Arabela Gown Rental System

This document describes what the system actually does today, based on a direct read of the code. It does not include planned features. Anything half-built, inconsistent, or broken is called out in Section 9.

## 1. Overview

Arabela is a reservation and inventory system for a gown/suit rental shop. It replaced a walk-in / Facebook-message booking process with a website where customers can browse real-time availability, reserve a gown online, pay a security deposit, and pick it up in person.

**Tech stack:**
- **Language / framework:** Python, Django 6.0.3
- **Database:** PostgreSQL (hosted on Supabase) in production; falls back to a local SQLite file automatically if no database environment variables are set, so the app also runs with zero DB setup
- **Hosting:** Render (implied by `gunicorn` as the WSGI server and a proxy-aware settings block), with WhiteNoise serving static files directly from the app
- **Media storage:** Cloudinary for gown photos, payment-proof screenshots, and profile pictures — falls back to local disk if no Cloudinary credentials are set (needed because Render wipes local disk on every restart/redeploy)
- **Authentication:** django-allauth, configured for Google sign-in
- **AI feature:** a chat widget backed by Google's Gemini API (not Anthropic, despite an unused `anthropic` package sitting in requirements — see Section 9)
- **Timezone:** Asia/Manila

## 2. User roles

| Role | How it's set | Access |
|---|---|---|
| **Customer** | Any signed-in Google account with no admin flags | Browse, search, reserve gowns, manage their own reservations/cancellations, upload payment proof, read their own message inbox, edit their display name |
| **Staff** | `UserProfile.role = Staff`, `is_staff = True` | Full admin panel **except** Staff Management and the owner's own account-settings/password page |
| **Manager** | `UserProfile.role = Manager`, `is_staff = True` | Same access as Staff — the code defines Manager as a separate role, but no permission check anywhere actually treats Manager differently from Staff. In practice these two roles are identical today. |
| **Owner** | `is_superuser = True`, or `UserProfile.role = Owner` | Everything Staff/Manager can do, **plus** Staff Management (create/edit/deactivate/delete staff accounts) and the owner-only change-password page |

Notes:
- A signed-in admin/staff account with no `UserProfile` row at all is treated as **Owner by default** in a couple of places (the header and notifications context processors), which is a looser default than the explicit `_is_owner()` permission check used to actually gate actions — see Section 9.
- Customers never see the admin panel at all; there's no in-between "customer with extra permissions" tier.

## 3. Features by module

### Customer-facing site
- **Homepage & info pages** — About, How It Works, Contact, Terms & Conditions, FAQs (static content pages).
- **Collections / catalog** — 13 fixed categories (Wedding Gown, Evening Gown, Long Gown, Luxury Gown, Mother Gown, Suit, Filipiniana, Guest Gown, Dresses, Kids Gown, Barong, Ball Gown Tulle, Bridesmaid Dresses), plus an "All" tab that shows every category together. Multiple physical units of the same gown are shown as one product card with an "available count," not one card per unit. Each category tile shows a picture of a gown on the shop's grey, which the owner manages in Admin → Categories (Section 12).
- **Product detail page** — photo, price, size, an availability calendar (dates a customer can't pick are greyed out), and a "You may also like" section (other real products from the same category — a plain, non-AI recommendation, separate from the chatbot below).
- **Site-wide search** — a search overlay that filters a small in-memory catalog by gown name only (not category or price) as the customer types; shows photo, name, and price per result. Its **View all** button (or Enter) opens a results page, `/search/?q=…`, with every matching gown from every category in one list, sorted Best Match by default.
- **"Arabela Recommends" AI chatbot** — a floating chat widget powered by Google Gemini, answering questions about the shop, policies, and (in theory) the catalog. It is grounded by a hand-written prompt describing shop hours, address, rental steps, and cancellation-lockout rules accurately — but its description of the actual gown catalog is out of date (Section 9).
- **Cart** — stored in the browser's `localStorage`; best-effort mirrored to the customer's account (server-side) so it can be picked up on another device, but the server copy is never treated as the source of truth for price or availability.
- **Reservation hold** — starting checkout begins a 20-minute countdown (tracked server-side in the session, not editable by the customer) that reserves the customer's spot in line while they fill out the form.
- **Checkout** — collects contact details and a required GCash payment-proof screenshot. All prices and availability are re-checked on the server at submission time; nothing from the browser is trusted.
- **My Reservations** — order history, a detail page per gown with its own timeline, a cancel button (with rules), and payment-proof upload for reservations still awaiting review.
- **Messages inbox** — in-app notices from staff/system: account flagged/unflagged, pick-up/return reminders, overdue notices, cancellation-lockout warnings.
- **Profile** — a customer can only edit their display name. A "saved addresses" section exists on the page but is not a real feature (Section 9).

### Admin panel
- **Dashboard** — live counts (registered customers, active rentals, total/available/out-of-stock gowns, reservations this month) that always match the detail page they link to; also the trigger point for the daily reminder sweep (see Section 5).
- **Rental Schedule (calendar)** — a calendar view of every booking's current stage (Pick-up, Reserved, Return, Overdue).
- **Pending Approval** — queue of reservations awaiting a first staff decision.
- **Payment Verification** — approve or reject a reservation based on its uploaded GCash proof.
- **Active Reservations** — currently live bookings; staff can mark a gown returned, reschedule dates, correct actual pick-up/return dates, undo an accidental pick-up, or send a manual reminder.
- **Reservation Records** — a per-reservation ledger, including searching for a reservation to manually attach a photographed paper receipt.
- **Rental History** — past/completed rental records.
- **Security Deposits** — which reservations are still holding a deposit, and a one-click "release deposit" action.
- **Gown Catalog (Inventory)** — add/edit/delete gowns one physical unit at a time, bulk actions, photo upload, status changes, and "Blocked Dates" (temporarily pulling one unit out of the bookable pool for cleaning/repair/alteration/other reasons).
- **Categories** *(Owner only)* — the picture each category shows on the customer site (Rentals page, Women's and Men's pages, home page): upload, replace, or go back to the standard picture (Section 12).
- **Clients** — the customer list, with a flag/unflag action for accounts under review.
- **Staff Management** *(Owner only)* — create, edit, deactivate, or delete staff/manager accounts.
- **Account Settings** *(Owner only)* — change the owner's own login password.
- **Notifications** — a header dropdown of live work items (pending approvals, unverified payments, overdue returns, late pick-ups, deposits still held, out-of-stock gowns, flagged customers, and, for the owner, deactivated staff accounts). Nothing here is stored — it's recalculated from real data (on every page load, and again every ~15 seconds while an admin page is open, through a small JSON feed at `api/notifications/`), so it can never fall out of sync, but that also means there's no history of past notifications. While a page is open the bell updates by itself and announces anything new with a chime, a toast, the tab title and (opt-in) a desktop pop-up; what each person has already seen is remembered only in their own browser.

## 4. Gown data

Every gown is one row (one physical unit) with these fields:

| Field | What it stores |
|---|---|
| `gown_id` | Unique ID, auto-generated as `CATEGORY-COLORCODE-###` |
| `name` | Product name |
| `slug` | Auto-generated, unique, URL-safe version of the name |
| `category` | One of 13 fixed choices (listed above) |
| `color_name` / `color_code` | Free text — not locked to a fixed list, so an "Other" color can always be entered. A list of 36 named presets (e.g. White/WH, Blush/BL, Royal Blue/RB) drives the Add/Edit dropdown and warns staff if a code is already used by a different color name. |
| `size` | Small, Medium, Large, Extra Large, or Free Size |
| `design_variant` | Optional free-text note (e.g. a style variant) |
| `rental_price` | Decimal peso amount |
| `condition` | New, Good, Fair, or Needs Repair |
| `status` | Available, Reserved, or Out-of-Stock (there is no "In Cleaning" or "Lost" status — see Section 6) |
| `photo_url` | Link to the gown's photo |
| `is_verified` | Yes/no flag |
| `last_returned_at` | Date it was last returned, if any |
| `notes` | Free-text staff notes |
| `created_at` / `updated_at` | Automatic timestamps |

**How names/IDs are created:** `gown_id` and the URL slug are both generated automatically using a counter that takes a database row lock before handing out the next number — this was specifically built to survive two staff members adding a gown in the same category/color at the exact same moment without colliding.

## 5. Rental flow

1. Customer browses a collection or searches, opens a product page, and checks the availability calendar.
2. Adds it to their cart (stored in the browser).
3. Starts checkout — this begins a 20-minute hold on their spot.
4. Fills in contact details and uploads a GCash payment-proof screenshot, then submits.
5. The server re-checks availability and price from scratch (never trusting anything sent by the browser), locks and assigns one specific physical gown unit, and creates the reservation with status **Pending**. The security deposit is a fixed ₱2,000 per gown. An automatic 3-day "Cooldown" block is placed on that gown right after its planned return date.
6. A staff member reviews the payment proof and approves (**Confirmed**) or rejects (**Rejected**) the reservation.
7. The system automatically sends in-app reminder messages: pick-up tomorrow/today, return tomorrow/today, and an overdue notice (capped at 30 days of nagging). These run once a day, triggered the next time any staff member opens the dashboard — there is no real scheduler/cron behind this.
8. Staff track the gown's real-world stage on the Rental Schedule calendar or Active Reservations: **Pick-up → Reserved → Return**. **Overdue is not set automatically** — a staff member has to manually mark an item Overdue once its return date has passed (the automatic reminder message above still goes out regardless of whether anyone does this).
9. When the gown comes back, staff mark it returned as either **Good** or **Needs Repair**. Needs Repair takes the physical gown out of the bookable pool (Out-of-Stock); Good keeps it available once its cooldown period passes.
10. **Cooldown:** every booking automatically blocks its gown for 3 extra days after the return date. Combined with the fact that a new booking's own pick-up window already reaches back 2 days before its event, this adds up to a full 7 days after the event before that same gown can be booked again. Staff can shorten or delete this block by hand once they've actually checked the gown.
11. Once every gown in a reservation is marked Returned, staff can release the security deposit with one click. **The system only tracks whether the full deposit was released, and when** — it does not calculate or record a partial deduction for a late return or damage. Any late fee (₱200/day is the shop's stated policy) is handled entirely by staff outside the system.
12. A customer can cancel anytime before pick-up, as long as no gown in that reservation has already been picked up. Cancelling — and separately, letting a checkout hold expire or cancelling it — both count against the same limit: 5 strikes = a 30-minute lockout on starting a new reservation, 10 strikes = a 2-hour lockout, 15 strikes = the account is permanently flagged for staff review (only an admin can clear a flag).

## 6. Inventory tracking

- Each physical gown is tracked as its own database row with its own `gown_id` — this is **not** a "1 product, quantity 5" model under the hood.
- For display, gowns sharing the same name (case-insensitive) are grouped together into one product card, and the cheapest unit in that group is used to represent its price and photo — this is what produces the "1 product, several available" experience customers see.
- There is no separate "stock count" field; the count is simply how many gown rows share that name, computed on the fly.
- **There is no formal inventory audit trail.** A gown's own edits (price changes, condition changes, status changes) are only reflected in its `updated_at` timestamp — there's no log of who changed what on the gown itself (as opposed to reservation actions, which are logged — see Section 7).
- **There is no dedicated "missing" or "lost" status.** The only statuses are Available, Reserved, and Out-of-Stock — a gown that's actually lost or destroyed would have to be represented as Out-of-Stock with an explanation typed into the free-text `notes` field, if a staff member remembers to.
- **Blocked Dates** is the general mechanism for pulling one unit out of the bookable pool for a date range, tagged Cleaning, Repair, Alteration, Cooldown, or Other. Cooldown blocks are created automatically by the system (see Section 5); the rest are added by staff by hand.

## 7. Logs and history

- Every meaningful thing that happens to a reservation — submitted, approved, rejected, cancelled, picked up, stage changed, marked returned, deposit released, reminder sent — is written to an append-only event log, tagged with who did it (Customer / Staff / System) and when. Nothing in this log is ever edited or deleted after the fact.
- Some log entries are marked staff-only (e.g. an internal note that a gown came back needing repair) and are hidden from the customer's own order timeline; the admin panel's timelines always show everything.
- **This logging only covers reservations, not gowns.** There's no equivalent log for inventory changes — no record of who edited a gown's price, condition, or status, or when.
- The admin notification bell is **not** a log either — it's a live list recalculated from current data on every page load and every ~15 seconds after that (see Section 3), so there's no historical record of past notifications, only what's currently outstanding.

## 8. Search and display

Gowns from different categories appear together in a few places:
- The **"All"** collections tab, showing every category's products grouped into panels on one page.
- The **site-wide search overlay**, which matches on gown name only and shows a photo, name, and price for each match; matching categories are listed separately in a sidebar.
- The **search results page** (`/search/?q=…`, the overlay's "View all"), which lists every matching gown from all categories together using the same name-only rule as the overlay. It opens on **Best Match** (names starting with the typed letters first, then names with a word starting with them, then names containing them anywhere, ties A–Z) and the sort drawer's 5th option, "Best match", appears only on this page; A–Z, Z–A and the two price sorts re-sort the whole mixed list. The sort chosen is remembered per search for the browser session.
- **Sort loading step** (`static/js/collection-sort.js`, styles in `base.html`): on the All page, every collection page and the search results page, applying, resetting or clearing a sort fades the gown cards into grey shimmer placeholders (the Orders page's skeleton look, one per card), re-orders them unseen, then fades them back in. It is skipped when nothing would move, for one card, for visitors who prefer reduced motion, and when a page opens with a remembered sort; a safety timer always puts every card back.
- The **admin Gown Catalog** table, which lists every gown across every category together, with client-side sorting, filtering, and a category chip strip.
- The **admin notification bell**, which pulls together items from across categories (e.g. any out-of-stock gown, regardless of category).

"You may also like" on a product page is the one exception — it only ever suggests other products from the **same** category, not a cross-category mix.

The AI chatbot is a separate case: it describes gowns by name and price from a fixed block of text written into its instructions, not by actually querying the real catalog — see the next section for why that's currently a problem.

## 9. Not built yet / known issues

- **The AI chatbot's product knowledge is stale and wrong.** Its instructions still describe an old placeholder catalog that was retired ("11 collections, each with 8 items named One through Eight," a fixed price ladder, item "Two" always Reserved). The real catalog now has 13 categories with real, staff-entered names and prices. The chatbot was never updated to match, so it will confidently recommend gowns that don't exist.
- **Cash is not actually a selectable payment method**, even though it's a valid choice in the data model and the FAQ/product-page copy tells customers "the system accepts GCash and cash payments." The real checkout form only ever submits GCash — there is no button, dropdown, or any way for a customer to choose Cash.
- **The AI chat silently does nothing useful if its API key isn't set.** With no `GEMINI_API_KEY` configured, every chat message gets a generic "having trouble thinking right now" reply instead of a real answer — and that environment variable isn't even listed in the project's example environment file, so a fresh setup could easily miss it.
- **The AI chat's rate limits aren't reliable in a multi-process deployment.** Its 10-per-minute / 40-per-day limits are enforced using Django's default in-memory cache (no shared cache is configured), so in production each server worker process counts separately, and the daily counter resets on every restart/redeploy instead of at midnight.
- **A fresh database deployment will crash the login page** until someone manually creates a Google-login configuration row through Django's own admin site — this isn't created by any migration or setup script.
- **Classic email/password sign-up is fully configured but unreachable.** The settings require mandatory email verification for it, but there's no sign-up form anywhere on the site — Google sign-in is the only way in. This is effectively dead configuration.
- **The customer profile page's "saved addresses" feature is not real.** It only lives in the browser's local storage, has no backend field or endpoint behind it, and has no connection to the actual address a customer enters at checkout. Clearing browser data loses it, and no other device or staff member can ever see it.
- **A leftover comment in the codebase still describes the AI chat widget as "mock replies, no backend"** — that was true at an earlier stage but isn't anymore; it's now a real, working Gemini-backed feature.
- **Security deposit release has no partial-amount tracking.** The system only records whether the full deposit was released and when — any late-fee deduction has to be worked out and handled by staff entirely outside the system.
- **"Overdue" is not automatic.** The system will message a customer automatically once their return is late, but the visible Overdue status on the staff calendar has to be set by hand.
- **No missing/lost gown tracking, and no audit log for inventory edits** — see Section 6.
- Two unused, leftover dependencies sit in the project's requirements: `mssql-django`/`pyodbc` (the project only ever connects to PostgreSQL or SQLite) and `anthropic` (the real AI feature runs entirely on Google Gemini).
- **Customer emails are built but stay off until they are set up** — see Section 10. Until then, every reminder and notice is an in-app message only.
- A separate vendor UI-kit source project (`arabela_admin_panel_template/`) sits in the repository but is not part of the running application at all — it's not installed, not routed, and any "Coming Soon" text inside it does not describe a gap in the real product.
- Minor: the customer profile template file is misspelled `profile.htmls` instead of `.html`. It works correctly, but it's inconsistent with every other template in the project.

## 10. Customer emails

Customers are also emailed about their own booking. The in-app notices (Messages page and reservation timeline) are always still there; the email is an extra.

| Email | When it is sent |
| --- | --- |
| We received your reservation | right after checkout |
| Reservation approved / rejected (with the reason) | staff approve or reject it |
| Pick-up and return reminders, overdue notice | the daily reminder sweep, and a reminder staff send by hand |
| Security deposit settled | the last gown on the booking is checked back in |
| Account flagged / unflagged | staff flag or unflag it, or the automatic flag at 15 attempts |

Not emailed on purpose: the temporary-lock notice, the "lock removed" message, check-ins (picked up / returned), date changes, and anything the customer did themselves (cancelling).

**How it works** (`accounts/customer_emails.py`): hooks on the two places notices already come from (`CustomerMessage` and `ReservationStatusEvent`), so no existing screen was rewritten. An email is sent only after the database transaction commits, by one background thread that never touches the database. Any failure is logged (Render's Logs tab, `accounts.customer_emails`) and swallowed, so an email can never break an approval, a reminder or a timeline entry. Staff accounts and placeholder addresses (`.invalid`, `example.com`, ...) are never emailed, and a double-clicked Approve sends one email, not two. The look is `templates/emails/email.html` (tables and inline styles, the only thing every mail app agrees on); the logo is attached inside each email, so it shows even while a free Render service is asleep. The shop's address, phone and Facebook link come from Edit Profile.

**Setting it up** (environment variables on Render; no migration):

1. **A way to send.** Render's **Free** plan blocks the normal Gmail connection (SMTP, ports 25/465/587), so pick one:
   - *Paid Render plan:* keep `EMAIL_BACKEND` as is and set `EMAIL_HOST_USER` (the shop's Gmail address) and `EMAIL_HOST_PASSWORD` (a Gmail App Password).
   - *Free — the Google relay:* open `apps_script/mail_relay.gs`, follow the steps at the top of that file (about 5 minutes in the shop's Google account), then set `EMAIL_BACKEND=accounts.email_backends.AppsScriptRelayBackend`, `EMAIL_RELAY_URL` and `EMAIL_RELAY_SECRET`. Emails are then sent by Gmail itself over HTTPS.
2. **Check delivery.** Admin → Edit Profile → *Customer emails* → *Preview and test emails*. It shows the current status and lets the owner preview every email at computer and phone width and press **Send test to me** (it only ever mails the signed-in owner's own address, and works even while the switch below is off).
3. **Switch it on, last.** Set `CUSTOMER_EMAILS_ENABLED=true`. It is **off by default** on purpose: local development shares the live database, so approving a real booking from a laptop must never email a real customer.
4. Optional: `SITE_BASE_URL` (for the links and logo in emails). When blank it is taken from the Site row the deployment uses (`SITE_ID=3` on Render, `arabela-gown-rental.onrender.com`).

**Limits:** Gmail allows about 500 emails a day over SMTP (some sources say 100) and the relay about 100 a day on a normal Gmail account; both are far above this shop's volume. Mail from a Gmail address that is not on an owned domain can land in a customer's Spam folder now and then — the owner's test email is the best check.

## 11. Owner account, passwords and recovery

The shop has **one owner account** — the sign-in for this admin panel. Staff and managers sign in with accounts the owner creates in Staff Management.

**Changing the owner's username or password** (owner only; staff cannot): Admin → Edit Profile → **Login & Security**, or the avatar menu → **Account settings**.

- Both forms ask for the **current password**.
- **Username:** 4–30 letters, numbers, `.`, `-` or `_` (at least one letter), unique ignoring capitals across every account. The owner stays signed in.
- **Password:** at least 10 characters, not one of the most common passwords, not only numbers, not close to the username. Changing it signs out every other device. Staff passwords set in Staff Management follow the same rules (existing staff keep theirs until it is reset).
- **Previous sign-in** (shown on both pages): when the account signed in before the current session, so a sign-in the owner does not recognise can be noticed. It is kept in the session, so there is no database change.
- 5 wrong sign-in attempts for a username lock that username for 15 minutes (the lockout ends 15 minutes after the 5th wrong attempt, and the message says how many minutes are left). The count is kept in the database (`accounts.LoginThrottle`, migration `accounts/0013`), so an app restart or a refresh cannot reset it; before that migration has run it falls back to the server's memory.

**There is deliberately no "Forgot password" email.** If the owner forgets the password, whoever looks after the project resets it:

1. **Get the code:** `git clone` the GitHub repository and install `requirements.txt`.
2. **Recreate `.env`** (it is not on GitHub, on purpose). The database lines are also on Render → *Environment*: `DATABASE_HOST`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_NAME`, `DATABASE_PORT` (reveal each with the eye icon). Supabase → Project Settings → Database has the same details and can reset the database password. Without them the project quietly starts with an empty local database instead of the shop's.
3. **Reset the password:** `python manage.py changepassword <username>` and type the new password twice. If the username is forgotten too: `python manage.py shell -c "from django.contrib.auth import get_user_model as g; print(list(g().objects.filter(is_superuser=True).values_list('username', flat=True)))"`.
4. **Keep a private copy** of those five database lines (a password manager — never GitHub) in case Render and Supabase are ever out of reach.

Local development uses the **live** database, so step 3 changes the real password.

## 12. Category pictures

Every category has a picture on its tile on the customer site: the **Rentals** page, the **Women's** and **Men's** collection pages, and four tiles on the **home** page. Which picture a tile shows is decided in this order (`gowns/covers.py`):

1. the picture the owner uploaded in **Admin → Inventory Management → Categories** (owner only);
2. the picture that ships with the site, `static/images/categories/<category key>.jpg` — every original category except Barong;
3. the plain placeholder, `static/images/categories/placeholder.jpg` (the tile picture the site always had), for a category with neither: Barong, and any category the owner adds until they upload one.

- **Uploading (owner only):** choosing a photo shows the finished picture inside the category's card first (**Save** / **Cancel**); nothing is stored until Save. A photo must be a **tall photo of the whole gown on a plain white background**, **at least 400 × 650 px** (a note says a bigger one looks sharper below 600 × 900), JPG, PNG or WEBP, up to 5 MB. Each rule has one short message (`gowns/cover_images.py` → `prepare_cover`): not tall, too small, background not white, gown cut off at the edge, gown too small in the photo. The same rules run on the server for the preview and for Save, so they cannot be skipped. An accepted photo is turned into the exact tile picture: 600 × 900, the gown centred in the upper part, its white shaded to the tile grey (`#FBFBFB`), the strip at the bottom left free for the category name. The same code made the pictures that ship with the site, so uploads and bundled pictures look alike. (Checked against the shop's own 460 photos: 459 pass; the one that does not really is cut off.)
- **Viewing (owner only, same page):** each card with a picture has a **View** button next to Upload, and clicking the picture does the same. It opens a big view built like Payment Verification's "View Payment" window: the picture uncropped (never bigger than the file, sized so the whole view fits the screen), the category name strip as customers see it, which customer pages show it, whether it is **Standard**, **Custom** or a **Preview (not saved yet)**, its size in pixels, and a **View Full Image** link to the saved file (not shown for an unsaved preview). A chosen-but-unsaved photo can be viewed big before pressing Save. Closes with the x, Close, Esc or a click outside, and the keyboard focus goes back to the View button. A category with no picture (Barong, or one the owner added) has no View button. It is all in `templates/arabela_admin/categories.html` (styles namespaced `.cvr-view-*`); there is no server code or database change.
- **Storing:** uploads go through the same storage as every other admin upload (Cloudinary on Render, the local `media/` folder otherwise) under `category_covers/`, and the name the storage gave each file is kept so replacing or resetting a picture deletes the old file. Removing a category the owner added removes its picture; removing one of the original categories keeps it, so adding that name back restores it.
- **Deploying:** this adds one table (`CategoryCover`, migration `gowns/0023_category_cover.py`). Run `python manage.py migrate` against the database **before** pushing to Render. If the code goes live first nothing breaks — customers just see the standard pictures, and the Categories page says uploads need the database update.
- **Renamed category:** *Ball Gown* is now **Evening Gown** (its address is `/collections/evening-gown/`; the old `/collections/ball-gown/` link and its product links still work). Two migrations carry the rename into the data — `gowns/0024` (gown category, ID prefix and `Ball Gown NN` names, number counters, Removal Log, removed-category list, tag colour, category picture) and `reservations/0023` (the gown name on existing bookings). Web addresses of individual gowns are unchanged, and *Ball Gown Tulle* is a separate category that was not touched. Deploy order for a rename like this: push, wait until Render is live, then run `python manage.py migrate` right away (the old site code would not find the renamed gowns).
- **Changing a picture that ships with the site:** `build_cover` and `cover_jpeg_bytes` in `gowns/cover_images.py` make the 600×900 JPEG from a photo; save it as `static/images/categories/<key>.jpg`. For a brand-new original category, also add its key to `BUNDLED_COVER_KEYS` in `gowns/covers.py`.

## 13. Backups and recovery

Supabase's free plan keeps **no backups** of the database, so the shop makes its own.

**Taking a backup** (read-only, safe any time, also against the live database):

```
python manage.py backup_database
```

It saves one compressed file, `arabela-backup-YYYYMMDD-HHMMSS.json.gz`, in `C:\Users\<you>\ArabelaBackups` (or the folder in the `ARABELA_BACKUP_DIR` setting, or `--output-dir`), reads the file back to prove it is complete, and keeps the newest 30 (`--keep N`). It holds every gown, reservation, customer, receipt, message and setting. It leaves out what `migrate` rebuilds (content types, permissions), current logins (sessions), the admin click log and failed-login counters. **The photos and payment proofs are not inside it**: they live in Cloudinary, which keeps them; the backup holds their addresses.

**Automatic daily backup (on the owner's laptop).** A Windows scheduled task named **Arabela Daily Backup** runs `C:\Users\<you>\ArabelaBackups\run-backup.bat` every day at **9 PM**, or the next time the laptop is on if it was off then. It saves into the Google Drive folder and keeps a short log in `C:\Users\<you>\ArabelaBackups\backup-log.txt` (the last line says `exit code 0` when it worked). The laptop must be on, signed in, and Google Drive for desktop running; if the Drive folder is not available the backup is saved in `C:\Users\<you>\ArabelaBackups` instead, with a warning in the log. Check it in *Task Scheduler → Task Scheduler Library*, run it now with `Start-ScheduledTask -TaskName "Arabela Daily Backup"`, and remove it with `Unregister-ScheduledTask -TaskName "Arabela Daily Backup" -Confirm:$false`. The task and its `.bat` file belong to that one laptop; they are not part of the project files.

**The file holds customers' personal details and password hashes. Keep it private**: never put it on GitHub (this repository is public), a shared drive or an email. Times are kept to the millisecond.

**If the database is lost** (a new, empty database is the only thing a restore goes into):

1. Create a new empty PostgreSQL database (for example a new free Supabase project).
2. Put its connection details in `.env` (`DATABASE_HOST`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_NAME`, `DATABASE_PORT`).
3. `python manage.py migrate`
4. `python manage.py restore_database C:\Users\<you>\ArabelaBackups\arabela-backup-....json.gz`

   It refuses a database that already has accounts, gowns or reservations (so it can never overwrite live data), clears what `migrate` pre-fills, loads the backup, and checks every table's count against the file. Customers are never emailed during a restore.
5. Point Render's `DATABASE_*` settings at the new database and redeploy.

The restore was tested on the real data: 484 records in 18 tables came back with every count, key and value matching. Run `backup_database` before any risky change (a big migration, a bulk delete) so there is always a fresh copy to go back to.

## 14. Error alerts and the uptime check

Two small safety nets, both free, so you hear about a problem before a customer has to tell you.

**The health page.** `/healthz/` answers `{"status": "ok", "error_alerts": "on" or "off"}` when the site is up and can reach its database, and `{"status": "error"}` (HTTP 503) when it cannot. It needs no login, shows nothing private, and asks the database one tiny question. A regular visit also stops Render's free plan from putting the site to sleep (the first visitor after a quiet period otherwise waits about 30 seconds).

**Uptime monitor (UptimeRobot, free):**

1. Sign up at uptimerobot.com, then **Add New Monitor** → type **HTTP(s)**.
2. URL: `https://arabela-gown-rental.onrender.com/healthz/`, check every **5 minutes**, and make sure your email is the alert contact.
3. Optional but good: in Render → the service → **Settings** → **Health Check Path** → `/healthz/`.

**Error alerts (Sentry, free):** when the live site crashes for a visitor, an email arrives with the exact page, line and code version.

1. Sign up at sentry.io (free plan), **Create Project** → platform **Django** → name it `arabela`. Copy the project's **DSN** (an address that starts with `https://`).
2. In **Render → the service → Environment**, add `SENTRY_DSN` with that address and save (Render redeploys). Do **not** put it in your laptop's `.env`: your laptop shares the live database, so its local errors would arrive as if they were the live site's.
3. Check it: open `/healthz/` on the live site; it should now say `"error_alerts": "on"`. To see an alert arrive, run on your computer: `$env:SENTRY_DSN = "the-address"; python manage.py send_test_alert` (PowerShell) and look in your Sentry inbox and email.
4. In Sentry, make sure email notifications are on for new issues (Alerts → the default rule, and your account's Notification settings).

What is sent is kept small on purpose (`arabela_system/monitoring.py`): the error, the page, and which version of the code was running. **No names, emails or IP addresses, and never what was typed into a form** (this was checked by sending a real crash to a stand-in Sentry and reading what arrived). Noise such as strangers' bots sending a made-up website name is dropped. Without `SENTRY_DSN` nothing is sent at all, so local development and the tests never report anything.

## 15. Page speed

The database is far from the web server (each question to it costs about 0.1 s), so speed comes from asking it fewer questions.

- **One page visit asks each category question once** (`gowns/request_memo.py`). The menu, the search box, the tiles and the page all read the category lists; they used to ask the database up to 15 times on one page. The memory lasts for that one visit only and is wiped the moment a category is added or removed, so nothing can ever show out-of-date categories.
- **The All page and Search fetch the gown list once** for every category (`_products_by_category` in `gowns/views.py`) instead of once per category, with the same filter, order and grouping as each category page.
- Measured on a copy of the real data (2026-10-10), every page byte-for-byte identical before and after: All page 45 → 5 database questions (4.7 s → 0.5 s), Search 49 → 9 (5.1 s → 0.9 s), category pages 11 → 5 (1.1 s → 0.5 s).
- **Static files are fingerprinted** (`arabela_system/storage.py`): `collectstatic` saves each file again with a short fingerprint in its name and pages point at that copy, which browsers keep for good (a changed file gets a new name, so nobody sees an old version). If a page ever names a file that does not exist it gets the plain name instead of crashing; a stylesheet pointing at a missing file stops `collectstatic` itself, so a broken build never goes live.
- **Gown photos are sent smaller** (`gowns/photos.py`): the customer pages ask Cloudinary for a resized, auto-compressed copy (800 px wide in the grids, 1600 px on the product and order pages, WebP where the browser supports it). Measured on the 29 live photos: 5.4 MB → 0.7 MB for the grids. The stored photo and the admin pages keep the original.
- The first visit after a quiet spell on Render's free plan still waits about 30 s while the server wakes; a paid instance, or a visit a few minutes before a demo, avoids that.

## 16. Error pages

`templates/404.html` (a page that does not exist) keeps the normal menu and footer and offers "Browse all gowns" (or "Back to the dashboard" inside the admin panel). `templates/500.html` (a crash) stands alone on purpose, with no database, no shared layout and its styles written inline, so it still appears when something is broken. Both only show on the live site: with `DEBUG=True` Django shows its own developer pages instead.

## 17. Laptop database (so local testing never changes the live shop)

Until this is set up, the laptop's `.env` points at the live database, and anything clicked locally really happens on the live site. To give the laptop its own copy:

1. In Supabase, create a second free project (same region as the live one), and keep its database password somewhere safe.
2. Copy `.env.laptop-db.example` to `.env.laptop-db` and fill it in from the new project's **Connect → Session pooler** page. The file is git-ignored and only its five `DATABASE_*` lines are read.
3. Build the tables and copy the live data across:
   ```
   python manage.py migrate
   python live.py backup_database
   python manage.py restore_database "<the backup file it printed>"
   ```

From then on `python manage.py runserver` says "Using the LAPTOP database" and the live shop is not touched. On the laptop, deleting a photo keeps the Cloudinary file (the live shop still shows it) and customer emails stay off.

To do something to the live database on purpose, use `live.py` for that one command: `python live.py migrate` after a new migration (run plain `python manage.py migrate` too, for the laptop copy). The daily backup task already runs `live.py backup_database`, and `backup_database` refuses to back up the laptop copy. Delete `.env.laptop-db` to go back to the old behaviour. Free Supabase projects pause after a week without use; press **Restore** in the dashboard to wake it.
