"""Location-drop delivery, manual bank coordination, country profiles and backups."""
import asyncio
import base64
import io
import json
import math
import os
import re
import secrets
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from storefront import StoreUI, OPEN, MENU, CANCEL, unpack

FULFILLMENT_SCHEMA = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS city TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS city TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS store_media (key TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('photo','sticker')),content BYTEA NOT NULL,filename TEXT NOT NULL);
ALTER TABLE users ADD COLUMN IF NOT EXISTS country TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS dropoff JSONB NOT NULL DEFAULT '{}';
ALTER TABLE users ADD COLUMN IF NOT EXISTS delivery_schedule JSONB NOT NULL DEFAULT '{}';
ALTER TABLE products ADD COLUMN IF NOT EXISTS country TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS country TEXT NOT NULL DEFAULT '';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS dropoff JSONB NOT NULL DEFAULT '{}';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_schedule JSONB NOT NULL DEFAULT '{}';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS subtotal_usd NUMERIC(24,6);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS discount_usd NUMERIC(24,6) NOT NULL DEFAULT 0;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS bank_released BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS eta TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS delivery_live (
    order_id BIGINT PRIMARY KEY REFERENCES orders(id),
    admin_id BIGINT NOT NULL,
    source_message_id BIGINT NOT NULL,
    customer_message_id BIGINT NOT NULL,
    last_lat DOUBLE PRECISION NOT NULL,
    last_lon DOUBLE PRECISION NOT NULL,
    updated TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires TIMESTAMPTZ NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(admin_id, source_message_id)
);
CREATE TABLE IF NOT EXISTS order_messages (
    id BIGSERIAL PRIMARY KEY,
    request_key TEXT NOT NULL UNIQUE,
    order_id BIGINT REFERENCES orders(id),
    customer_id BIGINT NOT NULL REFERENCES users(id),
    admin_id BIGINT,
    direction TEXT NOT NULL,
    body TEXT NOT NULL,
    created TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS vendors (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'upcoming' CHECK(status IN ('upcoming','verified','hidden'))
);
"""

# Country profiles share one bot. Country names are display labels, not delivery guarantees.
COUNTRIES = {'OM':'Oman','SA':'Saudi Arabia','AE':'United Arab Emirates','BH':'Bahrain','KW':'Kuwait','QA':'Qatar'}
BANK_CODES = ['USD','OMR','SAR','AED','BHD','KWD','QAR']

BACKUP_TABLES = ['users','settings','products','admin_product_drafts','bank_accounts','admin_sessions','vendors','store_media',
                 'orders','payments','jobs','order_bank_details','invoice_messages','payment_audit','delivery_live','order_messages']


def quote_usd(price):
    price = Decimal(price)
    discount = Decimal('150') if price > Decimal('1000') else Decimal('0')
    return price, discount, price-discount


def delivery_words(text):
    return re.sub(r'\beta\b', 'estimated delivery time', str(text), flags=re.IGNORECASE)


def location_pin(value):
    try: lat, lon = float(value['latitude']), float(value['longitude'])
    except (KeyError,TypeError,ValueError): raise ValueError('Send a map location pin for the drop-off point.')
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError('Send a valid map location.')
    return {'latitude':lat, 'longitude':lon}


def flag(code):
    return ''.join(chr(127397+ord(c)) for c in code) if len(code)==2 else '🌍'


def backup_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {'$bytes':base64.b64encode(value).decode()}
    if isinstance(value, Decimal): return {'$decimal':str(value)}
    if isinstance(value, datetime): return {'$datetime':value.isoformat()}
    return value


class FulfillmentUI(StoreUI):
    def __init__(self, app):
        super().__init__(app)
        self.tz_name = os.getenv('DELIVERY_TIMEZONE', 'Asia/Dubai')
        self.tz = ZoneInfo(self.tz_name)
        self.backup_time = os.getenv('AUTO_BACKUP_UTC', '02:00').strip()
        if self.backup_time.lower() not in ('off','disabled',''):
            if len(self.backup_time)!=5: raise ValueError('AUTO_BACKUP_UTC must be HH:MM in UTC or off')
            datetime.strptime(self.backup_time, '%H:%M')
        self.last_backup_minute = None

    async def menu(self, chat):
        return await self.send(chat,f'SnowmanBot Admin\nChoose what to manage.\nVersion: {self.b.VERSION}',[
            ('🎛 Store content & appearance','store:settings'),('➕ Add product','admin:add'),('📦 Manage products','ui:products:0'),
            ('🧾 Orders, payments & delivery','ui:orders:0'),('🏦 Saved bank templates','ui:banks'),('🪙 Crypto wallets','ui:wallets'),
            ('🔔 Daily reminder','ui:reminder'),('📊 Statistics','ui:stats'),('📨 Notification queue','ui:jobs'),
            ('💾 Export backup','ful:backupask'),('🆔 My Telegram ID','admin:myid'),('❔ Help','store:help')])

    async def home(self, chat):
        pending=unpack(await self.b.db.fetchval('SELECT state FROM users WHERE id=$1',chat))
        await self.b.state(chat,{})
        if pending:
            await self.b.main_bot.send(chat,'↩️ Back to the store.',{'remove_keyboard':True})
        await self.welcome_media(chat,self.b.main_bot)
        return await self.b.main_bot.send(chat,await self.welcome_text(),self.b.home_buttons())

    async def countries(self, chat, page=0, checkout=False, product_id=None):
        if product_id is not None:
            return await self.send(chat,'Country profiles are paused. All published products use the shared store.',MENU)
        return await self.home(chat)

    async def catalog(self, chat, offset=0):
        rows=await self.b.db.fetch('SELECT * FROM products WHERE published ORDER BY id DESC LIMIT 5 OFFSET $1',max(0,offset))
        if not rows:
            return await self.b.main_bot.send(chat,'New products are coming soon.',self.keys([('Support','support'),('← Store','home')]))
        for product in rows: await self.b.product_card(chat,product)
        buttons=[('← Store','home')]
        if len(rows)==5: buttons.insert(0,('More products',f'catalog:{offset+5}'))
        await self.b.main_bot.send(chat,'🛍 Browse photos and USD prices. Select a product whenever you are ready to order.',self.keys(buttons))

    async def start_checkout(self, chat, product_id, method, request_key, currency=None):
        product=await self.b.db.fetchrow('SELECT * FROM products WHERE id=$1 AND published',product_id)
        if not product: raise ValueError('Product unavailable.')
        if product['kind']=='digital':
            return await self.b.begin_checkout(chat,product_id,method,request_key,currency,delivery_ready=True)
        if method=='local': currency='USD'
        elif method not in self.b.enabled_crypto(): raise ValueError('This crypto option is unavailable.')
        city=await self.b.db.fetchval('SELECT city FROM users WHERE id=$1',chat)
        data=dict(drop_checkout=True,general_store=1,token=secrets.token_hex(5),pid=product_id,method=method,currency=currency,request_key=request_key,stage='city',city=city or '')
        await self.b.state(chat,data)
        if method=='local':
            await self.b.alert_admins(self.b.db,'bank-choice:'+request_key,
                f'🏦 Local bank details requested\nCustomer: {chat}\nProduct: {product["name"]}\nPrices are in USD. Discuss the bank and any local-currency payment amount with the customer.',
                buttons=self.keys([('Message customer',f'ful:chatuser:{chat}')]))
            await self.b.main_bot.send(chat,'🏦 Message support for local bank details.\nSupport has been notified and can reply here. No bank account is displayed automatically.',self.keys([('💬 Message support','storechat')]))
        return await self.city_prompt(chat,data)

    async def pin_prompt(self, chat, data):
        await self.b.main_bot.send(chat,'📍 Location-drop delivery\nSend your chosen drop-off point on the map so we can follow it and make the drop there. This is physical delivery to an agreed location, not home delivery.\n\nUse Telegram’s attachment → Location to choose a map pin. Share your current location only if that is the intended drop-off point.',
            {'keyboard':[[{'text':'📍 Share current location','request_location':True}],[{'text':'✖ Cancel'}]],'resize_keyboard':True,'one_time_keyboard':True})
        saved=unpack(await self.b.db.fetchval('SELECT dropoff FROM users WHERE id=$1',chat))
        if saved:
            await self.b.main_bot.send(chat,'Or use your saved drop-off point.',self.keys([('Use saved map point',f'drop:{data["token"]}:savedpin'),('✖ Cancel','home')]))

    async def schedule_prompt(self, chat, data):
        await self.b.main_bot.send(chat,'Map point received.',{'remove_keyboard':True})
        note=await self.copy('delivery')
        return await self.b.main_bot.send(chat,f'🕒 When would you like delivery in {data.get("city","your city")}?\n{note}\nYou can also tell support a preferred time.',self.keys([
            ('Same-day / as soon as available',f'drop:{data["token"]}:asap'),('Set a preferred delivery time',f'drop:{data["token"]}:preferred'),('✖ Cancel','home')]))

    async def dates(self, chat, data):
        return await self.schedule_prompt(chat,data)

    async def times(self, chat, data, day):
        return await self.schedule_prompt(chat,data)

    async def checkout_review(self, chat, data):
        product=await self.b.db.fetchrow('SELECT * FROM products WHERE id=$1 AND published',data['pid'])
        if not product: raise ValueError('Product unavailable.')
        data['schedule']['label']=delivery_words(data['schedule']['label'])
        subtotal,discount,total=quote_usd(product['price'])
        await self.b.main_bot.call('sendLocation',chat_id=chat,**location_pin(data['pin']))
        await self.b.main_bot.send(chat,f'Review your order\n{product["name"]}\nCity: {data["city"]}\nPhysical location-drop delivery\nRequested time: {data["schedule"]["label"]}\nSubtotal: {self.b.format_amount(subtotal,"USD")}\nDiscount: {self.b.format_amount(discount,"USD")}\nTotal: {self.b.format_amount(total,"USD")}\n\nSupport confirms delivery availability and estimated delivery time for your city.',self.keys([
            ('✅ Confirm checkout',f'drop:{data["token"]}:confirm'),('📍 Change map point',f'drop:{data["token"]}:pin'),('🕒 Change delivery time',f'drop:{data["token"]}:reschedule'),('🛡 Delivery & refund policy','policy'),('✖ Cancel','home')]))

    async def drop_action(self, chat, parts):
        user=await self.b.db.fetchrow('SELECT * FROM users WHERE id=$1',chat)
        data=unpack(user['state'])
        if not data.get('drop_checkout') or data.get('token')!=parts[1]: raise ValueError('This checkout form is closed. Choose the product again.')
        if not data.get('general_store'):
            data.update(general_store=1,stage='city',city=user.get('city','') if isinstance(user,dict) else user['city'])
            data.pop('country',None);await self.b.state(chat,data);return await self.city_prompt(chat,data)
        action=parts[2]
        if action=='savedcity' and data['stage']=='city' and user['city']:
            data.update(city=user['city'],stage='pin');await self.b.state(chat,data);return await self.pin_prompt(chat,data)
        if action=='pin':
            data['stage']='pin';await self.b.state(chat,data);return await self.pin_prompt(chat,data)
        if action=='savedpin' and data['stage']=='pin':
            data.update(pin=location_pin(unpack(user['dropoff'])),stage='schedule');await self.b.state(chat,data);return await self.schedule_prompt(chat,data)
        if action=='reschedule' and data.get('pin'):
            data['stage']='schedule';await self.b.state(chat,data);return await self.schedule_prompt(chat,data)
        if action in ('asap','preferred','days','day','time') and data['stage']=='schedule':
            if action!='asap':
                data['stage']='preferred_time';await self.b.state(chat,data)
                return await self.b.main_bot.send(chat,'Send your preferred delivery time in your city’s local time, for example “6 PM today”. Support will confirm the time with you.',self.keys([('✖ Cancel','home')]))
            data.update(schedule={'mode':'asap','label':'Same-day / as soon as available; support confirms the estimated delivery time'},stage='review')
            await self.b.state(chat,data);return await self.checkout_review(chat,data)
        if action=='confirm' and data['stage']=='review':
            product=await self.b.db.fetchrow('SELECT id FROM products WHERE id=$1 AND published',data['pid'])
            if not product: raise ValueError('Product unavailable.')
            pin=location_pin(data['pin'])
            await self.b.db.execute('UPDATE users SET city=$2,dropoff=$3::jsonb,delivery_schedule=$4::jsonb,shipping=$5 WHERE id=$1',chat,data['city'],json.dumps(pin),json.dumps(data['schedule']),f'City: {data["city"]} | Location drop: {pin["latitude"]}, {pin["longitude"]} | {data["schedule"]["label"]}')
            await self.b.begin_checkout(chat,data['pid'],data['method'],'drop:'+data['request_key'],'USD' if data['method']=='local' else None,delivery_ready=True)
            return
        raise ValueError('Use the buttons on the latest checkout prompt.')

    async def snapshot_delivery(self, conn, order, product, user):
        if product['kind']!='physical': return order
        pin=location_pin(unpack(user['dropoff']));schedule=unpack(user['delivery_schedule'])
        if not user['city'] or not schedule.get('label'): raise ValueError('Enter your city and choose your delivery time first.')
        schedule['label']=delivery_words(schedule['label'])
        subtotal,discount,total=quote_usd(product['price'])
        order=await conn.fetchrow("UPDATE orders SET country='',city=$2,dropoff=$3::jsonb,delivery_schedule=$4::jsonb,subtotal_usd=$5,discount_usd=$6 WHERE id=$1 RETURNING *",order['id'],user['city'],json.dumps(pin),json.dumps(schedule),subtotal,discount)
        await self.b.alert_admins(conn,f'new-drop:{order["id"]}',f'New order #{order["id"]}\nCustomer: {order["user_id"]}\nCity: {user["city"]}\nProduct: {order["name"]}\nRequested time: {schedule["label"]}\nPayment: {order["method"]}\nConfirm the city’s availability, payment and estimated delivery time.',buttons=self.keys([('Open order',f'ui:order:{order["id"]}')]))
        return order

    def delivery_summary(self, o):
        if o['kind']!='physical': return ''
        schedule=unpack(o.get('delivery_schedule'))
        text='\n\n📍 Physical location-drop delivery'
        if o.get('city'): text+='\nCity: '+o['city']
        if schedule.get('label'): text+='\nRequested: '+schedule['label']
        text+='\nEstimated delivery time: '+(o.get('eta') or 'Support will confirm for your city')
        if o.get('subtotal_usd') is not None:
            subtotal=Decimal(o['subtotal_usd']);discount=Decimal(o.get('discount_usd',0))
            text+=f'\nSubtotal: {self.b.format_amount(subtotal,"USD")}\nDiscount: {self.b.format_amount(discount,"USD")}\nUSD total: {self.b.format_amount(subtotal-discount,"USD")}'
        return delivery_words(text)

    def order_text(self, order):
        o=dict(order)
        if o['method']=='local' and o['status'] in OPEN:
            title=f'Order #{o["id"]} · {o["name"]}\nAmount: {self.b.format_amount(o["amount"],o["currency"])}\nReference: SNOW-{o["id"]}'
            if o.get('bank_released'):
                title+='\n\nPayment instructions from the store:\n'+o['note']+'\n\nAfter paying, upload your payment receipt or screenshot here (or tap Upload receipt / screenshot).'
            else:
                title+='\n\n🏦 Bank transfer requested\nThe admin has been notified and will contact you here to arrange the bank transfer. Wait for payment instructions before sending money.'
            if o['status']=='review': title+='\nYour receipt is awaiting review.'
            return title+self.delivery_summary(o)
        return super().order_text(o)+self.delivery_summary(o)

    def order_buttons(self, order, bank=None):
        o=dict(order);buttons=super().order_buttons(o,bank if o.get('bank_released') else None)
        extra=[]
        if o['method']=='local':
            for row in buttons['inline_keyboard']:
                for button in row:
                    if button.get('text')=='📋 Copy amount': button['text']='📋 Copy USD order total' if o['currency']=='USD' else '📋 Copy order amount ('+o['currency']+')'
        if o['method']=='local' and o['status'] in OPEN:
            extra.append([{'text':'💬 Message store about payment','callback_data':f'orderchat:{o["id"]}'}])
        if unpack(o.get('dropoff')):
            extra.append([{'text':'📍 View my drop-off point','callback_data':f'droppoint:{o["id"]}'}])
        extra.append([{'text':'🛡 Delivery & refund policy','callback_data':'policy'}])
        if o['status'] in ('paid','fulfilled'):
            extra.append([{'text':'↩️ Request a refund','callback_data':f'refundrequest:{o["id"]}'}])
        buttons['inline_keyboard']=extra+buttons['inline_keyboard']
        return buttons

    async def extra_order_buttons(self, o):
        buttons=[('💬 Message customer',f'ful:chatorder:{o["id"]}')]
        if o['method']=='local' and o['status'] in OPEN:
            buttons += [('🏦 Choose bank template',f'store:banktemplates:{o["id"]}'),('✏️ Write bank instructions',f'ful:bankcustom:{o["id"]}')]
        if o['kind']=='physical':
            buttons.append(('📍 View drop-off point',f'ful:drop:{o["id"]}'))
            if o['status'] in ('paid','fulfilled'):
                buttons += [('🕒 Set / update estimated delivery time',f'ful:eta:{o["id"]}'),('📡 Share delivery location',f'ful:track:{o["id"]}'),('⏹ Stop live tracking',f'ful:stoptrack:{o["id"]}')]
        return buttons

    async def extra_product_buttons(self, product):
        return []

    async def wallets(self, chat):
        lines=[]
        for asset in self.b.ASSETS:
            if asset=='TON': continue
            lines.append(f'{"✅" if asset in self.b.enabled_crypto() else "⚠️"} {asset} · {self.b.ASSETS[asset][0]}\n{os.getenv(asset+"_ADDRESS","").strip() or "Not configured"}')
        return await self.send(chat,'Crypto wallets\n\n'+'\n\n'.join(lines)+'\n\nTON is removed from new checkouts. Existing TON transactions remain available for payment review.')

    async def customer_command(self, message):
        text=message.get('text','').split(maxsplit=1)
        command=text[0].split('@')[0].lower() if text else ''
        if command in ('/start','/cancel') or (message.get('text','')=='✖ Cancel'):
            await self.home(message['chat']['id']);return True
        if command in ('/support','/paysupport'):
            await self.customer_callback(message['chat']['id'],'support');return True
        if command=='/address':
            await self.customer_callback(message['chat']['id'],'deliverydetails');return True
        return False

    async def _previous_customer_input(self, message, pending):
        chat=message['chat']['id']
        if pending.get('drop_checkout'):
            if pending.get('stage')!='pin':
                await self.b.main_bot.send(chat,'Use the delivery buttons to continue.',self.keys([('✖ Cancel','home')]));return True
            if not message.get('location'): raise ValueError('Send a map pin using Telegram’s attachment → Location. A typed address is not a location-drop point.')
            pending.update(pin=location_pin(message['location']),stage='schedule')
            await self.b.state(chat,pending);await self.schedule_prompt(chat,pending);return True
        if pending.get('profile_pin'):
            if not message.get('location'): raise ValueError('Attach a map location pin for the physical drop-off point.')
            pin=location_pin(message['location'])
            await self.b.db.execute('UPDATE users SET dropoff=$2::jsonb WHERE id=$1',chat,json.dumps(pin))
            await self.b.main_bot.send(chat,'Map point saved.',{'remove_keyboard':True});await self.home(chat);return True
        if pending.get('order_message') or pending.get('store_message'):
            oid=pending.get('order_message')
            if oid: await self.owned_order(chat,oid)
            text=message.get('text','').strip()
            if not 1<=len(text)<=2000: raise ValueError('Send your message as text (up to 2,000 characters). Tap Upload receipt / screenshot to send payment proof.')
            request=f'customer-message:{chat}:{message.get("message_id",0)}'
            async with self.b.db.acquire() as conn:
                async with conn.transaction():
                    sent=await conn.fetchval("INSERT INTO order_messages(request_key,order_id,customer_id,direction,body) VALUES($1,$2,$3,'customer',$4) ON CONFLICT DO NOTHING RETURNING id",request,oid,chat,text)
                    if sent: await self.b.alert_admins(conn,request,f'Customer message'+(f' · order #{oid}' if oid else '')+f'\nCustomer: {chat}\n\n{text}',buttons=self.keys([('Reply to customer',f'ful:chatorder:{oid}' if oid else f'ful:chatuser:{chat}')]))
            await self.b.state(chat,{})
            await self.b.main_bot.send(chat,'Your message was sent to the store.',self.keys(([('View order',f'receipt:{oid}')] if oid else [])+[('← Store','home')]));return True
        return await super().customer_input(message,pending)

    async def _previous_customer_callback(self, chat, data):
        if data=='home': await self.home(chat);return True
        if data.startswith('catalog:'): await self.catalog(chat,int(data.split(':')[1]));return True
        if data.startswith('countries:'): await self.countries(chat,int(data.split(':')[1]));return True
        if data.startswith(('countryset:','dropcountry:')):
            action,code=data.split(':')
            if code not in COUNTRIES: raise ValueError('Choose a country from the buttons.')
            if action=='dropcountry':
                state=unpack(await self.b.db.fetchval('SELECT state FROM users WHERE id=$1',chat))
                if not state.get('drop_checkout') or state.get('stage')!='country': raise ValueError('Start a new checkout to choose its country.')
                p=await self.b.db.fetchrow('SELECT country FROM products WHERE id=$1 AND published',state['pid'])
                if not p or (p['country'] and p['country']!=code): raise ValueError('This product is not listed for that country.')
                state.update(country=code,stage='pin');await self.b.state(chat,state)
                await self.b.db.execute('UPDATE users SET country=$2 WHERE id=$1',chat,code)
                await self.pin_prompt(chat,state)
            else:
                await self.b.db.execute('UPDATE users SET country=$2 WHERE id=$1',chat,code);await self.home(chat)
            return True
        if data.startswith('dropcountries:'): await self.countries(chat,int(data.split(':')[1]),checkout=True);return True
        if data.startswith('drop:'): await self.drop_action(chat,data.split(':'));return True
        if data=='deliverydetails':
            await self.b.state(chat,{'profile_pin':True})
            await self.b.main_bot.send(chat,'Send a map pin for your preferred drop-off point. Physical orders are delivered to an agreed location, not a home address.',{'keyboard':[[{'text':'📍 Share current location','request_location':True}],[{'text':'✖ Cancel'}]],'resize_keyboard':True,'one_time_keyboard':True});return True
        if data.startswith('droppoint:'):
            o=await self.owned_order(chat,int(data.split(':')[1]));pin=unpack(o['dropoff'])
            if not pin: raise ValueError('This older order has no map point saved. Contact the store.')
            await self.b.main_bot.call('sendLocation',chat_id=chat,**location_pin(pin));return True
        if data=='storechat':
            await self.b.state(chat,{'store_message':True})
            await self.b.main_bot.send(chat,'Type your message to the store.',self.keys([('✖ Cancel','home')]))
            return True
        if data.startswith('orderchat:'):
            oid=int(data.split(':')[1]);await self.owned_order(chat,oid)
            await self.b.state(chat,{'order_message':oid})
            await self.b.main_bot.send(chat,f'Type your message about order #{oid}. The store can reply to you here.',self.keys([('✖ Cancel','home')]));return True
        if data.startswith('vendors:'): await self.public_vendors(chat,int(data.split(':')[1]));return True
        if data.startswith('vendor:'): await self.public_vendor(chat,int(data.split(':')[1]));return True
        return await super().customer_callback(chat,data)

    async def _previous_prompt(self, chat, data, token):
        flow=data['flow'];step=data['step'];key=lambda value:f'ful:form:{token}:{value}'
        if flow=='manual_bank':
            prompts={'instructions':'Type the bank payment instructions to send this customer. This will only be sent after you confirm (up to 1,800 characters).','number':'Send the exact account / IBAN / mobile-money number for the Copy button (up to 200 characters).','image':'Attach a bank-details screenshot as a photo, or tap Skip.'}
            if step=='review':
                return await self.send(chat,f'Send these instructions for order #{data["oid"]}?\n\n{data["instructions"]}\nCopyable account: {data["number"]}',[('✅ Send to customer',key('send'))]+CANCEL)
            return await self.send(chat,prompts[step],([('Skip image',key('skip'))] if step=='image' else [])+CANCEL)
        prompts={'message':'Type your message to the customer (up to 2,000 characters).','eta':'Send the estimated delivery time, including the date and timezone. This will be sent to the customer.','tracking':'Attach the delivery location. To share genuine live movement, use Telegram attachment → Location → Share Live Location. A static pin sends a one-time map update.'}
        if flow in prompts: return await self.send(chat,prompts[flow],CANCEL)
        if flow=='vendor':
            prompts={'name':'Send the vendor’s name.','country':'Send the country name or two-letter code, or tap All countries.','contact':'Send the public Telegram username (@username) or HTTPS contact link.','description':'Send a short vendor description (up to 1,000 characters).'}
            if step=='review': return await self.send(chat,f'Review vendor\n{data["name"]}\n{COUNTRIES.get(data["country"],"All countries")}\n{data["contact"]}\n{data["description"]}\nInitial status: Upcoming',[('Save upcoming vendor',key('savevendor'))]+CANCEL)
            return await self.send(chat,prompts[step],([('All countries',key('allcountries'))] if step=='country' else [])+CANCEL)
        return await super().prompt(chat,data,token)

    async def _previous_admin_message(self, message, request_key):
        chat=message['chat']['id']
        text=(message.get('text') or message.get('caption') or '').strip()
        if text in ('/start','/admin','/help','/cancel'):
            return await super().admin_message(message,request_key)
        row=await self.session(chat)
        if not row: return await super().admin_message(message,request_key)
        data=unpack(row['data']);flow=data['flow'];step=data['step']
        if flow not in ('message','eta','tracking','manual_bank','vendor'): return await super().admin_message(message,request_key)
        if data.get('last_message')==message.get('message_id'):
            return await self.prompt(chat,data,row['token'])
        image=row['image']
        if flow=='message':
            if not 1<=len(text)<=2000: raise ValueError('Send 1–2,000 characters of text.')
            await self.send_customer_message(chat,data,text,request_key)
            await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1',chat)
            return await self.send(chat,'Message queued for the customer. Customer replies appear in the admin bot.',MENU)
        if flow=='eta':
            if not 1<=len(text)<=500: raise ValueError('Send the confirmed estimated delivery time in 1–500 characters.')
            text=delivery_words(text)
            async with self.b.db.acquire() as conn:
                async with conn.transaction():
                    order=await conn.fetchrow("UPDATE orders SET eta=$2 WHERE id=$1 AND kind='physical' AND status IN ('paid','fulfilled') RETURNING *",data['oid'],text)
                    if not order: raise ValueError('Only paid physical orders can have an estimated delivery time confirmed.')
                    await self.b.queue(conn,f'eta:{request_key}','main',order['user_id'],f'Delivery update for order #{order["id"]}\nEstimated delivery time: {text}\nPhysical location-drop delivery.',buttons=self.keys([('View order',f'receipt:{order["id"]}'),('View drop-off point',f'droppoint:{order["id"]}')]))
                    await conn.execute('DELETE FROM admin_sessions WHERE admin_id=$1',chat)
            return await self.order_detail(chat,data['oid'])
        if flow=='tracking':
            if not message.get('location'): raise ValueError('Send the delivery location as a Telegram map location.')
            await self.share_tracking(chat,data['oid'],message)
            await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1',chat)
            return await self.order_detail(chat,data['oid'])
        if flow=='manual_bank':
            if step=='instructions':
                if not 1<=len(text)<=1800: raise ValueError('Send bank instructions of 1–1,800 characters.')
                data.update(instructions=text,step='number')
            elif step=='number':
                if not 1<=len(text)<=200: raise ValueError('Send an account number of 1–200 characters.')
                data.update(number=text,step='image')
            elif step=='image':
                if not message.get('photo'): raise ValueError('Send a photo or tap Skip image.')
                image=await self.b.admin_bot.download(message['photo'][-1]['file_id'],5*1024*1024);data['step']='review'
            else: return await self.prompt(chat,data,row['token'])
        if flow=='vendor':
            if step=='name':
                if not 1<=len(text)<=80: raise ValueError('Send a vendor name of 1–80 characters.')
                data.update(name=text,step='country')
            elif step=='country':
                code=next((c for c,n in COUNTRIES.items() if text.lower() in (c.lower(),n.lower())),None)
                if not code: raise ValueError('Enter a supported country name or code, or tap All countries.')
                data.update(country=code,step='contact')
            elif step=='contact':
                if not (re.fullmatch(r'@[A-Za-z0-9_]{5,32}',text) or (text.startswith('https://') and len(text)<=240)): raise ValueError('Send an @username or an HTTPS contact link.')
                data.update(contact=text,step='description')
            elif step=='description':
                if not 1<=len(text)<=1000: raise ValueError('Send a description of 1–1,000 characters.')
                data.update(description=text,step='review')
            else: return await self.prompt(chat,data,row['token'])
        data['last_message']=message.get('message_id')
        await self.b.db.execute('UPDATE admin_sessions SET data=$2::jsonb,image=$3 WHERE admin_id=$1 AND token=$4',chat,json.dumps(data),image,row['token'])
        return await self.prompt(chat,data,row['token'])

    async def send_customer_message(self, admin_id, data, text, request_key):
        oid=data.get('oid');customer=data.get('customer_id')
        if oid:
            order=await self.b.db.fetchrow('SELECT user_id FROM orders WHERE id=$1',oid)
            if not order: raise ValueError('Order not found.')
            customer=order['user_id']
        if not customer or not await self.b.db.fetchval('SELECT 1 FROM users WHERE id=$1',customer): raise ValueError('Customer not found.')
        async with self.b.db.acquire() as conn:
            async with conn.transaction():
                mid=await conn.fetchval("INSERT INTO order_messages(request_key,order_id,customer_id,admin_id,direction,body) VALUES($1,$2,$3,$4,'admin',$5) ON CONFLICT DO NOTHING RETURNING id",'admin-message:'+request_key,oid,customer,admin_id,text)
                if mid:
                    buttons=self.keys([('Reply to store',f'orderchat:{oid}'),('View order',f'receipt:{oid}')]) if oid else self.keys([('Reply to store','storechat'),('← Store','home')])
                    await self.b.queue(conn,'admin-message:'+request_key,'main',customer,f'Message from SnowmanBot'+(f' · order #{oid}' if oid else '')+'\n\n'+text,buttons=buttons)

    async def manual_form(self, chat, token, action):
        row=await self.session(chat)
        if not row or row['token']!=token: raise ValueError('This form is closed.')
        data=unpack(row['data'])
        if data['flow']=='manual_bank' and data['step']=='image' and action=='skip':
            data['step']='review';await self.b.db.execute('UPDATE admin_sessions SET data=$2::jsonb WHERE admin_id=$1',chat,json.dumps(data));return await self.prompt(chat,data,token)
        if data['flow']=='vendor' and data['step']=='country' and action=='allcountries':
            data.update(country='',step='contact');await self.b.db.execute('UPDATE admin_sessions SET data=$2::jsonb WHERE admin_id=$1',chat,json.dumps(data));return await self.prompt(chat,data,token)
        if data['flow']=='manual_bank' and data['step']=='review' and action=='send':
            bank={'instructions':data['instructions'],'account_number':data['number'],'account_name':'','bank_name':''}
            await self.release_bank(chat,data['oid'],bank,row['image'])
            await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1 AND token=$2',chat,token)
            return await self.order_detail(chat,data['oid'])
        if data['flow']=='vendor' and data['step']=='review' and action=='savevendor':
            async with self.b.db.acquire() as conn:
                async with conn.transaction():
                    active=await conn.fetchval('SELECT token FROM admin_sessions WHERE admin_id=$1 FOR UPDATE',chat)
                    if active!=token: raise ValueError('This vendor was already saved.')
                    vid=await conn.fetchval('INSERT INTO vendors(name,country,contact,description) VALUES($1,$2,$3,$4) RETURNING id',data['name'],data['country'],data['contact'],data['description'])
                    await conn.execute('DELETE FROM admin_sessions WHERE admin_id=$1',chat)
            return await self.vendor_detail(chat,vid)
        raise ValueError('Use the buttons on the latest prompt.')

    async def release_bank(self, admin_id, oid, bank, image=None):
        import hashlib
        digest=hashlib.sha256((json.dumps(bank,sort_keys=True)).encode()+(image or b'')).hexdigest()[:24]
        async with self.b.db.acquire() as conn:
            async with conn.transaction():
                o=await conn.fetchrow('SELECT * FROM orders WHERE id=$1 FOR UPDATE',oid)
                if not o or o['method']!='local' or o['status'] not in OPEN: raise ValueError('This bank order is not awaiting payment.')
                await conn.execute('INSERT INTO order_bank_details(order_id,data,image) VALUES($1,$2::jsonb,$3) ON CONFLICT(order_id) DO UPDATE SET data=$2::jsonb,image=$3',oid,json.dumps(bank),image)
                await conn.execute('UPDATE orders SET note=$2,bank_released=true WHERE id=$1',oid,self.bank_text(bank))
                await conn.execute("INSERT INTO payment_audit(order_id,admin_id,action,reason) VALUES($1,$2,'bank_details_sent','Payment details released manually by admin')",oid,admin_id)
                await self.b.queue(conn,f'bank-release:{oid}:{digest}','bank_details',o['user_id'],str(oid))

    async def share_tracking(self, admin_id, oid, message):
        o=await self.b.db.fetchrow('SELECT * FROM orders WHERE id=$1',oid)
        if not o or o['kind']!='physical' or o['status'] not in ('paid','fulfilled'): raise ValueError('Delivery tracking is available for paid physical orders.')
        pin=location_pin(message['location'])
        period=int(message['location'].get('live_period') or 0)
        if period:
            source_date=int(message.get('date') or self.b.now().timestamp())
            remaining=min(86400,source_date+period-int(self.b.now().timestamp()))
            if remaining<60: raise ValueError('This live location is expiring. Share a fresh live location.')
            existing=await self.b.db.fetchrow('SELECT * FROM delivery_live WHERE order_id=$1',oid)
            if existing and existing['active'] and existing['source_message_id']==message['message_id'] and existing['admin_id']==admin_id:
                return
            await self.stop_tracking(oid)
            sent=await self.b.main_bot.call('sendLocation',chat_id=o['user_id'],**pin,live_period=remaining)
            await self.b.db.execute('''INSERT INTO delivery_live(order_id,admin_id,source_message_id,customer_message_id,last_lat,last_lon,expires)
                VALUES($1,$2,$3,$4,$5,$6,now()+$7*interval '1 second') ON CONFLICT(order_id) DO UPDATE SET admin_id=$2,source_message_id=$3,customer_message_id=$4,last_lat=$5,last_lon=$6,expires=now()+$7*interval '1 second',updated=now(),active=true''',oid,admin_id,message['message_id'],sent['message_id'],pin['latitude'],pin['longitude'],remaining)
            kind='Live delivery location shared. The map updates when the admin sends a live-location update.'
        else:
            await self.b.main_bot.call('sendLocation',chat_id=o['user_id'],**pin)
            kind='Delivery map update (one-time location pin).'
        await self.b.main_bot.send(o['user_id'],f'Order #{oid}\n{kind}\nEstimated delivery time: {delivery_words(o["eta"]) if o["eta"] else "Awaiting store confirmation"}',self.keys([('View order',f'receipt:{oid}')]))

    async def handle_live_edit(self, message):
        admin=message['from']['id']
        if admin not in self.b.ADMINS: return
        row=await self.b.db.fetchrow('''SELECT l.*,o.user_id,o.status FROM delivery_live l JOIN orders o ON o.id=l.order_id
            WHERE l.admin_id=$1 AND l.source_message_id=$2 AND l.active''',admin,message['message_id'])
        if not row: return
        if not message.get('location') or not message['location'].get('live_period') or row['expires']<=self.b.now() or row['status'] not in ('paid','fulfilled'):
            return await self.stop_tracking(row['order_id'])
        pin=location_pin(message['location'])
        if row['last_lat']==pin['latitude'] and row['last_lon']==pin['longitude']: return
        await self.b.main_bot.call('editMessageLiveLocation',chat_id=row['user_id'],message_id=row['customer_message_id'],**pin)
        await self.b.db.execute('UPDATE delivery_live SET last_lat=$2,last_lon=$3,updated=now() WHERE order_id=$1',row['order_id'],pin['latitude'],pin['longitude'])

    async def stop_tracking(self, oid):
        row=await self.b.db.fetchrow('SELECT l.*,o.user_id FROM delivery_live l JOIN orders o ON o.id=l.order_id WHERE l.order_id=$1 AND l.active',oid)
        if not row: return
        try: await self.b.main_bot.call('stopMessageLiveLocation',chat_id=row['user_id'],message_id=row['customer_message_id'])
        except self.b.TGError as e:
            if e.code not in (400,403): raise
        await self.b.db.execute('UPDATE delivery_live SET active=false,updated=now() WHERE order_id=$1',oid)

    async def vendor_list(self, chat, page=0):
        page=max(0,page)
        rows=await self.b.db.fetch('SELECT * FROM vendors ORDER BY id DESC LIMIT 9 OFFSET $1',page*8)
        items=[(f'#{v["id"]} {v["name"]} · {v["status"]}',f'ful:vendor:{v["id"]}') for v in rows[:8]]
        if page: items.append(('← Previous',f'ful:vendors:{page-1}'))
        if len(rows)>8: items.append(('Next →',f'ful:vendors:{page+1}'))
        return await self.send(chat,'Vendor directory\nVerified means approved by the store after its own checks. Add real vendors before making them visible.',[('➕ Add vendor','ful:addvendor')]+items+MENU)

    async def vendor_detail(self, chat, vid):
        v=await self.b.db.fetchrow('SELECT * FROM vendors WHERE id=$1',vid)
        if not v: raise ValueError('Vendor not found.')
        return await self.send(chat,f'{v["name"]}\nCountry: {COUNTRIES.get(v["country"],"All countries")}\nStatus: {v["status"]}\nContact: {v["contact"]}\n\n{v["description"]}',[
            ('✅ Mark store-verified',f'ful:verifyask:{vid}'),('🕒 Mark upcoming',f'ful:vstatus:{vid}:upcoming'),('⏸ Hide vendor',f'ful:vstatus:{vid}:hidden'),('← Vendors','ful:vendors:0')])

    async def public_vendors(self, chat, page=0):
        country=await self.b.db.fetchval('SELECT country FROM users WHERE id=$1',chat)
        rows=await self.b.db.fetch("SELECT id,name,status FROM vendors WHERE status IN ('upcoming','verified') AND (country='' OR country=$1) ORDER BY status DESC,id DESC LIMIT 9 OFFSET $2",country or '',max(0,page)*8)
        items=[(f'{"✅" if v["status"]=="verified" else "🕒"} {v["name"]} · {v["status"]}',f'vendor:{v["id"]}') for v in rows[:8]]
        if page: items.append(('← Previous',f'vendors:{page-1}'))
        if len(rows)>8: items.append(('Next →',f'vendors:{page+1}'))
        return await self.b.main_bot.send(chat,'Vendors\n✅ Verified by SnowmanBot\n🕒 Upcoming\n\n'+('Choose a vendor for details.' if rows else 'The vendor directory is coming soon. No vendors have been listed for this profile yet.'),self.keys(items+[('← Store','home')]))

    async def public_vendor(self, chat, vid):
        v=await self.b.db.fetchrow("SELECT * FROM vendors WHERE id=$1 AND status IN ('upcoming','verified')",vid)
        country=await self.b.db.fetchval('SELECT country FROM users WHERE id=$1',chat)
        if not v or (v['country'] and v['country']!=country): raise ValueError('Vendor unavailable for this profile.')
        status='Verified by SnowmanBot' if v['status']=='verified' else 'Upcoming vendor'
        await self.b.main_bot.send(chat,f'{v["name"]}\n{status}\n{COUNTRIES.get(v["country"],"All countries")}\n\n{v["description"]}\n\nContact: {v["contact"]}',self.keys([('← Vendors','vendors:0'),('← Store','home')]))

    async def schedule_backup(self, moment):
        if self.backup_time.lower() in ('off','disabled',''): return
        minute=moment.strftime('%Y-%m-%d %H:%M')
        if self.last_backup_minute==minute or moment.strftime('%H:%M')<self.backup_time: return
        for admin_id in self.b.ADMINS:
            await self.b.queue(self.b.db,'daily-backup:'+moment.strftime('%Y-%m-%d')+':'+str(admin_id),'backup',admin_id,moment.strftime('%Y-%m-%d'))
        self.last_backup_minute=minute

    async def export_backup(self):
        # A data backup excludes environment variables, bot tokens and private keys.
        with tempfile.SpooledTemporaryFile(max_size=8*1024*1024) as output:
            with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                uncompressed_size=0
                manifest={'format':'snowman-backup-v1','version':self.b.VERSION,'created':self.b.now().isoformat(),'tables':[]}
                async with self.b.db.acquire() as conn:
                    async with conn.transaction(isolation='repeatable_read',readonly=True):
                        for table in BACKUP_TABLES:
                            columns=await conn.fetch('SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=\'public\' AND table_name=$1 ORDER BY ordinal_position',table)
                            if not columns: continue
                            count=0
                            with archive.open('data/'+table+'.jsonl','w') as stream:
                                async for record in conn.cursor(f'SELECT * FROM "{table}"',prefetch=1):
                                    encoded=(json.dumps(dict(record),default=backup_value,ensure_ascii=False)+'\n').encode()
                                    uncompressed_size+=len(encoded)
                                    if uncompressed_size>512*1024*1024: raise ValueError('This backup exceeds the restore size limit. Use your database provider’s backup facility.')
                                    stream.write(encoded)
                                    count+=1
                                    if output.tell()>40*1024*1024: raise ValueError('This backup exceeds the Telegram export size. Use your database provider’s backup/export facility for this database.')
                            manifest['tables'].append({'name':table,'rows':count})
                archive.writestr('manifest.json',json.dumps(manifest,indent=2))
                archive.writestr('RESTORE.txt','Use restore_backup.py from this SnowmanBot release with an EMPTY PostgreSQL database. Keep bot tokens and wallet configuration separately. Data backups cannot restore a Telegram-banned account or make a replacement bot contact people who have not started it.')
            if output.tell()>40*1024*1024: raise ValueError('Backup is too large for this Telegram export. Use a database-provider backup.')
            output.seek(0)
            return output.read()

    async def _previous_admin_callback(self, chat, data, request_key):
        if not data.startswith('ful:'): return await super().admin_callback(chat,data,request_key)
        parts=data.split(':');action=parts[1]
        if action=='help':
            return await self.send(chat,'Use buttons to manage products, countries, banks, payments, delivery times and vendors. Customers send a drop-off map pin and choose a requested delivery time. Bank details are only sent after an admin releases them. Real live tracking begins when an admin shares a Telegram live location for a paid order. Backups export database content without tokens.')
        if action=='chatorder': return await self.begin(chat,{'flow':'message','step':'text','oid':int(parts[2])})
        if action=='chatuser': return await self.begin(chat,{'flow':'message','step':'text','customer_id':int(parts[2])})
        if action=='eta': return await self.begin(chat,{'flow':'eta','step':'text','oid':int(parts[2])})
        if action=='track': return await self.begin(chat,{'flow':'tracking','step':'location','oid':int(parts[2])})
        if action=='stoptrack':
            await self.stop_tracking(int(parts[2]));return await self.order_detail(chat,int(parts[2]))
        if action=='drop':
            o=await self.b.db.fetchrow('SELECT dropoff FROM orders WHERE id=$1',int(parts[2]))
            if not o or not unpack(o['dropoff']): raise ValueError('This order has no map point stored.')
            await self.b.admin_bot.call('sendLocation',chat_id=chat,**location_pin(unpack(o['dropoff'])));return await self.order_detail(chat,int(parts[2]))
        if action=='bankcustom': return await self.begin(chat,{'flow':'manual_bank','step':'instructions','oid':int(parts[2])})
        if action=='form': return await self.manual_form(chat,parts[2],parts[3])
        if action in ('bankask','banksend'):
            oid=int(parts[2]);o=await self.b.db.fetchrow('SELECT * FROM orders WHERE id=$1',oid)
            if not o or o['method']!='local' or o['status'] not in OPEN: raise ValueError('This bank order is not awaiting payment.')
            snapshot=await self.b.db.fetchrow('SELECT * FROM order_bank_details WHERE order_id=$1',oid)
            if not snapshot: raise ValueError('No bank details are saved. Use Write bank instructions.')
            bank=unpack(snapshot['data'])
            if action=='bankask':
                if snapshot['image']: await self.b.admin_bot.call('sendPhoto',chat_id=chat,photo=('bank.png',snapshot['image']))
                return await self.send(chat,f'Send these saved bank details to customer {o["user_id"]} for order #{oid}?\n\n{self.bank_text(bank)}',[('✅ Send bank details',f'ful:banksend:{oid}'),('Write different instructions',f'ful:bankcustom:{oid}')]+MENU)
            await self.release_bank(chat,oid,bank,snapshot['image']);return await self.order_detail(chat,oid)
        if action=='pcountries': return await self.countries(chat,int(parts[3]),product_id=int(parts[2]))
        if action=='pcountry':
            code='' if parts[3]=='ALL' else parts[3]
            if code and code not in COUNTRIES: raise ValueError('Choose a supported country.')
            await self.b.db.execute('UPDATE products SET country=$2 WHERE id=$1',int(parts[2]),code);return await self.product_detail(chat,int(parts[2]))
        if action=='vendors': return await self.vendor_list(chat,int(parts[2]))
        if action=='vendor': return await self.vendor_detail(chat,int(parts[2]))
        if action=='addvendor': return await self.begin(chat,{'flow':'vendor','step':'name'})
        if action=='verifyask': return await self.send(chat,'Mark this vendor as verified by SnowmanBot? Confirm only after completing your own vendor checks.',[('Confirm store verification',f'ful:vstatus:{int(parts[2])}:verified')]+MENU)
        if action=='vstatus':
            if parts[3] not in ('verified','upcoming','hidden'): raise ValueError('Choose a valid status.')
            await self.b.db.execute('UPDATE vendors SET status=$2 WHERE id=$1',int(parts[2]),parts[3]);return await self.vendor_detail(chat,int(parts[2]))
        if action=='backupask': return await self.send(chat,'Export a private backup of users, products, orders, receipts, banks and settings? Tokens and wallet keys are excluded. The file is sent to this authorised admin chat. Store it securely. Backups preserve data; Telegram account restrictions are not reversed by a backup.',[('💾 Export database backup','ful:backup')]+MENU)
        if action=='backup':
            await self.send(chat,'Preparing a consistent database backup…')
            data=await self.export_backup()
            filename='SnowmanBot_backup_'+self.b.now().strftime('%Y%m%d_%H%M%S')+'.zip'
            await self.b.admin_bot.call('sendDocument',chat_id=chat,document=(filename,data))
            return await self.send(chat,'Backup exported. Keep this file together with the code release and your private hosting-variable configuration.')
        return await self.menu(chat)

    async def copy(self, field):
        defaults={
            'name':'SnowmanBot',
            'welcome':'🛍 Browse product photos, check prices in USD and place your order with a few taps.',
            'delivery':'📍 Same-day delivery in supported cities. Delivery time depends on your city; support confirms availability and the estimated delivery time.',
            'refund':'Delivery is guaranteed. If you do not receive the quality you ordered, contact support for a refund. Support arranges and processes refunds.',
            'support':self.b.SUPPORT,
        }
        if field not in defaults: raise ValueError('Choose a store-content field from the buttons.')
        value=await self.b.setting('store:'+field,defaults[field])
        return delivery_words(value) if field in ('welcome','delivery','refund') else value

    async def welcome_text(self):
        return f'✨ Welcome to {await self.copy("name")}\n\n{await self.copy("welcome")}\n\n{await self.copy("delivery")}\n\n💳 Choose crypto or message support for local bank details.\n\n🛡 {await self.copy("refund")}\n\n🎁 Physical orders above $1,000 receive $150 off.'

    async def welcome_media(self, chat, bot):
        row=await self.b.db.fetchrow("SELECT * FROM store_media WHERE key='welcome'")
        if not row: return
        try:
            if row['kind']=='sticker': await bot.call('sendSticker',chat_id=chat,sticker=(row['filename'],row['content']))
            else: await bot.call('sendPhoto',chat_id=chat,photo=(row['filename'],row['content']))
        except self.b.TGError as error:
            if error.code not in (400,403): raise
            self.b.LOG.warning('Welcome media could not be sent: Telegram %s',error.code)

    async def content_panel(self, chat):
        return await self.send(chat,'🎛 Store content & appearance\nEdit the text customers see. Product names, prices and photos are managed under Manage products.\n\nStore version: '+self.b.VERSION,[
            ('✏️ Store display name','store:edit:name'),('💬 Welcome message','store:edit:welcome'),('🚚 Delivery message','store:edit:delivery'),
            ('🛡 Delivery / refund policy','store:edit:refund'),('☎️ Support contact','store:edit:support'),
            ('✨ Upload welcome sticker / photo','store:media'),('🧹 Remove welcome media','store:clearmedia'),('👁 Preview welcome','store:preview')]+MENU)

    async def city_prompt(self, chat, data):
        buttons=[('✖ Cancel','home')]
        if data.get('city'): buttons.insert(0,('Use '+data['city'][:40],f'drop:{data["token"]}:savedcity'))
        return await self.b.main_bot.send(chat,'🏙 Send your city for the delivery, for example Dubai, Doha or Muscat. No country selection is needed. Support confirms the delivery time based on your city.',self.keys(buttons))

    async def local_methods(self):
        # An enquiry is always available; the operator does not need to publish an account or FX rate first.
        return {'USD':('Message support for local bank details.',Decimal('1'))}

    async def bank_help(self, chat, product_id=0, request_key=None):
        if product_id:
            return await self.start_checkout(chat,product_id,'local',request_key or 'bank:'+secrets.token_hex(6),'USD')
        key=request_key or 'bank-enquiry:'+secrets.token_hex(6)
        await self.b.alert_admins(self.b.db,'bank-enquiry:'+key,f'🏦 Local bank payment enquiry\nCustomer: {chat}\nPlease reply with bank details and agree the payment amount.',buttons=self.keys([('Message customer',f'ful:chatuser:{chat}')]))
        return await self.b.main_bot.send(chat,'🏦 Message support for local bank details.\nSupport has been notified. Product prices are in USD; support will explain how to pay by bank.',self.keys([('💬 Message support','storechat'),('Browse products','catalog:0'),('← Store','home')]))

    async def snapshot_bank(self, conn, order, account=None):
        data={field:'' for field in ('instructions','bank_name','account_name','account_number')}
        await conn.execute('INSERT INTO order_bank_details(order_id,data,image) VALUES($1,$2::jsonb,NULL) ON CONFLICT DO NOTHING',order['id'],json.dumps(data))

    async def bank_menu(self, chat):
        rows=await self.b.db.fetch('SELECT * FROM bank_accounts')
        items=[(f'{"✅" if r["enabled"] else "⏸"} {r["currency"]} · {r["bank_name"] or "Bank template"}',f'ui:bank:{r["currency"]}') for r in rows if r['currency'] in BANK_CODES]
        return await self.send(chat,'🏦 Private bank templates\nPrices stay in USD. These accounts are only sent after you explicitly release them for an order. No exchange rate is required.',[('➕ Add / update bank template','ui:bankcodes:0')]+items+MENU)

    async def currency_picker(self, chat, page=0):
        return await self.send(chat,'Choose a private bank-template currency. This does not change the customer’s USD order price.',[(code,'ui:banknew:'+code) for code in BANK_CODES]+[('← Bank templates','ui:banks')])

    async def bank_begin(self, chat, code):
        if code not in BANK_CODES: raise ValueError('Choose a USD or supported Gulf bank template.')
        old=await self.b.db.fetchrow('SELECT * FROM bank_accounts WHERE currency=$1',code)
        data={field:old[field] if old else '' for field in ('instructions','bank_name','account_name','account_number')}
        data.update(flow='bank_template',step='format',currency=code)
        return await self.begin(chat,data,old['image'] if old else None)

    async def bank_detail(self, chat, code):
        row=await self.b.db.fetchrow('SELECT * FROM bank_accounts WHERE currency=$1',code)
        if not row: return await self.bank_begin(chat,code)
        if row['image']: await self.b.admin_bot.call('sendPhoto',chat_id=chat,photo=('bank.png',row['image']))
        return await self.send(chat,f'Private template: {code}\n{self.bank_text(dict(row))}\n\nCustomer order prices remain in USD. Agree any local payment amount with the customer before sending these details.',[
            ('✏️ Edit template','ui:banknew:'+code),('⏸ Disable' if row['enabled'] else '▶️ Enable','ui:banktoggle:'+code),('← Bank templates','ui:banks')])

    async def customer_input(self, message, pending):
        chat=message['chat']['id']
        if pending.get('drop_checkout'):
            if not pending.get('general_store'):
                pending.update(general_store=1,stage='city',city='');pending.pop('country',None)
                await self.b.state(chat,pending);await self.city_prompt(chat,pending);return True
            stage=pending.get('stage')
            if stage=='city':
                text=message.get('text','').strip()
                if not 1<=len(text)<=100 or not any(ch.isalpha() for ch in text): raise ValueError('Send your city name (up to 100 characters).')
                pending.update(city=text,stage='pin');await self.b.state(chat,pending);await self.pin_prompt(chat,pending);return True
            if stage=='pin':
                if not message.get('location'): raise ValueError('Attach a map pin using Telegram’s attachment → Location.')
                pending.update(pin=location_pin(message['location']),stage='schedule');await self.b.state(chat,pending);await self.schedule_prompt(chat,pending);return True
            if stage=='preferred_time':
                text=message.get('text','').strip()
                if not 1<=len(text)<=160: raise ValueError('Send your preferred local delivery time (up to 160 characters).')
                pending.update(schedule={'mode':'preferred','label':text+' (requested local time; support confirms for '+pending['city']+')'},stage='review')
                await self.b.state(chat,pending);await self.checkout_review(chat,pending);return True
            await self.b.main_bot.send(chat,'Use the latest checkout buttons to continue.',self.keys([('✖ Cancel','home')]));return True
        return await self._previous_customer_input(message,pending)

    async def customer_callback(self, chat, data, request_key=None):
        if data in ('support','storecontact'):
            contact=await self.copy('support')
            await self.b.main_bot.send(chat,'💬 Support\n'+contact+'\n\nYou can also message support here about delivery, bank details or a refund.',self.keys([('Message support here','storechat'),('My orders','myorders'),('← Store','home')]))
            return True
        if data=='policy':
            await self.b.main_bot.send(chat,'🛡 Delivery & refund policy\n\n'+await self.copy('delivery')+'\n\n'+await self.copy('refund'),self.keys([('Message support','storechat'),('← Store','home')]))
            return True
        if data.startswith('refundrequest:'):
            oid=int(data.split(':')[1]);order=await self.owned_order(chat,oid)
            if order['status'] not in ('paid','fulfilled'): raise ValueError('Only a paid order can start a refund request.')
            await self.b.state(chat,{'order_message':oid})
            await self.b.main_bot.send(chat,f'Describe the issue with order #{oid} and say that you are requesting a refund. Support will review and arrange the refund with you.',self.keys([('✖ Cancel','home')]))
            return True
        if data.startswith('bankhelp:'):
            await self.bank_help(chat,int(data.split(':')[1]),request_key);return True
        if data.startswith('local:') or data.startswith('bank:'):
            await self.bank_help(chat,int(data.split(':')[1]),request_key);return True
        if data in ('currencies',) or data.startswith('currency:'):
            await self.bank_help(chat,request_key=request_key);return True
        if data.startswith(('countries:','countryset:','dropcountry:','dropcountries:','vendors:','vendor:')):
            pending=unpack(await self.b.db.fetchval('SELECT state FROM users WHERE id=$1',chat))
            if pending.get('drop_checkout'):
                pending.update(general_store=1,stage='city',city=pending.get('city',''));pending.pop('country',None)
                await self.b.state(chat,pending);await self.city_prompt(chat,pending)
            else: await self.home(chat)
            return True
        return await self._previous_customer_callback(chat,data)

    async def prompt(self, chat, data, token):
        flow=data['flow'];step=data['step'];key=lambda a:f'store:form:{token}:{a}'
        if flow=='store_text':
            if step=='review': return await self.send(chat,'Preview your new '+data['field']+' text:\n\n'+data['value'],[('✅ Save text',key('savetext'))]+CANCEL)
            current=await self.copy(data['field'])
            return await self.send(chat,'Send the new '+data['field']+' text. It will replace the current text after you review and save.\n\nCurrent:\n'+current,CANCEL)
        if flow=='store_media': return await self.send(chat,'Send a Telegram sticker (static, animated or video) or upload a welcome photo. The bot will show it when opening the store.',CANCEL)
        if flow=='bank_template':
            if step=='format': return await self.send(chat,'Choose how to update the private bank instructions.',[('Enter text instructions',key('text')),('Upload bank screenshot',key('image')),('Keep current instructions',key('keep'))]+CANCEL)
            prompts={'text':'Send the bank instructions (up to 1,800 characters).','image':'Upload the bank-details screenshot as a photo.','bank_name':'Send the bank / mobile-money provider name.','account_name':'Send the account holder name.','account_number':'Send the exact account / IBAN / mobile-money number for the Copy button.'}
            if step=='review': return await self.send(chat,'Review private '+data['currency']+' template\n\n'+self.bank_text(data)+'\n\nSaving will not publish the account to customers.',[('Save private template',key('savebank'))]+CANCEL)
            buttons=[('Keep current value',key('keepfield'))] if data.get(step) else []
            return await self.send(chat,prompts[step],buttons+CANCEL)
        return await self._previous_prompt(chat,data,token)

    async def admin_message(self, message, request_key):
        chat=message['chat']['id'];text=(message.get('text') or message.get('caption') or '').strip()
        if text.split('@')[0].lower() in ('/start','/admin','/help','/cancel'): return await self._previous_admin_message(message,request_key)
        row=await self.session(chat)
        if not row: return await self._previous_admin_message(message,request_key)
        data=unpack(row['data']);flow=data['flow']
        if flow in ('bank','vendor'):
            await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1',chat)
            return await self.send(chat,'That older form has closed. Use the updated menu.',MENU)
        if flow not in ('store_text','store_media','bank_template'): return await self._previous_admin_message(message,request_key)
        if data.get('last_message')==message.get('message_id'): return await self.prompt(chat,data,row['token'])
        if flow=='store_text':
            if data.get('step')=='review': return await self.send(chat,'Review the preview above, then save it.',[('Save text',f'store:form:{row["token"]}:savetext')]+CANCEL)
            limits={'name':60,'welcome':1000,'delivery':700,'refund':1400,'support':300}
            if not 1<=len(text)<=limits[data['field']]: raise ValueError('Text must contain 1–'+str(limits[data['field']])+' characters.')
            data.update(value=text,step='review',last_message=message.get('message_id'))
            await self.b.db.execute('UPDATE admin_sessions SET data=$2::jsonb WHERE admin_id=$1 AND token=$3',chat,json.dumps(data),row['token'])
            return await self.send(chat,'Preview your new '+data['field']+' text:\n\n'+text,[('✅ Save text',f'store:form:{row["token"]}:savetext')]+CANCEL)
        if flow=='store_media':
            if message.get('sticker'):
                item=message['sticker'];kind='sticker';filename='welcome.webm' if item.get('is_video') else 'welcome.tgs' if item.get('is_animated') else 'welcome.webp'
            elif message.get('photo'): item=message['photo'][-1];kind='photo';filename='welcome.jpg'
            else: raise ValueError('Send a Telegram sticker or a photo.')
            content=await self.b.admin_bot.download(item['file_id'],8*1024*1024)
            await self.b.db.execute("INSERT INTO store_media(key,kind,content,filename) VALUES('welcome',$1,$2,$3) ON CONFLICT(key) DO UPDATE SET kind=$1,content=$2,filename=$3",kind,content,filename)
            await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1',chat)
            return await self.send(chat,'Welcome '+kind+' saved. Use Preview welcome to check it.',[('Preview welcome','store:preview'),('← Store settings','store:settings')])
        step=data['step'];image=row['image']
        if step=='text':
            if not 1<=len(text)<=1800: raise ValueError('Send 1–1,800 characters of instructions.')
            data.update(instructions=text,step='bank_name');image=None
        elif step=='image':
            if not message.get('photo'): raise ValueError('Upload the bank screenshot as a photo.')
            image=await self.b.admin_bot.download(message['photo'][-1]['file_id'],5*1024*1024);data.update(instructions=message.get('caption','').strip()[:1800],step='bank_name')
        elif step in ('bank_name','account_name','account_number'):
            if not 1<=len(text)<=200: raise ValueError('Send 1–200 characters.')
            data[step]=text;data['step']={'bank_name':'account_name','account_name':'account_number','account_number':'review'}[step]
        else: return await self.prompt(chat,data,row['token'])
        data['last_message']=message.get('message_id')
        await self.b.db.execute('UPDATE admin_sessions SET data=$2::jsonb,image=$3 WHERE admin_id=$1 AND token=$4',chat,json.dumps(data),image,row['token'])
        return await self.prompt(chat,data,row['token'])

    async def store_form(self, chat, token, action):
        row=await self.session(chat)
        if not row or row['token']!=token: raise ValueError('This form is closed.')
        data=unpack(row['data'])
        if data['flow']=='store_text' and data['step']=='review' and action=='savetext':
            async with self.b.db.acquire() as conn:
                async with conn.transaction():
                    active=await conn.fetchval('SELECT token FROM admin_sessions WHERE admin_id=$1 FOR UPDATE',chat)
                    if active!=token: raise ValueError('This text was already saved or cancelled.')
                    await conn.execute('INSERT INTO settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=$2','store:'+data['field'],data['value'])
                    await conn.execute('DELETE FROM admin_sessions WHERE admin_id=$1 AND token=$2',chat,token)
            return await self.content_panel(chat)
        if data['flow']!='bank_template': raise ValueError('Use the current form buttons.')
        if data['step']=='format' and action in ('text','image','keep'):
            if action=='keep' and not (data.get('instructions') or row['image'] or data.get('account_number')): raise ValueError('Add bank instructions first.')
            data['step']='bank_name' if action=='keep' else action
        elif action=='keepfield' and data['step'] in ('bank_name','account_name','account_number') and data.get(data['step']):
            data['step']={'bank_name':'account_name','account_name':'account_number','account_number':'review'}[data['step']]
        elif action=='savebank' and data['step']=='review':
            async with self.b.db.acquire() as conn:
                async with conn.transaction():
                    active=await conn.fetchval('SELECT token FROM admin_sessions WHERE admin_id=$1 FOR UPDATE',chat)
                    if active!=token: raise ValueError('This bank template was already saved.')
                    await conn.execute('''INSERT INTO bank_accounts(currency,instructions,bank_name,account_name,account_number,image,enabled)
                        VALUES($1,$2,$3,$4,$5,$6,true) ON CONFLICT(currency) DO UPDATE SET instructions=$2,bank_name=$3,account_name=$4,account_number=$5,image=$6,enabled=true''',data['currency'],data['instructions'],data['bank_name'],data['account_name'],data['account_number'],row['image'])
                    await conn.execute('DELETE FROM admin_sessions WHERE admin_id=$1 AND token=$2',chat,token)
            return await self.bank_detail(chat,data['currency'])
        else: return await self.prompt(chat,data,token)
        await self.b.db.execute('UPDATE admin_sessions SET data=$2::jsonb WHERE admin_id=$1 AND token=$3',chat,json.dumps(data),token)
        return await self.prompt(chat,data,token)

    async def admin_callback(self, chat, data, request_key):
        if data in ('ful:help','admin:help','ui:help'): data='store:help'
        if data.startswith(('ful:pcountr','ful:vendor','ful:addvendor','ful:verifyask','ful:vstatus')):
            return await self.send(chat,'Country profiles and vendors are paused for a later phase. All published products appear in the shared store.',MENU)
        if data.startswith('ui:form:'):
            old=await self.session(chat)
            if old and unpack(old['data']).get('flow')=='bank':
                await self.b.db.execute('DELETE FROM admin_sessions WHERE admin_id=$1',chat)
                return await self.bank_menu(chat)
        if data.startswith(('ful:bankask:','ful:banksend:')):
            oid=int(data.split(':')[2]);snapshot=await self.b.db.fetchrow('SELECT * FROM order_bank_details WHERE order_id=$1',oid)
            bank=unpack(snapshot['data']) if snapshot else {}
            if not snapshot or not (snapshot['image'] or bank.get('instructions') or bank.get('account_number')):
                return await self.send(chat,'Choose a saved template or write payment instructions before sending details.',[('Choose bank template',f'store:banktemplates:{oid}'),('Write bank instructions',f'ful:bankcustom:{oid}')]+MENU)
        if not data.startswith('store:'): return await self._previous_admin_callback(chat,data,request_key)
        parts=data.split(':');action=parts[1]
        if action=='settings': return await self.content_panel(chat)
        if action=='help': return await self.send(chat,'Use Store content & appearance to edit the welcome message, delivery/refund policy, support contact and welcome sticker/photo. Manage products controls prices and pictures. Prices stay in USD. Customers enter their city and map pin. Bank payments go to support before any account details are sent. The current release has one shared store with no country or vendor selector.')
        if action=='edit':
            if parts[2] not in ('name','welcome','delivery','refund','support'): raise ValueError('Choose a store-content field.')
            return await self.begin(chat,{'flow':'store_text','step':'text','field':parts[2]})
        if action=='form': return await self.store_form(chat,parts[2],parts[3])
        if action=='media': return await self.begin(chat,{'flow':'store_media','step':'media'})
        if action=='clearmedia':
            await self.b.db.execute("DELETE FROM store_media WHERE key='welcome'")
            return await self.content_panel(chat)
        if action=='preview':
            await self.welcome_media(chat,self.b.admin_bot)
            return await self.send(chat,await self.welcome_text(),[('← Store settings','store:settings')])
        if action in ('banktemplates','bankpick','bankrelease'):
            oid=int(parts[2]);o=await self.b.db.fetchrow('SELECT * FROM orders WHERE id=$1',oid)
            if not o or o['method']!='local' or o['status'] not in OPEN: raise ValueError('This bank order is not awaiting payment.')
            if action=='banktemplates':
                rows=await self.b.db.fetch('SELECT * FROM bank_accounts WHERE enabled')
                items=[(r['currency']+' · '+(r['bank_name'] or 'Bank template'),f'store:bankpick:{oid}:{r["currency"]}') for r in rows if r['currency'] in BANK_CODES]
                return await self.send(chat,'Choose private bank details to send. USD order total: '+self.b.format_amount(o['amount'],o['currency'])+'\nAgree any local payment amount with the customer. Templates do not change the order price.',items+[('Write custom instructions',f'ful:bankcustom:{oid}'),('Add bank template','ui:bankcodes:0')]+MENU)
            code=parts[3]
            if code not in BANK_CODES: raise ValueError('Choose a supported private template.')
            row=await self.b.db.fetchrow('SELECT * FROM bank_accounts WHERE currency=$1 AND enabled',code)
            if not row: raise ValueError('This bank template is unavailable.')
            bank={field:row[field] for field in ('instructions','bank_name','account_name','account_number')}
            if action=='bankpick':
                if row['image']: await self.b.admin_bot.call('sendPhoto',chat_id=chat,photo=('bank.png',row['image']))
                return await self.send(chat,f'Send these details to the customer for order #{oid}?\n\n{self.bank_text(bank)}\n\nConfirm any local payment amount in your instructions.',[('✅ Send bank details',f'store:bankrelease:{oid}:{code}'),('← Choose another template',f'store:banktemplates:{oid}')]+MENU)
            await self.release_bank(chat,oid,bank,row['image'])
            return await self.order_detail(chat,oid)
        return await self.menu(chat)
