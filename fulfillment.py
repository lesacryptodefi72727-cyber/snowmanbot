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
COUNTRIES = dict(line.split('|', 1) for line in '''NG|Nigeria
GH|Ghana
KE|Kenya
ZA|South Africa
DZ|Algeria
AO|Angola
BJ|Benin
BW|Botswana
BF|Burkina Faso
BI|Burundi
CV|Cabo Verde
CM|Cameroon
CF|Central African Republic
TD|Chad
KM|Comoros
CG|Congo
CD|DR Congo
CI|Côte d'Ivoire
DJ|Djibouti
EG|Egypt
GQ|Equatorial Guinea
ER|Eritrea
SZ|Eswatini
ET|Ethiopia
GA|Gabon
GM|Gambia
GN|Guinea
GW|Guinea-Bissau
LS|Lesotho
LR|Liberia
LY|Libya
MG|Madagascar
MW|Malawi
ML|Mali
MR|Mauritania
MU|Mauritius
MA|Morocco
MZ|Mozambique
NA|Namibia
NE|Niger
RW|Rwanda
ST|São Tomé and Príncipe
SN|Senegal
SC|Seychelles
SL|Sierra Leone
SO|Somalia
SS|South Sudan
SD|Sudan
TZ|Tanzania
TG|Togo
TN|Tunisia
UG|Uganda
ZM|Zambia
ZW|Zimbabwe
US|United States
GB|United Kingdom
CA|Canada
AE|United Arab Emirates
AU|Australia
DE|Germany
FR|France
ES|Spain
IT|Italy
NL|Netherlands
BE|Belgium
PT|Portugal
TR|Türkiye
IN|India
BR|Brazil
MY|Malaysia
SG|Singapore'''.splitlines())

BACKUP_TABLES = ['users','settings','products','admin_product_drafts','bank_accounts','admin_sessions','vendors',
                 'orders','payments','jobs','order_bank_details','invoice_messages','payment_audit','delivery_live','order_messages']


