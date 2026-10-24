# SnowmanBot — location-drop delivery release

Version: `snowman-2026-10-24-location-delivery`

This update implements the two supplied screenshots: physical delivery to a map point, requested delivery times, private admin/customer bank coordination, a $150 discount above $1,000, product browsing, country profiles, an upcoming/verified vendor directory, backups, and removal of TON from new checkouts.

## Deploy the complete release

Extract the ZIP. Upload **all seven application files** to the root of your GitHub repository, replacing the existing files and adding the new ones:

1. `bot.py`
2. `crypto.py`
3. `storefront.py`
4. `fulfillment.py` — new and required
5. `restore_backup.py` — new backup restoration utility
6. `requirements.txt`
7. `Dockerfile`

Also upload this `README.md`. Upload the extracted files, not the ZIP itself and not text copied from a preview. The Dockerfile checks Python syntax during build.

Keep the same PostgreSQL database and existing hosting variables. Commit and deploy the latest version. The additive schema migration preserves products, users, orders, images, receipts and payment settings. On Render, keep `/health` as the health-check path and use one running replica for the two bots.

## Customer flow

- Customers can **Browse products** without starting an order.
- The home screen explains physical location-drop delivery. It is not home delivery.
- The **Country profile** button chooses the country storefront. Products assigned to a country are listed in that profile; products set to **All country profiles** remain visible everywhere. Country selection does not promise coverage: the store confirms delivery availability.
- After choosing a physical product and payment method, the customer sends a Telegram map pin. Use attachment → Location to choose a drop-off point. The current-location button is optional and should only be used when that is the intended drop-off point.
- The customer chooses **As soon as available** or a requested date/time, reviews the map point, total and requested time, then confirms checkout.
- Requested delivery slots cover the next seven days, with hourly options from 08:00 to 22:00. The admin confirms availability and the final ETA.
- The order keeps its own snapshot of country, map point, requested time, subtotal and discount. Changing a profile later does not move an existing order.

`DELIVERY_TIMEZONE` controls delivery scheduling. Default: `Africa/Lagos`. The timezone is shown in the prompts and time labels. Daily reminders and backup scheduling use UTC separately.

## Discount

A physical order with a USD subtotal **strictly greater than $1,000** receives **$150 off** before local-currency or crypto conversion.

| Subtotal | Discount | Total before currency conversion |
|---|---|---|
| $1,000.00 | $0.00 | $1,000.00 |
| $1,000.01 | $150.00 | $850.01 |
| $1,200.00 | $150.00 | $1,050.00 |

Each checkout currently purchases one product; the threshold applies to that order's subtotal. Digital products continue to use Telegram Stars and do not receive a USD discount. Exact crypto invoice amounts may include the small atomic-unit adjustment used to identify the invoice.

## Bank transfers are coordinated manually

Customers are not automatically shown your bank account information.

1. Set up a currency, bank and exchange rate under **Bank accounts & rates**. A valid rate is needed to quote the amount. Stored details are private until you send them for an order.
2. When a customer chooses bank transfer, the admin receives a notification with a **Message customer** button.
3. After the customer confirms the map point and requested time, open the order under **Orders, payments & delivery**.
4. Use **Message customer** to discuss the transfer. Customer replies are relayed to the admin bot.
5. Choose **Send saved bank details**, review the details, then confirm sending. Alternatively, choose **Write bank instructions**, enter the instructions and copyable account number, optionally attach an image, review, then send.
6. Only after admin release does the customer's payment page show the bank text/image and native Copy account button. The customer can then upload a receipt without a caption command.
7. Check your bank and use **Mark payment received** or **Decline payment**, enter a review note and confirm.

Bank details, country and prices are snapshotted for an order. Changing the master bank setup does not silently change instructions already attached to an order. Bank details are not sent by a delayed notification after an order has already been paid.

## Delivery map and ETA

Open a paid physical order:

- **View drop-off point** displays the customer's chosen map pin.
- **Set / update ETA** sends the confirmed time to the customer and saves it on the order. Include date and timezone in your message.
- **Share delivery location** asks you to attach a location in the admin chat. A normal map pin sends a one-time map update. For actual movement, use Telegram attachment → Location → **Share Live Location**.
- Live tracking relays edits to that specific authorised admin message to the customer's live map. No GPS location is invented or inferred. Updates continue only while the actual source live location is shared and its period remains active.
- **Stop live tracking** ends the customer live map. Stopping the source live location also ends the customer map on the next edit update.
- **Mark delivered / tracking** records fulfillment information for a paid order.

Only IDs in `ADMIN_IDS` can operate delivery updates. This release does not provide a separate courier-role account; an approved admin shares the delivery location.

## Crypto payments

New checkouts offer **USDT (TRC20/TRON), BTC, SOL and ETH** when their receiving wallets and required RPCs are configured. **TON is removed from new payment menus**, including stale pay buttons. Historical TON orders remain available for deposit verification and review, so funds already sent are not abandoned.

Active crypto quotes retain address QR codes, native Copy address / Copy exact amount buttons, a 30-minute countdown and payment confirmation notifications. The countdown refreshes roughly every 15 seconds while the service runs. The QR contains the address, not the amount. Use the stated network and exact amount; network fees are separate.

