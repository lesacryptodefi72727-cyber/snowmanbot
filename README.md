# SnowmanBot — receipt prompts and clear delivery wording

Version: `snowman-2026-10-25-receipt-prompts`

This release adds automatic receipt/screenshot prompts after payment details and spells out estimated delivery time throughout the store. It also retains the general-store changes from the latest five screenshots. It removes the country selector, country labels such as Ghana, and country-specific product/vendor navigation. Customers use one shared store, see physical-product prices in USD, enter their city and a map pin, and contact support for local bank details. Country/vendor expansion is paused for a later phase.

The target markets are Oman, Saudi Arabia, United Arab Emirates, Bahrain, Kuwait and Qatar. The app does not invent local vendors or claim a staff location. Delivery availability and estimated delivery time are confirmed by support for the customer's city.

## Upload and deploy

Extract the ZIP and upload **all seven application files** to the root of your GitHub repository:

1. `bot.py`
2. `crypto.py`
3. `storefront.py`
4. `fulfillment.py`
5. `restore_backup.py`
6. `requirements.txt`
7. `Dockerfile`

Also upload this README. Replace the existing files, commit and deploy the latest commit. Upload files from the extracted ZIP, not the ZIP itself or code copied from a preview.

Keep the existing PostgreSQL database and hosting variables. The schema adds city and editable-media storage without deleting products, customers, orders, receipts or legacy settings. Archived country tags no longer filter the store. On Render, use `/health` and one running replica for the two bots.

After deployment, open the admin bot and press **Start**. Its menu must show:

```text
Version: snowman-2026-10-25-receipt-prompts
```

The startup log and `/health` response also include that version. If you still see the old version, the previous code is still deployed. Old Telegram message bubbles remain in chat history; open the new store menu to see the updated content. Old country buttons redirect into the general store/city flow.

## Customer experience

- The welcome message has no country suffix or country selector.
- **Browse products** shows published products, photos and USD prices without creating an order. Archived regional tags do not hide products.
- Physical-product payment options are crypto or **Bank transfer — contact support**.
- A physical checkout asks for the **city**, then a **map pin**. There is no country-selection step.
- The customer chooses **Same-day / as soon as available**, or types a preferred delivery time in the city's local time. Support confirms the available time and estimated delivery time. No country or timezone picker is required.
- The customer reviews the city, map point, requested time, discount and USD total before confirming.
- A previous saved city/map point can be reused.
- **Delivery & refund policy** and support are available from the main menu and order screens.

Default delivery text:

> Same-day delivery in supported cities. Delivery time depends on your city; support confirms availability and the estimated delivery time.

Default store policy:

> Delivery is guaranteed. If you do not receive the quality you ordered, contact support for a refund. Support arranges and processes refunds.

These are editable store-policy statements. The app does not automatically send bank/crypto refunds. A paid-order **Request a refund** button opens a private support discussion. The operator must arrange and process refunds. Native Telegram Stars refunds remain available for eligible digital orders.

If a customer uploads a receipt while several orders are unpaid, the bot asks which order to attach it to and uses the upload already sent. It does not guess the order or require the customer to resend the screenshot. Once payment is confirmed, an older receipt prompt shows the current order status instead of changing it back to unpaid. Telegram Stars payments continue to use Telegram’s own confirmation and do not ask for an external bank/crypto receipt.

Delivery messages and buttons spell out **estimated delivery time**. Previously saved delivery text and schedule labels are also displayed with the full wording.

## Your editable control panel

Open the admin bot → **Start → Store content & appearance**.

You can edit, preview and save:

- Store display name.
- Welcome message.
- Delivery message.
- Delivery/refund policy.
- Support contact.
- Welcome sticker or photo.

Text changes have a preview and Save button. No command syntax is needed. This changes messages inside the store; the public Telegram profile name/bio/description are managed separately in BotFather.