def quote_usd(price):
    price = Decimal(price)
    discount = Decimal('150') if price > Decimal('1000') else Decimal('0')
    return price, discount, price-discount


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
        self.tz_name = os.getenv('DELIVERY_TIMEZONE', 'Africa/Lagos')
        self.tz = ZoneInfo(self.tz_name)
        self.backup_time = os.getenv('AUTO_BACKUP_UTC', '02:00').strip()
        if self.backup_time.lower() not in ('off','disabled',''):
            if len(self.backup_time)!=5: raise ValueError('AUTO_BACKUP_UTC must be HH:MM in UTC or off')
            datetime.strptime(self.backup_time, '%H:%M')
        self.last_backup_minute = None

    async def menu(self, chat):
        return await self.send(chat, 'SnowmanBot Admin\nChoose what to manage.', [
            ('➕ Add product','admin:add'), ('📦 Manage products','ui:products:0'),
            ('🧾 Orders, payments & delivery','ui:orders:0'), ('🏦 Bank accounts & rates','ui:banks'),
            ('🪙 Crypto wallets','ui:wallets'), ('🏪 Vendors','ful:vendors:0'),
            ('🔔 Daily reminder','ui:reminder'), ('📊 Statistics','ui:stats'),
            ('📨 Notification queue','ui:jobs'), ('💾 Export backup','ful:backupask'),
            ('🆔 My Telegram ID','admin:myid'), ('❔ Help','ful:help')])

    async def home(self, chat):
        code = await self.b.db.fetchval('SELECT country FROM users WHERE id=$1', chat)
        region = COUNTRIES.get(code, 'Choose your country')
        await self.b.state(chat, {})
        await self.b.main_bot.send(chat, 'Menu opened.', {'remove_keyboard':True})
        return await self.b.main_bot.send(chat, f'SnowmanBot · {region}\nBrowse products without placing an order. Physical products use location-drop delivery at an agreed map point, with a requested delivery time.\n\nPhysical orders above $1,000 receive $150 off.', self.b.home_buttons())

    async def countries(self, chat, page=0, checkout=False, product_id=None):
        codes=list(COUNTRIES)
        page=max(0,min(page,(len(codes)-1)//8))
        if product_id is not None:
            prefix=f'ful:pcountry:{product_id}'
            items=[('🌍 All country profiles',prefix+':ALL')]
            items += [(flag(c)+' '+COUNTRIES[c],prefix+':'+c) for c in codes[page*8:page*8+8]]
            if page: items.append(('← Previous',f'ful:pcountries:{product_id}:{page-1}'))
            if (page+1)*8<len(codes): items.append(('Next →',f'ful:pcountries:{product_id}:{page+1}'))
            return await self.send(chat,'Choose where this product is listed.',items+MENU)
        prefix='dropcountry' if checkout else 'countryset'
        items=[(flag(c)+' '+COUNTRIES[c],prefix+':'+c) for c in codes[page*8:page*8+8]]
        pager='dropcountries' if checkout else 'countries'
        if page: items.append(('← Previous',f'{pager}:{page-1}'))
        if (page+1)*8<len(codes): items.append(('Next →',f'{pager}:{page+1}'))
        await self.b.main_bot.send(chat,'Choose your country profile. Delivery availability is confirmed by the store.',self.keys(items+[('✖ Cancel','home')]))

    async def catalog(self, chat, offset=0):
        country=await self.b.db.fetchval('SELECT country FROM users WHERE id=$1',chat)
        rows=await self.b.db.fetch("SELECT * FROM products WHERE published AND (country='' OR country=$1) ORDER BY id DESC LIMIT 5 OFFSET $2",country or '',offset)
        if not rows:
            return await self.b.main_bot.send(chat,'No products are listed for this profile yet.',self.keys([('Choose country','countries:0'),('← Store','home')]))
        for p in rows: await self.b.product_card(chat,p)
        items=[('🌍 Change country','countries:0'),('← Store','home')]
        if len(rows)==5: items.insert(0,('More products',f'catalog:{offset+5}'))
        await self.b.main_bot.send(chat,'Browse products or choose a product to order.',self.keys(items))

    async def start_checkout(self, chat, product_id, method, request_key, currency=None):
        product=await self.b.db.fetchrow('SELECT * FROM products WHERE id=$1 AND published',product_id)
        if not product: raise ValueError('Product unavailable.')
        if product['kind']=='digital':
            return await self.b.begin_checkout(chat,product_id,method,request_key,currency,delivery_ready=True)
        if method=='local':
            if currency not in await self.b.local_methods(): raise ValueError('Choose an available bank-transfer currency.')
        elif method not in self.b.enabled_crypto(): raise ValueError('This crypto option is unavailable.')
        country=await self.b.db.fetchval('SELECT country FROM users WHERE id=$1',chat)
        token=secrets.token_hex(5)
        data=dict(drop_checkout=True,token=token,pid=product_id,method=method,currency=currency,request_key=request_key,stage='pin',country=country or '')
        if product['country'] and country and product['country']!=country: raise ValueError('This product is not listed for your selected country.')
        if method=='local':
            await self.b.alert_admins(self.b.db,'bank-choice:'+request_key,
                f'Bank transfer selected\nCustomer: {chat}\nProduct: {product["name"]}\nCurrency: {currency}\nThe customer is choosing a drop-off point and delivery time.',
                buttons=self.keys([('Message customer',f'ful:chatuser:{chat}')]))
        if not country: data['stage']='country'
        await self.b.state(chat,data)
        if not country: return await self.countries(chat,checkout=True)
        return await self.pin_prompt(chat,data)

    async def pin_prompt(self, chat, data):
        await self.b.main_bot.send(chat,'📍 Location-drop delivery\nSend your chosen drop-off point on the map so we can follow it and make the drop there. This is physical delivery to an agreed location, not home delivery.\n\nUse Telegram’s attachment → Location to choose a map pin. Share your current location only if that is the intended drop-off point.',
            {'keyboard':[[{'text':'📍 Share current location','request_location':True}],[{'text':'✖ Cancel'}]],'resize_keyboard':True,'one_time_keyboard':True})
        saved=unpack(await self.b.db.fetchval('SELECT dropoff FROM users WHERE id=$1',chat))
        if saved:
            await self.b.main_bot.send(chat,'Or use your saved drop-off point.',self.keys([('Use saved map point',f'drop:{data["token"]}:savedpin'),('✖ Cancel','home')]))

    async def schedule_prompt(self, chat, data):
        await self.b.main_bot.send(chat,'Map point received.',{'remove_keyboard':True})
        return await self.b.main_bot.send(chat,f'When would you like the physical delivery?\nRequested times use {self.tz_name}. The store confirms availability and sends the final ETA.',
            self.keys([('As soon as available',f'drop:{data["token"]}:asap'),('Choose a delivery time',f'drop:{data["token"]}:days'),('✖ Cancel','home')]))

    async def dates(self, chat, data):
        today=self.b.now().astimezone(self.tz).date()
        items=[((today+timedelta(days=i)).strftime('%a %d %b'),f'drop:{data["token"]}:day:{(today+timedelta(days=i)).isoformat()}') for i in range(8)]
        await self.b.main_bot.send(chat,f'Choose a delivery date ({self.tz_name}).',self.keys(items+[('✖ Cancel','home')]))

    async def times(self, chat, data, day):
        try: date=datetime.strptime(day,'%Y-%m-%d').date()
        except ValueError: raise ValueError('Choose a date from the buttons.')
        current=self.b.now().astimezone(self.tz)
        if not current.date()<=date<=current.date()+timedelta(days=7): raise ValueError('Choose a date within the next 7 days.')
        items=[]
        for hour in range(8,23):
            slot=datetime.combine(date,datetime.min.time(),tzinfo=self.tz).replace(hour=hour)
            if slot>current+timedelta(minutes=15): items.append((slot.strftime('%H:%M'),f'drop:{data["token"]}:time:{day}:{hour}'))
        await self.b.main_bot.send(chat,f'Choose a requested time on {day} ({self.tz_name}).',self.keys(items+[('← Choose another day',f'drop:{data["token"]}:days'),('✖ Cancel','home')]))

    async def checkout_review(self, chat, data):
        product=await self.b.db.fetchrow('SELECT * FROM products WHERE id=$1 AND published',data['pid'])
        if not product: raise ValueError('Product unavailable.')
        subtotal,discount,total=quote_usd(product['price'])
        when=data['schedule']['label']
        await self.b.main_bot.call('sendLocation',chat_id=chat,**data['pin'])
        await self.b.main_bot.send(chat,f'Review your order\n{product["name"]}\nCountry: {COUNTRIES[data["country"]]}\nPhysical location drop\nRequested time: {when}\nSubtotal: {self.b.format_amount(subtotal,"USD")}\nDiscount: {self.b.format_amount(discount,"USD")}\nTotal: {self.b.format_amount(total,"USD")}\n\nThe store confirms the delivery ETA after reviewing the order.',self.keys([
            ('✅ Confirm checkout',f'drop:{data["token"]}:confirm'),('📍 Change map point',f'drop:{data["token"]}:pin'),('🕒 Change delivery time',f'drop:{data["token"]}:reschedule'),('✖ Cancel','home')]))

    async def drop_action(self, chat, parts):
        user=await self.b.db.fetchrow('SELECT * FROM users WHERE id=$1',chat)
        data=unpack(user['state'])
        if not data.get('drop_checkout') or data.get('token')!=parts[1]: raise ValueError('This checkout form is closed. Choose the product again.')
        action=parts[2]
        if action=='pin':
            data['stage']='pin';await self.b.state(chat,data);return await self.pin_prompt(chat,data)
        if action=='savedpin' and data['stage']=='pin':
            data['pin']=location_pin(unpack(user['dropoff']));data['stage']='schedule'
            await self.b.state(chat,data);return await self.schedule_prompt(chat,data)
        if action=='reschedule' and data.get('pin'):
            data['stage']='schedule';await self.b.state(chat,data);return await self.schedule_prompt(chat,data)
        if action in ('days','day','time','asap') and data['stage']=='schedule':
            if action=='days': return await self.dates(chat,data)
            if action=='day': return await self.times(chat,data,parts[3])
            if action=='asap': data['schedule']={'mode':'asap','label':'As soon as available; store confirms ETA','timezone':self.tz_name}
            else:
                try: slot=datetime.strptime(parts[3],'%Y-%m-%d').replace(hour=int(parts[4]),tzinfo=self.tz)
                except (ValueError,IndexError): raise ValueError('Choose a valid delivery slot.')
                if not 8<=slot.hour<=22 or not self.b.now()+timedelta(minutes=15)<slot<=self.b.now()+timedelta(days=8): raise ValueError('That slot is no longer available. Choose another time.')
                data['schedule']={'mode':'scheduled','at':slot.astimezone(timezone.utc).isoformat(),'timezone':self.tz_name,'label':slot.strftime('%a %d %b %Y %H:%M')+' '+self.tz_name+' (requested)'}
            data['stage']='review';await self.b.state(chat,data);return await self.checkout_review(chat,data)
        if action=='confirm' and data['stage']=='review':
            p=await self.b.db.fetchrow('SELECT country FROM products WHERE id=$1 AND published',data['pid'])
            if not p or (p['country'] and p['country']!=data['country']): raise ValueError('This product is unavailable for the selected country.')
            if data['schedule'].get('at') and datetime.fromisoformat(data['schedule']['at'])<=self.b.now(): raise ValueError('Your chosen time has passed. Choose a new delivery time.')
            pin=location_pin(data['pin'])
            await self.b.db.execute('UPDATE users SET country=$2,dropoff=$3::jsonb,delivery_schedule=$4::jsonb,shipping=$5 WHERE id=$1',chat,data['country'],json.dumps(pin),json.dumps(data['schedule']),f"Location drop: {pin['latitude']}, {pin['longitude']} | {data['schedule']['label']}")
            await self.b.begin_checkout(chat,data['pid'],data['method'],'drop:'+data['request_key'],data.get('currency'),delivery_ready=True)
            await self.b.state(chat,{})
            return
        raise ValueError('Use the buttons on the latest checkout prompt.')

    async def snapshot_delivery(self, conn, order, product, user):
        if product['kind']!='physical': return order
        pin=location_pin(unpack(user['dropoff']))
        schedule=unpack(user['delivery_schedule'])
        if not schedule.get('label'): raise ValueError('Choose your delivery time first.')
        subtotal,discount,total=quote_usd(product['price'])
        order=await conn.fetchrow('UPDATE orders SET country=$2,dropoff=$3::jsonb,delivery_schedule=$4::jsonb,subtotal_usd=$5,discount_usd=$6 WHERE id=$1 RETURNING *',order['id'],user['country'],json.dumps(pin),json.dumps(schedule),subtotal,discount)
        await self.b.alert_admins(conn,f'new-drop:{order["id"]}',f'New location-drop order #{order["id"]}\nCustomer: {order["user_id"]}\nProduct: {order["name"]}\nRequested time: {schedule["label"]}\nPayment: {order["method"]}\nReview availability, payment and delivery ETA.',buttons=self.keys([('Open order',f'ui:order:{order["id"]}')]))
        return order

    def delivery_summary(self, o):
        if o['kind']!='physical': return ''
        schedule=unpack(o.get('delivery_schedule'))
        text='\n\n📍 Physical location-drop delivery'
        if o.get('country'): text+='\nCountry: '+COUNTRIES.get(o['country'],o['country'])
        if schedule.get('label'): text+='\nRequested: '+schedule['label']
        text+='\nETA: '+(o.get('eta') or 'Awaiting store confirmation')
        if o.get('subtotal_usd') is not None:
            text+=f'\nSubtotal: {self.b.format_amount(o["subtotal_usd"],"USD")}\nDiscount: {self.b.format_amount(o.get("discount_usd",0),"USD")}'
        return text

    def order_text(self, order):
        o=dict(order)
        if o['method']=='local' and o['status'] in OPEN:
            title=f'Order #{o["id"]} · {o["name"]}\nAmount: {self.b.format_amount(o["amount"],o["currency"])}\nReference: SNOW-{o["id"]}'
            if o.get('bank_released'):
                title+='\n\nPayment instructions from the store:\n'+o['note']+'\n\nAfter paying, tap Upload receipt.'
            else:
                title+='\n\n🏦 Bank transfer requested\nThe admin has been notified and will contact you here to arrange the bank transfer. Wait for payment instructions before sending money.'
            if o['status']=='review': title+='\nYour receipt is awaiting review.'
            return title+self.delivery_summary(o)
        return super().order_text(o)+self.delivery_summary(o)

    def order_buttons(self, order, bank=None):
        o=dict(order);buttons=super().order_buttons(o,bank if o.get('bank_released') else None)
        extra=[]
        if o['method']=='local' and o['status'] in OPEN:
            extra.append([{'text':'💬 Message store about payment','callback_data':f'orderchat:{o["id"]}'}])
        if unpack(o.get('dropoff')):
            extra.append([{'text':'📍 View my drop-off point','callback_data':f'droppoint:{o["id"]}'}])
        buttons['inline_keyboard']=extra+buttons['inline_keyboard']
        return buttons

    async def extra_order_buttons(self, o):
        items=[('💬 Message customer',f'ful:chatorder:{o["id"]}')]
        if o['method']=='local' and o['status'] in OPEN:
            items += [('🏦 Send saved bank details',f'ful:bankask:{o["id"]}'),('✏️ Write bank instructions',f'ful:bankcustom:{o["id"]}')]
        if o['kind']=='physical':
            items += [('📍 View drop-off point',f'ful:drop:{o["id"]}')]
            if o['status'] in ('paid','fulfilled'):
                items += [('🕒 Set / update ETA',f'ful:eta:{o["id"]}'),('📡 Share delivery location',f'ful:track:{o["id"]}'),('⏹ Stop live tracking',f'ful:stoptrack:{o["id"]}')]
        return items

    async def extra_product_buttons(self, product):
        return [('🌍 Set country profile',f'ful:pcountries:{product["id"]}:0')]

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
        if command=='/address':
            await self.customer_callback(message['chat']['id'],'deliverydetails');return True
        return False

    async def customer_input(self, message, pending):
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
            if not 1<=len(text)<=2000: raise ValueError('Send your message as text (up to 2,000 characters). Use Upload receipt for a payment screenshot.')
            request=f'customer-message:{chat}:{message.get("message_id",0)}'
            async with self.b.db.acquire() as conn:
                async with conn.transaction():
                    sent=await conn.fetchval("INSERT INTO order_messages(request_key,order_id,customer_id,direction,body) VALUES($1,$2,$3,'customer',$4) ON CONFLICT DO NOTHING RETURNING id",request,oid,chat,text)
                    if sent: await self.b.alert_admins(conn,request,f'Customer message'+(f' · order #{oid}' if oid else '')+f'\nCustomer: {chat}\n\n{text}',buttons=self.keys([('Reply to customer',f'ful:chatorder:{oid}' if oid else f'ful:chatuser:{chat}')]))
            await self.b.state(chat,{})
            await self.b.main_bot.send(chat,'Your message was sent to the store.',self.keys(([('View order',f'receipt:{oid}')] if oid else [])+[('← Store','home')]));return True
        return await super().customer_input(message,pending)

    async def customer_callback(self, chat, data):
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

    async def prompt(self, chat, data, token):
        flow=data['flow'];step=data['step'];key=lambda value:f'ful:form:{token}:{value}'
        if flow=='manual_bank':
            prompts={'instructions':'Type the bank payment instructions to send this customer. This will only be sent after you confirm (up to 1,800 characters).','number':'Send the exact account / IBAN / mobile-money number for the Copy button (up to 200 characters).','image':'Attach a bank-details screenshot as a photo, or tap Skip.'}
            if step=='review':
                return await self.send(chat,f'Send these instructions for order #{data["oid"]}?\n\n{data["instructions"]}\nCopyable account: {data["number"]}',[('✅ Send to customer',key('send'))]+CANCEL)
            return await self.send(chat,prompts[step],([('Skip image',key('skip'))] if step=='image' else [])+CANCEL)
        prompts={'message':'Type your message to the customer (up to 2,000 characters).','eta':'Send the confirmed delivery time / ETA, including the date and timezone. This will be sent to the customer.','tracking':'Attach the delivery location. To share genuine live movement, use Telegram attachment → Location → Share Live Location. A static pin sends a one-time map update.'}
        if flow in prompts: return await self.send(chat,prompts[flow],CANCEL)
        if flow=='vendor':
            prompts={'name':'Send the vendor’s name.','country':'Send the country name or two-letter code, or tap All countries.','contact':'Send the public Telegram username (@username) or HTTPS contact link.','description':'Send a short vendor description (up to 1,000 characters).'}
            if step=='review': return await self.send(chat,f'Review vendor\n{data["name"]}\n{COUNTRIES.get(data["country"],"All countries")}\n{data["contact"]}\n{data["description"]}\nInitial status: Upcoming',[('Save upcoming vendor',key('savevendor'))]+CANCEL)
            return await self.send(chat,prompts[step],([('All countries',key('allcountries'))] if step=='country' else [])+CANCEL)
        return await super().prompt(chat,data,token)

    async def admin_message(self, message, request_key):
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
            if not 1<=len(text)<=500: raise ValueError('Send the confirmed ETA in 1–500 characters.')
            async with self.b.db.acquire() as conn:
                async with conn.transaction():
                    order=await conn.fetchrow("UPDATE orders SET eta=$2 WHERE id=$1 AND kind='physical' AND status IN ('paid','fulfilled') RETURNING *",data['oid'],text)
                    if not order: raise ValueError('Only paid physical orders can have a delivery ETA confirmed.')
                    await self.b.queue(conn,f'eta:{request_key}','main',order['user_id'],f'Delivery update for order #{order["id"]}\nConfirmed ETA: {text}\nPhysical location-drop delivery.',buttons=self.keys([('View order',f'receipt:{order["id"]}'),('View drop-off point',f'droppoint:{order["id"]}')]))
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
        await self.b.main_bot.send(o['user_id'],f'Order #{oid}\n{kind}\nETA: {o["eta"] or "Awaiting store confirmation"}',self.keys([('View order',f'receipt:{oid}')]))

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

    async def admin_callback(self, chat, data, request_key):
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
