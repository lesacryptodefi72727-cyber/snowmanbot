"""Button-driven Telegram storefront, payment receipts and admin workflows."""
import asyncio
import io
import json
import secrets
from datetime import datetime, timezone
from decimal import Decimal

import qrcode
from qrcode.image.pure import PyPNGImage

UI_SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_accounts (
    currency TEXT PRIMARY KEY,
    instructions TEXT NOT NULL DEFAULT '',
    bank_name TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    account_number TEXT NOT NULL DEFAULT '',
    image BYTEA,
    enabled BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    admin_id BIGINT PRIMARY KEY,
    token TEXT NOT NULL,
    data JSONB NOT NULL,
    image BYTEA
);
CREATE TABLE IF NOT EXISTS order_bank_details (
    order_id BIGINT PRIMARY KEY REFERENCES orders(id),
    data JSONB NOT NULL,
    image BYTEA
);
CREATE TABLE IF NOT EXISTS invoice_messages (
    order_id BIGINT PRIMARY KEY REFERENCES orders(id),
    chat BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    due TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS payment_audit (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NOT NULL REFERENCES orders(id),
    admin_id BIGINT,
    action TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    txid TEXT,
    created TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_source TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_seen BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS buttons JSONB;
"""

OPEN = ('pending', 'review', 'expired', 'needs_review')
MENU = [("← Admin menu", "admin:menu")]
CANCEL = [("✖ Cancel", "ui:cancel")]


def unpack(value):
    return json.loads(value) if isinstance(value, str) else dict(value or {})


def copy_button(label, value):
    if not 1 <= len(str(value)) <= 256:
        raise ValueError("Copy value must contain 1–256 characters.")
    return {"text": label, "copy_text": {"text": str(value)}}


def qr_png(address):
    # Address only: works across wallet scanners without guessing token URI formats.
    image = qrcode.make(address, image_factory=PyPNGImage, box_size=10, border=4)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue()


def countdown(expires, moment):
    seconds = max(0, int((expires - moment).total_seconds()))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


class StoreUI:
    def __init__(self, app):
        self.b = app

    def keys(self, items):
        return self.b.keyboard(items)

    async def send(self, chat, text, items=None):
        return await self.b.admin_bot.send(chat, text, self.keys(items or MENU))

    async def menu(self, chat):
        return await self.send(chat, "☃️ SnowmanBot Admin\nWhat would you like to manage?", [
            ("➕ Add product", "admin:add"), ("📦 Manage products", "ui:products:0"),
            ("🏦 Bank accounts & rates", "ui:banks"), ("🪙 Crypto wallets", "ui:wallets"),
            ("🧾 Orders & payment review", "ui:orders:0"), ("🔔 Daily reminder", "ui:reminder"),
            ("📊 Statistics", "ui:stats"), ("📨 Notification queue", "ui:jobs"),
            ("🆔 My Telegram ID", "admin:myid"), ("❔ Help", "ui:help")])

    async def banks(self):
        rows = await self.b.db.fetch("SELECT key,value FROM settings WHERE key LIKE 'method:%' OR key LIKE 'rate:%'")
        config = {r['key']: r['value'] for r in rows}
        stored = {r['currency']: dict(r) for r in await self.b.db.fetch("SELECT * FROM bank_accounts")}
        accounts = {}
        for code in self.b.CURRENCIES:
            account = stored.get(code, {'currency': code, 'instructions': config.get('method:'+code, ''), 'bank_name': '', 'account_name': '', 'account_number': '', 'image': None, 'enabled': True})
            if code not in stored and not account['instructions']:
                continue
            raw_rate = config.get('rate:'+code, '1' if code == 'USD' else '')
            try:
                rate = self.b.positive(raw_rate)
            except (ValueError, ArithmeticError):
                rate = None
            account['rate'] = rate
            account['ready'] = bool(account['enabled'] and rate and (account['instructions'] or account['account_number'] or account['image']))
            accounts[code] = account
        return accounts

    async def local_methods(self):
        return {code: (self.bank_text(a), a['rate']) for code, a in (await self.banks()).items() if a['ready']}

    def bank_text(self, account):
        parts = [f"Bank: {account['bank_name']}" if account.get('bank_name') else '',
                 f"Account name: {account['account_name']}" if account.get('account_name') else '',
                 f"Account number: {account['account_number']}" if account.get('account_number') else '',
                 account.get('instructions', '')]
        return '\n'.join(p for p in parts if p) or 'See the bank details image above.'

    async def bank_menu(self, chat):
        accounts = await self.banks()
        items = [(f"{'✅' if a['ready'] else '⚠️'} {self.b.CURRENCY_LABELS.get(code, code)}", f"ui:bank:{code}") for code, a in accounts.items()]
        await self.send(chat, "🏦 Bank accounts\n✅ Ready at checkout\n⚠️ Disabled or missing an exchange rate\n\nSetup includes bank details and the exchange rate, so customers see the account immediately after saving.",
                        [("➕ Add bank account", "ui:bankcodes:0")] + items + MENU)

    async def currency_picker(self, chat, page=0):
        codes = self.b.CURRENCIES
        page = max(0, min(page, (len(codes)-1)//8))
        items = [(self.b.CURRENCY_LABELS.get(c, c), 'ui:banknew:'+c) for c in codes[page*8:page*8+8]]
        if page: items.append(("← Previous", f"ui:bankcodes:{page-1}"))
        if (page+1)*8 < len(codes): items.append(("Next →", f"ui:bankcodes:{page+1}"))
        await self.send(chat, "Choose the bank account currency.", items + [("← Bank accounts", "ui:banks")])

    async def bank_detail(self, chat, code):
        account = (await self.banks()).get(code)
        if not account:
            return await self.bank_begin(chat, code)
        if account['image']:
            await self.b.admin_bot.call('sendPhoto', chat_id=chat, photo=('bank.png', account['image']))
        status = 'Visible at checkout' if account['ready'] else 'Not visible: enable it and set an exchange rate.'
        await self.send(chat, f"🏦 {code}\n{status}\n\n{self.bank_text(account)}\n\nRate: {account['rate'] or 'not set'} {code} per 1 USD", [
            ("✏️ Update account / screenshot / rate", 'ui:banknew:'+code),
            ("⏸ Disable" if account['enabled'] else "▶️ Enable", 'ui:banktoggle:'+code),
            ("← Bank accounts", 'ui:banks')])

    async def begin(self, chat, data, image=None):
        token = secrets.token_hex(5)
        await self.b.db.execute("INSERT INTO admin_sessions(admin_id,token,data,image) VALUES($1,$2,$3::jsonb,$4) ON CONFLICT(admin_id) DO UPDATE SET token=$2,data=$3::jsonb,image=$4", chat, token, json.dumps(data), image)
        return await self.prompt(chat, data, token)

    async def session(self, chat):
        row = await self.b.db.fetchrow('SELECT * FROM admin_sessions WHERE admin_id=$1', chat)
        return dict(row) if row else None

    async def bank_begin(self, chat, code):
        if code not in self.b.CURRENCIES:
            raise ValueError('Choose a currency from the buttons.')
        old = (await self.banks()).get(code, {})
        data = {k: old.get(k, '') for k in ('bank_name', 'account_name', 'account_number', 'instructions')}
        data.update(flow='bank', step='format', currency=code, rate=str(old.get('rate') or ''), had_image=bool(old.get('image')))
        await self.begin(chat, data, old.get('image'))

    async def prompt(self, chat, data, token):
        flow, step = data['flow'], data['step']
        key = lambda a: f'ui:form:{token}:{a}'
        if flow == 'bank':
            if step == 'format':
                return await self.send(chat, f"Update {data['currency']} bank details\nChoose how to add instructions. The next steps collect a copyable account number and exchange rate.", [
                    ("📝 Enter details as text", key('text')), ("🖼 Upload bank screenshot", key('image')),
                    ("Keep current details", key('keep'))] + CANCEL)
            prompts = {'text': 'Send the bank payment instructions as text (up to 1,800 characters).',
                       'image': 'Upload the bank details screenshot as a photo (up to 5 MB). You can include extra instructions in its caption.',
                       'bank_name': 'Send the bank / mobile-money provider name.',
                       'account_name': 'Send the account holder name.',
                       'account_number': 'Send the account number / IBAN / mobile-money number exactly as customers should copy it. Screenshots are not read automatically.',
                       'rate': f"Enter how many {data['currency']} equal 1 USD. This is your checkout conversion rate; enter numbers only."}
            if step == 'review':
                row = await self.session(chat)
                if row['image']:
                    await self.b.admin_bot.call('sendPhoto', chat_id=chat, photo=('bank.png', row['image']))
                return await self.send(chat, f"Review {data['currency']} bank account\n\n{self.bank_text(data)}\n\n1 USD = {data['rate']} {data['currency']}\nSaving enables this bank at checkout.", [("✅ Save & enable bank", key('save')), ("✏️ Start over", 'ui:banknew:'+data['currency'])] + CANCEL)
            return await self.send(chat, prompts[step], ([("Keep current value", key('keepfield'))] if data.get(step) else []) + CANCEL)
        if flow == 'product_edit':
            prompts = {'name': 'Send the new product name (1–80 characters).', 'price': 'Send the new price as a number. Physical products use USD; digital products use whole Telegram Stars.',
                       'description': 'Send the new description (up to 2,500 characters).', 'image': 'Upload the new product photo (up to 5 MB).', 'delivery': 'Send the digital delivery file (up to 10 MB), or the delivery text / link.'}
            return await self.send(chat, prompts[step], CANCEL)
        prompts = {('reminder', 'time'): 'Send the daily reminder time as HH:MM in UTC, for example 09:00. This is 10:00 in Nigeria.',
                   ('reminder', 'text'): 'Send the daily reminder message (up to 3,000 characters).',
                   ('fulfill', 'text'): 'Send the shipping / tracking / delivery information for this order.',
                   ('crypto_hash', 'text'): 'Paste the transaction hash from your receiving wallet. It will be attached to this order for verification and review.',
                   ('decision', 'reason'): 'Send your review note. For approval, first check that the money reached your bank / wallet. For rejection, explain why the payment cannot be accepted.'}
        if flow == 'reminder' and step == 'review':
            return await self.send(chat, f"Daily reminder at {data['time']} UTC\n\n{data['text']}", [("✅ Enable reminder", key('save'))] + CANCEL)
        if flow == 'decision' and step == 'confirm':
            word = 'APPROVE' if data['action'] == 'approve' else 'DECLINE'
            return await self.send(chat, f"{word} payment for order #{data['oid']}?\n\n{data['reason']}\n\nManual approval records your decision; it does not mean the network verified the transaction. Declining does not refund money already sent.", [(f"Confirm {word.lower()}", key('save'))] + CANCEL)
        return await self.send(chat, prompts[(flow, step)], CANCEL)

    async def admin_message(self, message, request_key):
        chat = message['chat']['id']
        text = (message.get('text') or message.get('caption') or '').strip()
        if text.split('@')[0].lower() in ('/start', '/admin', '/help', '/cancel'):
            await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1', chat)
            return await self.menu(chat)
        row = await self.session(chat)
        if not row:
            if await self.b.product_wizard(chat, message=message):
                return
            return await self.menu(chat)
        data = unpack(row['data'])
        if message.get('message_id') == data.get('last_message'):
            return await self.prompt(chat, data, row['token'])
        image = row['image']
        flow, step = data['flow'], data['step']
        if flow == 'bank':
            if step == 'text':
                if not 1 <= len(text) <= 1800: raise ValueError('Send bank instructions of 1–1,800 characters.')
                data['instructions'] = text
                image = None
                data['step'] = 'bank_name'
            elif step == 'image':
                if not message.get('photo'): raise ValueError('Upload the bank screenshot as a photo.')
                image = await self.b.admin_bot.download(message['photo'][-1]['file_id'], 5*1024*1024)
                data['instructions'] = message.get('caption', '').strip()[:1800]
                data['step'] = 'bank_name'
            elif step in ('bank_name', 'account_name', 'account_number'):
                if not 1 <= len(text) <= 200: raise ValueError('Send 1–200 characters.')
                data[step] = text
                data['step'] = {'bank_name': 'account_name', 'account_name': 'account_number', 'account_number': 'rate'}[step]
            elif step == 'rate':
                data['rate'] = str(self.b.positive(text))
                data['step'] = 'review'
            else:
                return await self.prompt(chat, data, row['token'])
        elif flow == 'product_edit':
            pid = data['pid']
            product = await self.b.db.fetchrow('SELECT * FROM products WHERE id=$1', pid)
            if not product: raise ValueError('Product no longer exists.')
            if step in ('name', 'description'):
                if not 1 <= len(text) <= (80 if step == 'name' else 2500): raise ValueError('Text is empty or too long.')
                await self.b.db.execute(f'UPDATE products SET {step}=$2 WHERE id=$1', pid, text)
            elif step == 'price':
                price = self.b.positive(text)
                if product['kind'] == 'digital' and (price != int(price) or price > 1000000): raise ValueError('Use whole Stars from 1 to 1,000,000.')
                await self.b.db.execute('UPDATE products SET price=$2 WHERE id=$1', pid, price)
            elif step == 'image':
                if not message.get('photo'): raise ValueError('Upload a product photo.')
                content = await self.b.admin_bot.download(message['photo'][-1]['file_id'], 5*1024*1024)
                await self.b.db.execute('UPDATE products SET image=$2 WHERE id=$1', pid, content)
            elif step == 'delivery':
                if message.get('document'):
                    doc = message['document']
                    content = await self.b.admin_bot.download(doc['file_id'], 10*1024*1024)
                    await self.b.db.execute("UPDATE products SET file=$2,filename=$3,delivery='' WHERE id=$1", pid, content, doc.get('file_name', 'product.bin'))
                else:
                    if not 1 <= len(text) <= 2500: raise ValueError('Send the file or up to 2,500 characters of delivery instructions.')
                    await self.b.db.execute('UPDATE products SET delivery=$2,file=NULL,filename=NULL WHERE id=$1', pid, text)
            await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1', chat)
            return await self.product_detail(chat, pid)
        elif flow == 'reminder':
            if step == 'time':
                if len(text) != 5: raise ValueError('Enter the time as HH:MM in UTC.')
                datetime.strptime(text, '%H:%M')
                data.update(time=text, step='text')
            elif step == 'text':
                if not 1 <= len(text) <= 3000: raise ValueError('Send a reminder message of 1–3,000 characters.')
                data.update(text=text, step='review')
            else: return await self.prompt(chat, data, row['token'])
        elif flow == 'decision':
            if step != 'reason': return await self.prompt(chat, data, row['token'])
            if not 1 <= len(text) <= 800: raise ValueError('Send a review note of 1–800 characters.')
            data.update(reason=text, step='confirm')
        elif flow == 'crypto_hash':
            o = await self.b.db.fetchrow('SELECT * FROM orders WHERE id=$1', data['oid'])
            if not o or not o['asset'] or o['status'] not in OPEN: raise ValueError('This crypto order is not awaiting payment review.')
            value = self.b.tx_hash(o['asset'], text)
            claimed = await self.b.db.fetchval('SELECT order_id FROM payments WHERE asset=$1 AND txid=$2', o['asset'], value)
            if claimed and claimed != o['id']: raise ValueError('This hash was already used by another order.')
            async with self.b.db.acquire() as conn:
                async with conn.transaction():
                    changed = await conn.fetchval("UPDATE orders SET txid=$2,payment_seen=false WHERE id=$1 AND status IN ('pending','review','expired','needs_review') RETURNING id", o['id'], value)
                    if not changed: raise ValueError('The order status changed. Refresh it before continuing.')
                    await conn.execute("INSERT INTO payment_audit(order_id,admin_id,action,reason,txid) VALUES($1,$2,'hash_added','Transaction hash entered by admin',$3)", o['id'], chat, value)
                    await conn.execute('DELETE FROM admin_sessions WHERE admin_id=$1', chat)
            return await self.order_detail(chat, o['id'])
        elif flow == 'fulfill':
            if not 1 <= len(text) <= 2000: raise ValueError('Send delivery information of 1–2,000 characters.')
            async with self.b.db.acquire() as conn:
                async with conn.transaction():
                    changed = await conn.fetchrow("UPDATE orders SET status='fulfilled',note=$2 WHERE id=$1 AND status='paid' RETURNING *", data['oid'], text)
                    if not changed: raise ValueError('Only a paid order can be marked delivered.')
                    await self.b.queue(conn, f"fulfilled:{data['oid']}", 'main', changed['user_id'], f"Order #{data['oid']}: {text}", buttons=self.keys([('View order', f"receipt:{data['oid']}")]))
                    await conn.execute('DELETE FROM admin_sessions WHERE admin_id=$1', chat)
            return await self.order_detail(chat, data['oid'])
        data['last_message'] = message.get('message_id')
        await self.b.db.execute('UPDATE admin_sessions SET data=$2::jsonb,image=$3 WHERE admin_id=$1 AND token=$4', chat, json.dumps(data), image, row['token'])
        return await self.prompt(chat, data, row['token'])

    async def form_action(self, chat, token, action):
        row = await self.session(chat)
        if not row or row['token'] != token:
            return await self.send(chat, 'That form is closed. Open the current menu to continue.')
        data = unpack(row['data'])
        if data['flow'] == 'bank' and data['step'] == 'format' and action in ('text', 'image', 'keep'):
            if action == 'keep' and not (row['image'] or data['instructions'] or data['account_number']):
                raise ValueError('Add bank details before continuing.')
            data['step'] = 'bank_name' if action == 'keep' else action
            await self.b.db.execute('UPDATE admin_sessions SET data=$2::jsonb WHERE admin_id=$1 AND token=$3', chat, json.dumps(data), token)
            return await self.prompt(chat, data, token)
        if data['flow'] == 'bank' and action == 'keepfield' and data['step'] in ('bank_name', 'account_name', 'account_number', 'rate') and data.get(data['step']):
            data['step'] = {'bank_name':'account_name', 'account_name':'account_number', 'account_number':'rate', 'rate':'review'}[data['step']]
            await self.b.db.execute('UPDATE admin_sessions SET data=$2::jsonb WHERE admin_id=$1 AND token=$3', chat, json.dumps(data), token)
            return await self.prompt(chat, data, token)
        if action != 'save' or data['step'] not in ('review', 'confirm'):
            return await self.prompt(chat, data, token)
        async with self.b.db.acquire() as conn:
            async with conn.transaction():
                fresh = await conn.fetchrow('SELECT token FROM admin_sessions WHERE admin_id=$1 FOR UPDATE', chat)
                if not fresh or fresh['token'] != token:
                    return await self.send(chat, 'Already saved or cancelled.')
                if data['flow'] == 'bank':
                    code = data['currency']
                    await conn.execute('''INSERT INTO bank_accounts(currency,instructions,bank_name,account_name,account_number,image,enabled)
                        VALUES($1,$2,$3,$4,$5,$6,true) ON CONFLICT(currency) DO UPDATE SET instructions=$2,bank_name=$3,account_name=$4,account_number=$5,image=$6,enabled=true''',
                        code, data['instructions'], data['bank_name'], data['account_name'], data['account_number'], row['image'])
                    for key, value in [('method:'+code, self.bank_text(data)), ('rate:'+code, str(self.b.positive(data['rate'])))]:
                        await conn.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2', key, value)
                elif data['flow'] == 'reminder':
                    await conn.execute("INSERT INTO settings(key,value) VALUES('reminder',$1) ON CONFLICT(key) DO UPDATE SET value=$1", json.dumps({'time': data['time'], 'text': data['text']}))
                elif data['flow'] == 'decision':
                    await self.decide(conn, chat, data)
                await conn.execute('DELETE FROM admin_sessions WHERE admin_id=$1 AND token=$2', chat, token)
        if data['flow'] == 'bank':
            await self.send(chat, f"✅ {data['currency']} bank saved and enabled at checkout. Existing orders keep their original bank details; new checkouts use this update.", [('View bank account', 'ui:bank:'+data['currency'])] + MENU)
        elif data['flow'] == 'decision':
            await self.order_detail(chat, data['oid'])
        else:
            await self.reminder_menu(chat)

    async def products(self, chat, page=0):
        page = max(page, 0)
        rows = await self.b.db.fetch('SELECT id,name,published FROM products ORDER BY id DESC LIMIT 9 OFFSET $1', page*8)
        items = [(f"{'✅' if r['published'] else '📝'} #{r['id']} {r['name'][:45]}", f"ui:product:{r['id']}") for r in rows[:8]]
        if page: items.append(('← Previous', f'ui:products:{page-1}'))
        if len(rows)>8: items.append(('Next →', f'ui:products:{page+1}'))
        await self.send(chat, '📦 Products\nChoose a product to edit its name, price, description, image or delivery.', [('➕ Add product', 'admin:add')] + items + MENU)

    async def extra_product_buttons(self, product):
        return []

    async def product_detail(self, chat, pid):
        product = await self.b.db.fetchrow('SELECT * FROM products WHERE id=$1', pid)
        if not product: raise ValueError('Product not found.')
        if product['image']:
            await self.b.admin_bot.call('sendPhoto', chat_id=chat, photo=('product.jpg', product['image']))
        items = [(label, f'ui:edit:{pid}:{field}') for label, field in [('✏️ Name', 'name'), ('💰 Price', 'price'), ('📝 Description', 'description'), ('🖼 Image', 'image')]]
        if product['kind'] == 'digital': items.append(('📎 Digital delivery', f'ui:edit:{pid}:delivery'))
        items += await self.extra_product_buttons(product)
        items += [('⏸ Hide from store' if product['published'] else '✅ Publish product', f"ui:publish:{pid}:{'hide' if product['published'] else 'show'}"), ('← Products', 'ui:products:0')]
        currency = 'XTR' if product['kind'] == 'digital' else 'USD'
        await self.send(chat, f"#{pid} {product['name']}\n{'Published' if product['published'] else 'Draft'} · {self.b.format_amount(product['price'], currency)}\n\n{product['description']}", items)

    async def publish(self, chat, pid, visible=True):
        async with self.b.db.acquire() as conn:
            async with conn.transaction():
                p = await conn.fetchrow('SELECT * FROM products WHERE id=$1 FOR UPDATE', pid)
                if not p: raise ValueError('Product not found.')
                if visible and p['kind'] == 'digital' and not (p['delivery'] or p['file']):
                    raise ValueError('Add digital delivery before publishing.')
                await conn.execute('UPDATE products SET published=$2 WHERE id=$1', pid, visible)
                if visible and not p['published']:
                    await conn.execute("""INSERT INTO jobs(dedup,target,chat,text,marketing,buttons)
                        SELECT 'product:'||$1::text||':'||id::text,'main',id,$2,true,$3::jsonb FROM users
                        WHERE alerts AND NOT blocked ON CONFLICT(dedup) DO NOTHING""", str(pid), f"New product: {p['name']}", json.dumps(self.keys([('View product', f'viewproduct:{pid}'), ('Disable alerts', 'alerts:off')])))
        return await self.product_detail(chat, pid)

    async def orders(self, chat, page=0, customer=False):
        page = max(page, 0)
        if customer:
            rows = await self.b.db.fetch('SELECT id,name,status FROM orders WHERE user_id=$1 ORDER BY id DESC LIMIT 9 OFFSET $2', chat, page*8)
        else:
            rows = await self.b.db.fetch('SELECT id,name,status FROM orders ORDER BY id DESC LIMIT 9 OFFSET $1', page*8)
        prefix = 'receipt' if customer else 'ui:order'
        pagination = 'customerorders' if customer else 'ui:orders'
        items = [(f"#{r['id']} {r['name'][:35]} · {r['status']}", f"{prefix}:{r['id']}") for r in rows[:8]]
        if page: items.append(('← Previous', f'{pagination}:{page-1}'))
        if len(rows)>8: items.append(('Next →', f'{pagination}:{page+1}'))
        if customer:
            await self.b.main_bot.send(chat, 'Your orders' if rows else 'No orders yet.', self.keys(items+[('← Store', 'home')]))
        else:
            await self.send(chat, '🧾 Orders & payment review' if rows else 'No orders yet.', items+MENU)

    async def extra_order_buttons(self, order):
        return []

    async def order_detail(self, chat, oid):
        o = await self.b.db.fetchrow('SELECT * FROM orders WHERE id=$1', oid)
        if not o: raise ValueError('Order not found.')
        o = dict(o)
        if o['proof']:
            await self.b.admin_bot.call('sendDocument', chat_id=chat, document=(o['proof_name'] or 'receipt.jpg', o['proof']))
        audit = await self.b.db.fetch('SELECT admin_id,action,reason FROM payment_audit WHERE order_id=$1 ORDER BY id DESC LIMIT 3', oid)
        audit_text = '\n'.join(f"{a['action']} by {a['admin_id'] or 'network'}: {a['reason']}" for a in audit)
        text = f"Order #{oid} · {o['name']}\nStatus: {o['status']}\nCustomer: {o['user_id']}\nAmount: {self.b.format_amount(o['amount'], o['currency'])}\nMethod: {o['method']}\nConfirmation: {o.get('payment_source') or 'not confirmed'}\nTX: {o['txid'] or 'not submitted'}\nAddress: {o['address'] or 'bank transfer / Stars'}\nShipping: {o['shipping']}\n\n{audit_text}"
        items = []
        if o['method'] != 'stars' and o['status'] in OPEN:
            items += [('✅ Mark payment received', f'ui:approve:{oid}'), ('❌ Decline payment', f'ui:reject:{oid}')]
        if o['asset'] and o['status'] in OPEN:
            items += [('🔗 Add / change transaction hash', f'ui:adminhash:{oid}'), ('🔄 Check blockchain', f'ui:rescan:{oid}')]
        if o['status'] == 'paid': items.append(('🚚 Mark delivered / tracking', f'ui:fulfill:{oid}'))
        if o['method'] == 'stars' and o['status'] in ('paid', 'fulfilled', 'refunding'):
            items.append(('↩️ Refund Stars', f'ui:refundask:{oid}'))
        items += await self.extra_order_buttons(o)
        await self.send(chat, text, items+[('← Orders', 'ui:orders:0')])

    async def decide(self, conn, admin_id, data):
        oid = data['oid']
        o = await conn.fetchrow('SELECT * FROM orders WHERE id=$1 FOR UPDATE', oid)
        if not o or o['status'] not in OPEN or o['method'] == 'stars':
            raise ValueError('This payment has already been decided or uses Telegram Stars.')
        if data['action'] == 'approve':
            if o['asset']:
                if not o['txid']: raise ValueError('Use the Add / change transaction hash button before approving crypto. The transaction hash prevents crediting the same transfer twice.')
                await self.claim(conn, o)
            await conn.execute("UPDATE orders SET payment_source='admin' WHERE id=$1", oid)
            await self.b.mark_paid(conn, o, source='admin')
        else:
            await conn.execute("UPDATE orders SET status='rejected' WHERE id=$1", oid)
            await self.b.queue(conn, f'rejected:{oid}', 'main', o['user_id'], f"Payment for order #{oid} was declined.\n{data['reason']}\nContact support if you already paid.", buttons=self.keys([('View order', f'receipt:{oid}'), ('Support', 'support')]))
        await conn.execute('INSERT INTO payment_audit(order_id,admin_id,action,reason,txid) VALUES($1,$2,$3,$4,$5)', oid, admin_id, data['action'], data['reason'], o['txid'])

    async def claim(self, conn, o):
        claimed = await conn.fetchval('INSERT INTO payments(asset,txid,order_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING order_id', o['asset'], o['txid'], o['id'])
        if not claimed:
            existing = await conn.fetchval('SELECT order_id FROM payments WHERE asset=$1 AND txid=$2', o['asset'], o['txid'])
            if existing != o['id']:
                raise ValueError('This transaction was already used for another order. Payment was not approved.')

    async def reminder_menu(self, chat):
        reminder = unpack(await self.b.setting('reminder', '{}'))
        await self.send(chat, f"🔔 Daily reminder\n{'Enabled at '+reminder['time']+' UTC' if reminder.get('time') else 'Disabled'}\n\n{reminder.get('text', '')}\nOnly subscribed users receive reminders.", [('✏️ Set reminder', 'ui:reminderset'), ('⏸ Disable reminder', 'ui:reminderoff')]+MENU)

    async def wallets(self, chat):
        import os
        lines = []
        for asset in self.b.ASSETS:
            enabled = asset in self.b.enabled_crypto()
            lines.append(f"{'✅' if enabled else '⚠️'} {asset} · {self.b.ASSETS[asset][0]}\n{os.getenv(asset+'_ADDRESS', '').strip() or 'Address not configured'}")
        await self.send(chat, '🪙 Crypto wallets\n\n'+'\n\n'.join(lines)+'\n\nWallet addresses remain in hosting variables. ETH and SOL also need mainnet RPC URLs. Customers can upload receipts or submit hashes using buttons.')

    async def jobs(self, chat):
        rows = await self.b.db.fetch("SELECT id,status,target FROM jobs WHERE status IN ('uncertain','failed') ORDER BY id DESC LIMIT 15")
        await self.send(chat, 'Notification queue\nAn uncertain notification may already have arrived. Retrying can send a duplicate.', [(f"Retry #{r['id']} · {r['status']} · {r['target']}", f"ui:retryask:{r['id']}") for r in rows]+MENU)

    async def admin_callback(self, chat, data, request_key):
        if data in ('admin:menu', 'admin:settings', 'admin:help', 'admin:products', 'admin:orders', 'admin:stats'):
            data = {'admin:menu':'ui:menu', 'admin:settings':'ui:banks', 'admin:help':'ui:help', 'admin:products':'ui:products:0', 'admin:orders':'ui:orders:0', 'admin:stats':'ui:stats'}[data]
        if data == 'admin:myid': return await self.send(chat, f'Your Telegram user ID: {chat}')
        if data == 'admin:add':
            await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1', chat)
            return await self.b.product_wizard(chat, action='start')
        if data.startswith('form:'): return await self.b.product_wizard(chat, action=data[5:])
        if data.startswith('catalog:'): return await self.products(chat, int(data.split(':')[1])//5)
        if not data.startswith('ui:'): return await self.menu(chat)
        parts = data.split(':'); action = parts[1]
        if action in ('menu', 'cancel'):
            await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1', chat)
            if action == 'cancel': await self.b.db.execute('DELETE FROM admin_product_drafts WHERE admin_id=$1', chat)
            return await self.menu(chat)
        if action == 'help':
            return await self.send(chat, 'Use the menu buttons to add or edit products, set up banks and rates, review receipts, approve or decline payments, manage reminders, and check notifications.\n\nAfter choosing a field, send its value or upload its image. No command formats are needed.')
        if action == 'banks': return await self.bank_menu(chat)
        if action == 'bankcodes': return await self.currency_picker(chat, int(parts[2]))
        if action == 'bank': return await self.bank_detail(chat, parts[2])
        if action == 'banknew': return await self.bank_begin(chat, parts[2])
        if action == 'banktoggle':
            code = parts[2]
            a = (await self.banks()).get(code)
            if not a: raise ValueError('Add the bank first.')
            await self.b.db.execute('INSERT INTO bank_accounts(currency,instructions,enabled) VALUES($1,$2,$3) ON CONFLICT(currency) DO UPDATE SET enabled=$3', code, a['instructions'], not a['enabled'])
            return await self.bank_detail(chat, code)
        if action == 'form': return await self.form_action(chat, parts[2], parts[3])
        if action == 'products': return await self.products(chat, int(parts[2]))
        if action == 'product': return await self.product_detail(chat, int(parts[2]))
        if action == 'edit':
            if parts[3] not in ('name', 'price', 'description', 'image', 'delivery'): raise ValueError('Choose a product field.')
            return await self.begin(chat, dict(flow='product_edit', step=parts[3], pid=int(parts[2])))
        if action == 'publish': return await self.publish(chat, int(parts[2]), len(parts)<4 or parts[3] == 'show')
        if action == 'orders': return await self.orders(chat, int(parts[2]))
        if action == 'order': return await self.order_detail(chat, int(parts[2]))
        if action in ('approve', 'reject'):
            return await self.begin(chat, dict(flow='decision', step='reason', action=action, oid=int(parts[2])))
        if action == 'adminhash': return await self.begin(chat, dict(flow='crypto_hash', step='text', oid=int(parts[2])))
        if action == 'fulfill': return await self.begin(chat, dict(flow='fulfill', step='text', oid=int(parts[2])))
        if action == 'rescan':
            o = await self.b.db.fetchrow('SELECT * FROM orders WHERE id=$1', int(parts[2]))
            if not o or not o['asset'] or o['status'] not in OPEN: raise ValueError('This order cannot be rescanned.')
            await self.check_order(dict(o))
            return await self.order_detail(chat, o['id'])
        if action == 'refundask':
            return await self.send(chat, f"Refund Stars for order #{int(parts[2])}?", [('Confirm Stars refund', 'ui:refund:'+parts[2])]+MENU)
        if action == 'refund':
            # Existing Stars refund code uses Telegram's idempotent charge identity.
            await self.b.legacy_admin_message({'chat':{'id':chat}, 'text':'/refund '+parts[2]}, request_key)
            return await self.order_detail(chat, int(parts[2]))
        if action == 'reminder': return await self.reminder_menu(chat)
        if action == 'reminderset': return await self.begin(chat, dict(flow='reminder', step='time'))
        if action == 'reminderoff':
            await self.b.set_setting('reminder', '{}'); return await self.reminder_menu(chat)
        if action == 'wallets': return await self.wallets(chat)
        if action == 'stats':
            users = await self.b.db.fetchval('SELECT count(*) FROM users')
            rows = await self.b.db.fetch('SELECT status,count(*) AS n FROM orders GROUP BY status')
            return await self.send(chat, f"Users: {users}\n"+'\n'.join(f"{r['status']}: {r['n']}" for r in rows))
        if action == 'jobs': return await self.jobs(chat)
        if action == 'retryask':
            return await self.send(chat, f"Retry notification #{int(parts[2])}? It may already have been delivered.", [('Confirm retry', 'ui:retry:'+parts[2])]+MENU)
        if action == 'retry':
            await self.b.db.execute("UPDATE jobs SET status='queued',due=now() WHERE id=$1 AND status IN ('uncertain','failed')", int(parts[2]))
            return await self.jobs(chat)
        return await self.menu(chat)

    async def snapshot_bank(self, conn, order, account=None):
        account = account or (await self.banks()).get(order['currency'])
        if not account or not account['ready']: raise ValueError('This bank is not ready. Choose another payment method.')
        data = {k: account.get(k, '') for k in ('bank_name', 'account_name', 'account_number', 'instructions')}
        await conn.execute('INSERT INTO order_bank_details(order_id,data,image) VALUES($1,$2::jsonb,$3) ON CONFLICT DO NOTHING', order['id'], json.dumps(data), account['image'])

    async def owned_order(self, chat, oid):
        row = await self.b.db.fetchrow('SELECT * FROM orders WHERE id=$1 AND user_id=$2', oid, chat)
        if not row: raise ValueError('Order not found.')
        return dict(row)

    def order_text(self, o):
        title = f"Order #{o['id']} · {o['name']}\n"
        status = o['status']
        if status == 'paid' or status == 'fulfilled':
            confirmed = 'Manually approved by the store' if o.get('payment_source') == 'admin' else 'Payment confirmed'
            return title + f"✅ {confirmed}\n{self.b.format_amount(o['amount'], o['currency'])}" + (f"\nDelivery: {o['note']}" if status == 'fulfilled' else '')
        if status == 'rejected': return title + '❌ Payment declined. Contact support if you already sent funds.'
        if status in ('refunded', 'refunding'): return title + f"Status: {status}"
        if o['asset']:
            expired = o['expires'] <= self.b.now()
            if expired:
                return title + '⏱ 00:00 · Quote expired\nDo not send a new payment to this quote. If you already paid, submit the hash or receipt below. On-time transactions can still finish confirming.'
            confirmation = 'Transfer detected · waiting for network confirmation' if o.get('payment_seen') else 'Waiting for payment'
            if status == 'review': confirmation += '\nReceipt sent to admin for review'
            return title + f"{confirmation}\n\nSend exactly {self.b.format_amount(o['amount'], o['asset'])}\nNetwork: {self.b.ASSETS[o['asset']][0]}\nAddress:\n{o['address']}\n\n⏳ Time left: {countdown(o['expires'], self.b.now())}\nPay network fees separately. The address QR does not include the amount; use Copy exact amount.\nWe check the blockchain automatically and notify you after confirmation."
        if o['method'] == 'local':
            return title + f"{'Receipt under review' if status == 'review' else 'Awaiting bank transfer'}\nAmount: {self.b.format_amount(o['amount'], o['currency'])}\nReference: SNOW-{o['id']}\n\n{o['note']}\n\nAfter paying, upload your payment receipt or screenshot here (or tap Upload receipt / screenshot). The store checks its bank before approving."
        return title + f"Status: {status}\nAmount: {self.b.format_amount(o['amount'], o['currency'])}"

    def order_buttons(self, o, bank=None):
        oid = o['id']
        rows = []
        if o['status'] in OPEN:
            if o['asset'] and o['expires'] > self.b.now():
                rows += [[copy_button('📋 Copy address', o['address'])], [copy_button('📋 Copy exact amount', format(o['amount'], 'f'))], [{'text':'📱 Show address QR', 'callback_data':f'payqr:{oid}'}]]
            if o['method'] == 'local':
                for label, field in [('📋 Copy account number', 'account_number'), ('📋 Copy account name', 'account_name'), ('📋 Copy bank name', 'bank_name')]:
                    if bank and bank.get(field): rows.append([copy_button(label, bank[field])])
                rows += [[copy_button('📋 Copy amount', format(o['amount'], 'f'))], [copy_button('📋 Copy reference', f'SNOW-{oid}')]]
            if o['asset']: rows.append([{'text':'🔗 Submit transaction hash', 'callback_data':f'payhash:{oid}'}])
            if o['method'] != 'stars': rows.append([{'text':'📸 Upload receipt / screenshot', 'callback_data':f'payproof:{oid}'}])
        rows += [[{'text':'🔄 Refresh payment status', 'callback_data':f'receipt:{oid}'}], [{'text':'🧾 My orders', 'callback_data':'customerorders:0'}], [{'text':'💬 Support', 'callback_data':'support'}], [{'text':'← Store', 'callback_data':'home'}]]
        return {'inline_keyboard': rows}

    async def show_order(self, chat, order):
        o = dict(order)
        bank = None
        if o['method'] == 'local' and o.get('bank_released', True):
            snapshot = await self.b.db.fetchrow('SELECT * FROM order_bank_details WHERE order_id=$1', o['id'])
            if snapshot:
                bank = unpack(snapshot['data'])
                if snapshot['image']:
                    await self.b.main_bot.call('sendPhoto', chat_id=chat, photo=('bank.png', snapshot['image']), caption=f"Bank details for order #{o['id']}")
        if o['asset'] and o['status'] in OPEN and o['expires'] > self.b.now():
            await self.show_qr(chat, o)
        sent = await self.b.main_bot.send(chat, self.order_text(o), self.order_buttons(o, bank))
        if o['asset'] and o['status'] in OPEN and sent and sent.get('message_id'):
            await self.b.db.execute('''INSERT INTO invoice_messages(order_id,chat,message_id,finished) VALUES($1,$2,$3,false)
                ON CONFLICT(order_id) DO UPDATE SET chat=$2,message_id=$3,finished=false,due=now()''', o['id'], chat, sent['message_id'])
        if o['status'] in ('paid', 'fulfilled') and o['kind'] == 'digital':
            if o['delivery']: await self.b.main_bot.send(chat, o['delivery'])
            if o['file']: await self.b.main_bot.call('sendDocument', chat_id=chat, document=(o['filename'] or 'product.bin', o['file']))

    async def show_qr(self, chat, o):
        if not o['asset'] or o['status'] not in OPEN or o['expires'] <= self.b.now():
            raise ValueError('This quote is no longer available for a new payment.')
        await self.b.main_bot.call('sendPhoto', chat_id=chat, photo=('payment-qr.png', qr_png(o['address'])),
            caption=f"{o['asset']} · {self.b.ASSETS[o['asset']][0]}\nScan to fill the address. Enter the exact amount shown below.\nOrder #{o['id']}")

    async def save_hash(self, chat, o, text):
        if not o['asset'] or o['status'] not in OPEN: raise ValueError('This order is not accepting transaction hashes.')
        value = self.b.tx_hash(o['asset'], text)
        existing = await self.b.db.fetchval('SELECT order_id FROM payments WHERE asset=$1 AND txid=$2', o['asset'], value)
        if existing and existing != o['id']: raise ValueError('That transaction has already been used for a different order.')
        changed = await self.b.db.fetchval("UPDATE orders SET txid=$2,payment_seen=false WHERE id=$1 AND user_id=$3 AND status IN ('pending','review','expired','needs_review') RETURNING id", o['id'], value, chat)
        if not changed: raise ValueError('The order status changed. Refresh it to continue.')
        await self.b.state(chat, {})
        await self.b.main_bot.send(chat, f"Transaction submitted for order #{o['id']}. We will verify the destination, exact amount, network, payment time and confirmations. You will receive an update after verification.", self.keys([('View payment', f"receipt:{o['id']}"), ('Upload receipt / screenshot', f"payproof:{o['id']}")]))

    async def request_receipt(self, chat, order):
        o = await self.owned_order(chat, order['id'])
        if o['method'] == 'stars' or o['status'] not in OPEN or o.get('proof'):
            return
        pending = unpack(await self.b.db.fetchval('SELECT state FROM users WHERE id=$1', chat))
        if not pending:
            await self.b.state(chat, dict(payment_input='proof', oid=o['id'], receipt_prompt=True))
        if o['asset'] and o['expires'] <= self.b.now():
            text = 'If you already paid before the deadline, upload your payment receipt or screenshot here. Do not send a new payment to this expired quote.'
        else:
            text = 'After making your payment, upload your payment receipt or screenshot here.'
            if o['method'] == 'local' and not o.get('bank_released', True):
                text = 'Wait for support to send the bank details before paying.\n\n' + text
        text = f"📸 Payment receipt for order #{o['id']}\n{text}\nSend a photo or attach the receipt file (up to 10 MB). No caption or command is needed."
        text += '\n\nA receipt is sent to the admin for review. Payment still needs to be verified.'
        await self.b.main_bot.send(chat, text, self.keys([
            ('📸 Upload receipt / screenshot', f"payproof:{o['id']}"),
            ('View payment details', f"receipt:{o['id']}"),
            ('Message support about this order', f"orderchat:{o['id']}"),
        ]))

    async def save_proof(self, chat, o, message):
        if o['method'] == 'stars' or o['status'] not in OPEN: raise ValueError('This order cannot accept a receipt now.')
        media = message['photo'][-1] if message.get('photo') else message.get('document')
        if not media: raise ValueError('Upload the payment screenshot as a photo, or attach the receipt file (up to 10 MB).')
        content = await self.b.main_bot.download(media['file_id'], 10*1024*1024)
        async with self.b.db.acquire() as conn:
            async with conn.transaction():
                changed = await conn.fetchval("UPDATE orders SET proof=$2,proof_name=$3,status='review' WHERE id=$1 AND user_id=$4 AND status IN ('pending','review','expired','needs_review') RETURNING id", o['id'], content, media.get('file_name', 'receipt.jpg'), chat)
                if not changed: raise ValueError('The order was already reviewed. Refresh its status.')
                await self.b.alert_admins(conn, f"proof:{o['id']}:{message.get('message_id', 0)}", f"New payment receipt for order #{o['id']} · {o['method']}\nCheck your bank / wallet before approving.", buttons=self.keys([('Review receipt', f"ui:order:{o['id']}")]))
        await self.b.state(chat, {})
        await self.b.main_bot.send(chat, '✅ Receipt uploaded. We will notify you after payment review. Uploading a receipt does not confirm the payment.', self.keys([('View payment', f"receipt:{o['id']}"), ('Support', 'support')]))

    async def customer_input(self, message, pending):
        chat = message['chat']['id']
        if pending.get('edit_shipping'):
            value = message.get('text', '').strip()
            if not 1 <= len(value) <= 1500: raise ValueError('Send your delivery details in 1–1,500 characters.')
            await self.b.db.execute('UPDATE users SET shipping=$2 WHERE id=$1', chat, value)
            await self.b.state(chat, {})
            await self.b.main_bot.send(chat, 'Delivery details updated for future orders.', self.b.home_buttons())
            return True
        if pending.get('payment_input'):
            o = await self.owned_order(chat, pending['oid'])
            if pending.get('receipt_prompt') and o['status'] not in OPEN:
                await self.b.state(chat, {})
                await self.show_order(chat, o)
                return True
            if pending['payment_input'] == 'proof' and o['asset'] and message.get('text'):
                try: self.b.tx_hash(o['asset'], message['text'])
                except ValueError: pass
                else:
                    await self.save_hash(chat, o, message['text'])
                    return True
            if pending['payment_input'] == 'hash': await self.save_hash(chat, o, message.get('text', ''))
            else: await self.save_proof(chat, o, message)
            return True
        # Accept an attachment sent directly after viewing payment details.
        # Never guess its order when several payments are still open.
        if (not pending or pending.get('receipt_pick')) and (message.get('photo') or message.get('document')):
            orders = await self.b.db.fetch("SELECT * FROM orders WHERE user_id=$1 AND method<>'stars' AND status IN ('pending','review','expired','needs_review') ORDER BY id DESC LIMIT 20", chat)
            if len(orders) == 1:
                await self.save_proof(chat, dict(orders[0]), message)
            elif orders:
                saved = {'message_id': message.get('message_id', 0)}
                if message.get('photo'):
                    saved['photo'] = [{'file_id': message['photo'][-1]['file_id']}]
                else:
                    saved['document'] = {k: message['document'][k] for k in ('file_id', 'file_name') if k in message['document']}
                await self.b.state(chat, {'receipt_pick': saved})
                await self.b.main_bot.send(chat, 'Which order is this receipt for? Choose below to attach the screenshot or file you just sent.', self.keys([
                    (f"Order #{o['id']} · {o['name'][:35]}", f"proofpick:{o['id']}") for o in orders
                ] + [('✖ Cancel', 'home')]))
            else:
                await self.b.main_bot.send(chat, 'There is no order waiting for a payment receipt. Open My orders to check your payment status.', self.keys([('My orders', 'myorders'), ('Support', 'support')]))
            return True
        # Accept a bare hash as in the user's screenshot; never guess between orders.
        text = message.get('text', '').strip()
        if text and not text.startswith('/'):
            candidates = []
            for o in await self.b.db.fetch("SELECT * FROM orders WHERE user_id=$1 AND asset IS NOT NULL AND status IN ('pending','review','expired','needs_review') ORDER BY id DESC LIMIT 30", chat):
                try: self.b.tx_hash(o['asset'], text)
                except ValueError: continue
                candidates.append(dict(o))
            if len(candidates) == 1:
                await self.save_hash(chat, candidates[0], text); return True
            if candidates:
                await self.b.main_bot.send(chat, 'Choose which order this transaction belongs to, then tap Submit transaction hash.', self.keys([(f"Order #{o['id']} · {o['asset']}", f"payhash:{o['id']}") for o in candidates]))
                return True
        return False

    async def customer_callback(self, chat, data):
        if data.startswith('proofpick:'):
            pending = unpack(await self.b.db.fetchval('SELECT state FROM users WHERE id=$1', chat))
            if not pending.get('receipt_pick'):
                raise ValueError('This receipt selection is closed. Upload the screenshot or file again.')
            o = await self.owned_order(chat, int(data.split(':')[1]))
            await self.save_proof(chat, o, pending['receipt_pick'])
            return True
        if data == 'support':
            await self.b.main_bot.send(chat, self.b.SUPPORT, self.keys([('My orders', 'customerorders:0'), ('← Store', 'home')]))
            return True
        if data == 'deliverydetails':
            await self.b.state(chat, {'edit_shipping': True})
            await self.b.main_bot.send(chat, 'Send your name, phone number, country and full delivery address in one message. This will apply to future orders.', self.keys([('✖ Cancel', 'home')]))
            return True
        if data == 'home':
            await self.b.state(chat, {})
            await self.b.main_bot.send(chat, '☃️ Welcome to SnowmanBot!\nChoose an option below.', self.b.home_buttons())
            return True
        if data == 'myorders' or data.startswith('customerorders:'):
            await self.orders(chat, int(data.split(':')[1]) if ':' in data else 0, customer=True); return True
        if data.startswith('viewproduct:'):
            p = await self.b.db.fetchrow('SELECT * FROM products WHERE id=$1 AND published', int(data.split(':')[1]))
            if not p: raise ValueError('This product is unavailable.')
            await self.b.product_card(chat, p); return True
        if data.startswith(('receipt:', 'payhash:', 'payproof:', 'payqr:')):
            action, raw = data.split(':')
            o = await self.owned_order(chat, int(raw))
            if action == 'receipt':
                await self.b.state(chat, {})
                await self.show_order(chat, o)
            elif action == 'payqr': await self.show_qr(chat, o)
            else:
                if o['status'] not in OPEN: raise ValueError('This order is not awaiting payment review.')
                if action == 'payhash' and not o['asset']: raise ValueError('Bank transfers do not use a crypto hash.')
                if o['method'] == 'stars': raise ValueError('This order uses Telegram Stars.')
                kind = 'hash' if action == 'payhash' else 'proof'
                await self.b.state(chat, dict(payment_input=kind, oid=o['id']))
                prompt = f"Paste the {o['asset']} transaction hash for order #{o['id']}." if kind == 'hash' else f"Upload the payment screenshot / receipt for order #{o['id']}. No caption needed."
                if kind == 'hash' and o['asset'] == 'TON': prompt += " Use the receiving wallet's transaction hash; automatic wallet scanning can also find the incoming transfer."
                await self.b.main_bot.send(chat, prompt, self.keys([('✖ Cancel', 'home')]))
            return True
        return False

    async def check_order(self, o):
        if not o['txid']: return False
        valid = await asyncio.wait_for(self.b.crypto.verify(o), timeout=40)
        if not valid: return False
        async with self.b.db.acquire() as conn:
            async with conn.transaction():
                fresh = await conn.fetchrow('SELECT * FROM orders WHERE id=$1 FOR UPDATE', o['id'])
                if not fresh or fresh['status'] not in OPEN or fresh['txid'] != o['txid']: return False
                await self.claim(conn, fresh)
                await conn.execute("UPDATE orders SET payment_source='network',payment_seen=true WHERE id=$1", o['id'])
                await self.b.mark_paid(conn, fresh, source='network')
                await conn.execute("INSERT INTO payment_audit(order_id,action,reason,txid) VALUES($1,'network_confirmed','Verified exact amount, recipient, network, time and confirmations',$2)", o['id'], o['txid'])
        return True

    async def scan_cycle(self):
        rows = await self.b.db.fetch("""SELECT * FROM orders WHERE asset IS NOT NULL
            AND status IN ('pending','review','expired','needs_review')
            AND (expires > now()-interval '24 hours' OR (txid IS NOT NULL AND status<>'needs_review')) ORDER BY id""")
        grouped = {}
        for row in rows:
            o = dict(row)
            grouped.setdefault((o['asset'], o['address']), []).append(o)
        for (asset, address), orders in grouped.items():
            try:
                # Candidate discovery is separate from authoritative per-chain verification.
                candidates = await asyncio.wait_for(self.b.crypto.incoming(asset, address, min(int(o['created'].timestamp()) for o in orders)), timeout=35)
                for o in orders:
                    matches = [c for c in candidates if int(c['units']) == int(o['units']) and (c.get('timestamp') is None or int(c['timestamp']) >= int(o['created'].timestamp()))]
                    if len(matches) != 1: continue
                    txid = self.b.tx_hash(asset, matches[0]['txid'])
                    if o['txid'] == txid and o.get('payment_seen'): continue
                    changed = await self.b.db.fetchval("UPDATE orders SET txid=$2,payment_seen=true WHERE id=$1 AND status IN ('pending','review','expired','needs_review') RETURNING id", o['id'], txid)
                    if changed:
                        o['txid'] = txid
                        await self.b.queue(self.b.db, f"detected:{o['id']}", 'main', o['user_id'], f"Transfer detected for order #{o['id']}. Waiting for the required network confirmations and payment checks. Do not send again.", buttons=self.keys([('View status', f"receipt:{o['id']}")]))
            except asyncio.CancelledError: raise
            except Exception as error:
                self.b.LOG.warning('Discovery %s: %s', asset, type(error).__name__)
            for o in orders:
                try:
                    if await self.check_order(o): continue
                    if o['txid'] and (self.b.now()-o['expires']).total_seconds() > 86400 and o['status'] != 'needs_review':
                        async with self.b.db.acquire() as conn:
                            async with conn.transaction():
                                changed = await conn.fetchval("UPDATE orders SET status='needs_review' WHERE id=$1 AND status IN ('pending','review','expired') RETURNING id", o['id'])
                                if changed:
                                    await self.b.alert_admins(conn, f"unresolved:{o['id']}", f"Crypto order #{o['id']} could not be verified. Review the payment before asking the customer to pay again.", buttons=self.keys([('Review payment', f"ui:order:{o['id']}")]))
                except asyncio.CancelledError: raise
                except Exception as error: self.b.LOG.warning('Payment %s: %s', o['id'], type(error).__name__)
                await asyncio.sleep(0.4)

    async def timers(self):
        while True:
            try:
                rows = await self.b.db.fetch('SELECT * FROM invoice_messages WHERE NOT finished AND due<=now() ORDER BY due LIMIT 20')
                for row in rows:
                    o = await self.owned_order(row['chat'], row['order_id'])
                    expired = o['expires'] <= self.b.now()
                    if expired and o['status'] == 'pending':
                        await self.b.db.execute("UPDATE orders SET status='expired' WHERE id=$1 AND status='pending'", o['id'])
                        o['status'] = 'expired'
                    finished = expired or o['status'] not in OPEN
                    try:
                        await self.b.main_bot.call('editMessageText', chat_id=row['chat'], message_id=row['message_id'], text=self.order_text(o), reply_markup=self.order_buttons(o))
                    except self.b.TGError as error:
                        if error.code == 429:
                            await self.b.db.execute("UPDATE invoice_messages SET due=now()+$2*interval '1 second' WHERE order_id=$1", o['id'], max(15, error.retry))
                            continue
                        if error.code == 403 or (error.code == 400 and 'not modified' not in error.description.lower()): finished = True
                        elif error.code != 400: raise
                    await self.b.db.execute("UPDATE invoice_messages SET due=now()+interval '15 seconds',finished=$2 WHERE order_id=$1", o['id'], finished)
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError: raise
            except Exception as error: self.b.LOG.warning('Invoice timer: %s', type(error).__name__)
            await asyncio.sleep(3)