For a welcome sticker, choose **Upload welcome sticker / photo** and send a real Telegram sticker, including a static, animated or video sticker. For a welcome picture, upload a photo. The app stores the file bytes so the main bot can send it even though it was uploaded to the separate admin bot. Use **Preview welcome** to check it or **Remove welcome media** to clear it. No third-party sticker is silently selected: the operator chooses the visual.

Use **Manage products** to change product names, prices, descriptions, photos, publishing status and digital delivery. A new product can be added through guided prompts and reviewed before publishing. Admin access is limited to `ADMIN_IDS`.

## Bank payments without country/currency setup

**No saved bank account or exchange rate is required for a customer to request bank payment.**

1. The customer taps **Bank transfer — contact support**.
2. The admin receives a notification immediately, with a **Message customer** button.
3. The customer is told: **Message support for local bank details**. They can message support before completing checkout.
4. Their order total remains in USD. Support agrees the bank, any local-currency transfer amount and payment instructions directly with the customer.
5. After an order is created, open it under **Orders, payments & delivery**. Choose **Write bank instructions**, or **Choose bank template**.
6. Review and explicitly send the details. Only then does the customer's order page show the account instructions/image and native Copy account button.
7. The bot explicitly asks the customer to send a **payment receipt or screenshot after paying**. They can send the photo/file directly in chat, or tap **Upload receipt / screenshot**. No caption or command is required. The prompt is shown again when support releases the bank details. While details are pending, the prompt tells the customer to wait before paying.
8. Check the actual bank receipt of funds, then use **Mark payment received** or **Decline payment**, add a review note and confirm.

Private templates can be saved for USD, OMR, SAR, AED, BHD, KWD or QAR under **Saved bank templates**. No exchange-rate field is required. Templates are not automatically published and do not change the USD order price. If a transfer is to be made in another currency, explicitly agree that local amount with the customer. The Copy USD order total button is labelled so it is not confused with a local transfer amount.

Older bank/rate data is preserved for historical orders but is not required by the new checkout.

## USD pricing and discount

Physical order prices are in USD. A physical subtotal **strictly greater than $1,000** gets **$150 off**, before crypto conversion.

| Subtotal | Discount | USD total |
|---|---|---|
| $1,000.00 | $0.00 | $1,000.00 |
| $1,000.01 | $150.00 | $850.01 |
| $1,200.00 | $150.00 | $1,050.00 |

Each checkout currently purchases one product. Digital products continue to use Telegram Stars under their existing payment flow. Crypto checkout displays the USD order value together with the exact coin/token amount to send.

## Crypto

Supported new payments: **USDT on TRC20/TRON, BTC, SOL and ETH**. TON remains removed from new payment options. Historical TON orders can still be reviewed and verified so funds already sent are not abandoned.

Active crypto quotes include an address QR, native copy-address/copy-amount buttons and a 30-minute countdown. The QR contains the address; customers enter the copied exact amount on the stated network and pay network fees separately. The chat countdown refreshes roughly every 15 seconds while the service is running.

Public/free-tier APIs discover candidate payments, which are verified separately for receiving address, exact atomic amount, correct network/token, transaction success, invoice timing and confirmations. Bitcoin requires 3 confirmations, Ethereum 12, Solana finalization, and TRC20 USDT a confirmed official-token transfer. The app notifies the customer when payment is confirmed. A known hash can still be checked if a public history index is delayed. After the payment address and amount are shown, the bot asks the customer to upload a receipt or screenshot. They can send it directly in chat or use **Upload receipt / screenshot**. Transaction hashes can still be pasted or submitted with the hash button. A receipt is supporting evidence and does not replace blockchain verification.

Manual crypto approval needs a transaction hash and prevents reusing an already credited transaction. It is labelled as an admin decision, not network verification. A screenshot alone never automatically confirms a deposit. Free API availability/rate limits affect detection timing; no paid QR/deposit API is required by the code. `COINGECKO_API_KEY` remains optional for price conversion, with a free Demo key available if needed.