The bot scans public/free-tier APIs for candidate payments, then verifies recipient, exact amount, network, success, timing and confirmations before crediting. It also supports hash submission and receipt upload. Public APIs can throttle or lag; a hash can still be verified if the history index is unavailable. Bitcoin needs 3 confirmations; Ethereum needs 12; Solana needs finalization; USDT needs a confirmed official TRC20 transfer. On-time transfers can finish confirming after the quote expires. Late or unverified transfers need review.

Manual crypto approval requires a transaction hash and prevents using a previously credited transaction twice. Manual approval is labelled as an admin decision, not network confirmation. A screenshot alone never automatically confirms a payment.

No paid QR or deposit API is required by the code. `COINGECKO_API_KEY` remains optional for USD conversion; a free Demo key can help when keyless pricing is throttled.

## Country profiles and vendors

This implements the **one bot with a country selector** option from the screenshots. It does not create separate Telegram bots or separate database tenants for each country.

Use **Manage products → product → Set country profile** to limit a listing to a country, or choose all country profiles.

Use **Vendors** in the admin panel to add a real vendor name, country, public contact and description. Vendors start as **Upcoming**. After your own checks, explicitly mark one **Verified by SnowmanBot**. This is your store's approval label, not Telegram verification or an external identity check. You can hide a vendor later. Customer directories follow the selected country profile and show global vendors too. An empty directory says it is coming soon; no invented vendors are seeded.

## Backups and recovery

**Export backup** in the admin panel creates a consistent PostgreSQL snapshot and sends the ZIP to the authorised admin chat. It includes products, users, orders, receipts/images, bank settings, map points, schedules, payment audit, messages and vendors. Environment variables, bot tokens and private wallet keys are not exported.

A private daily backup is queued once per UTC day for each authorised admin. Default time: `02:00 UTC` (03:00 in Nigeria). If the service starts later that day, it queues that day's backup on startup. Set `AUTO_BACKUP_UTC=HH:MM` to change it, or `AUTO_BACKUP_UTC=off` to disable automatic exports. Admins must have started the admin bot. Uncertain/failed backup notifications can be deliberately retried through **Notification queue**; on-demand export remains available.

Save your downloaded backups, this GitHub code release and private hosting-variable configuration separately. Telegram exports are limited to 40 MiB in this release; use your database provider's backup facility if the data grows larger.

Inspect a downloaded backup:

```bash
python restore_backup.py SnowmanBot_backup_YYYYMMDD_HHMMSS.zip --inspect
```

Restore into an **empty** PostgreSQL database, with the bot service stopped and the destination `DATABASE_URL` set in your environment:

```bash
python restore_backup.py SnowmanBot_backup_YYYYMMDD_HHMMSS.zip --restore-empty
```

The restore refuses a nonempty destination and does not delete existing data. Old queued notifications are paused for review, live-map sessions are disabled, and obsolete polling offsets are cleared. Reconfigure hosting variables, start the service and test the restored bot.

A backup preserves data and cannot reverse a Telegram account restriction. A replacement bot needs valid tokens, and customers must start that new bot before it can contact them. Backups do not recreate a banned username or let a new bot contact people automatically.

## Hosting variables

Required: `MAIN_BOT_TOKEN`, `ADMIN_BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL`.

Use personal numeric Telegram user IDs in `ADMIN_IDS`. Customer/user IDs are distinct from bot IDs. Unapproved accounts can see only their own ID, not management actions.

Wallets: `USDT_ADDRESS`, `BTC_ADDRESS`, `SOL_ADDRESS`, `ETH_ADDRESS`. RPCs: `SOL_RPC_URL` for Solana mainnet and `ETH_RPC_URL` for Ethereum mainnet. Optional free-tier keys: `TRONGRID_API_KEY`, `COINGECKO_API_KEY`. Keep legacy `TON_ADDRESS` / `TONCENTER_API_KEY` if historical TON orders need checking; they do not re-enable TON for new purchases.

Other settings:

- `SUPPORT_CONTACT`: customer support contact.
- `DELIVERY_TIMEZONE`: default `Africa/Lagos`.
- `AUTO_BACKUP_UTC`: default `02:00`; use `off` to disable.
- Legacy `LOCAL_PAYMENTS_JSON` / `FX_RATES_JSON` only seed missing settings. Use the admin bank forms to update accounts and rates.

Reminders remain opt-in, use UTC and can be configured through **Daily reminder**. Payment/order messages are independent of marketing subscriptions. All polling, timers, scans, scheduled exports and reminders require a running service; an HTTP health endpoint does not keep an idle hosting plan awake.

## Validation

Local integration checks use an embedded PostgreSQL engine and simulated Telegram/network responses. They exercise migrations, map requirements, requested delivery slots, country restrictions, strict discount boundaries, manual bank release, messaging, receipt review, live-location edits, authorisation, vendor status, backup export and restore refusal on a nonempty database. Payment verification fixtures cover the current networks and legacy TON recovery. These are not live Telegram or real-money tests. Test the deployed bot in Telegram before opening it to customers.