## Delivery tracking

On a paid physical order, the admin can view the customer's drop-off pin, set/update the estimated delivery time, share a delivery map point or share a real Telegram live location. A static pin is labelled a one-time update. A real live location is relayed only from the authorised admin's specific source message, and subsequent edited-message updates move the customer's map.

Use Telegram attachment → Location → **Share Live Location** when genuine movement is needed. No GPS position is generated. **Stop live tracking** ends the map; stopping the source live sharing also stops the customer map on the next update. Only IDs in `ADMIN_IDS` can operate this; there is no separate courier-role account in this release.

## Backups

**Export backup** sends an on-demand consistent database snapshot to the authorised admin chat. Private daily backups are also queued once per UTC day, at `02:00 UTC` by default. Configure `AUTO_BACKUP_UTC=HH:MM`, or `AUTO_BACKUP_UTC=off` to disable automatic exports. Admins must have started the admin bot.

Backups include products, customers, orders, receipts/images, city/map data, schedules, payment audit, private messages, bank templates, editable content and welcome media. Tokens, environment variables and private wallet keys are not exported. Keep downloaded backups, your code release and private variable configuration separately.

Inspect a backup:

```bash
python restore_backup.py SnowmanBot_backup_YYYYMMDD_HHMMSS.zip --inspect
```

Restore into an **empty** PostgreSQL database with the service stopped and the destination `DATABASE_URL` set in your environment:

```bash
python restore_backup.py SnowmanBot_backup_YYYYMMDD_HHMMSS.zip --restore-empty
```

The restore refuses a nonempty destination. It pauses old queued notifications and disables stale live-map sessions/polling offsets for review. The Telegram export is limited to 40 MiB compressed and 512 MiB of logical data; use a database-provider backup if larger.

Data backup cannot reverse a Telegram account restriction. A replacement bot needs valid tokens, and customers must start it before it can contact them. It does not restore a banned username.

## Hosting variables and operation

Required: `MAIN_BOT_TOKEN`, `ADMIN_BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL`.

`ADMIN_IDS` contains personal numeric Telegram user IDs, not bot IDs or usernames. Wallets: `USDT_ADDRESS`, `BTC_ADDRESS`, `SOL_ADDRESS`, `ETH_ADDRESS`. Also configure `SOL_RPC_URL` and `ETH_RPC_URL` for the correct mainnets. Optional provider keys: `TRONGRID_API_KEY`, `COINGECKO_API_KEY`. Keep legacy TON variables only if historical payments need checking; they do not re-enable TON.

`SUPPORT_CONTACT` is the initial support text; it can then be overridden through the admin content panel. `AUTO_BACKUP_UTC` defaults to `02:00`. `DELIVERY_TIMEZONE` defaults to `Asia/Dubai` for legacy time helpers; the new preferred-time prompt asks for the city's local time and relies on support confirmation. Daily reminders remain UTC and opt-in. Payment/order updates are independent of marketing subscriptions.

Legacy `LOCAL_PAYMENTS_JSON` and `FX_RATES_JSON` seeds are retained for compatibility. They no longer determine whether a customer can request bank payment.

Polling, countdowns, deposit scans, reminders and scheduled backups require a running service. A health endpoint does not keep an idle hosting plan awake.

## Validation

All 39 local workflow and payment checks passed for this release. Local integration checks use embedded PostgreSQL and simulated Telegram/payment responses. They cover the receipt prompts, direct photo/file uploads, explicit selection between multiple unpaid orders, bank/crypto review, older delivery wording, and existing Ghana profiles, shared browsing with photos, USD checkout without bank/FX setup, city/map/preferences, stale buttons/forms, private bank release, editable policy/support content, welcome sticker/photo storage, authorisation, refund discussion, crypto checks, and backup/restore of the added content/media. These are not live Telegram or real-money tests. Test the deployed version in Telegram before opening it to customers.
