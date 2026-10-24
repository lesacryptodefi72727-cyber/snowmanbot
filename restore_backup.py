"""Inspect a SnowmanBot backup or restore it into an empty PostgreSQL database.

Usage: python restore_backup.py BACKUP.zip --inspect
       DATABASE_URL=... python restore_backup.py BACKUP.zip --restore-empty
No existing data is deleted. Keep the bot service stopped during restoration.
"""
import argparse
import asyncio
import base64
import json
import os
import zipfile
from datetime import datetime
from decimal import Decimal

import asyncpg
from bot import SCHEMA, UI_SCHEMA, FULFILLMENT_SCHEMA
from fulfillment import BACKUP_TABLES


def decode_field(value, datatype):
    if value is None: return None
    if datatype=='bytea':
        if not isinstance(value,dict) or set(value)!={'$bytes'}: raise ValueError('Invalid binary backup field')
        return base64.b64decode(value['$bytes'],validate=True)
    if datatype=='numeric': return Decimal(value['$decimal'] if isinstance(value,dict) else str(value))
    if datatype.startswith('timestamp'):
        return datetime.fromisoformat(value['$datetime'] if isinstance(value,dict) else value)
    if datatype in ('json','jsonb'): return value if isinstance(value,str) else json.dumps(value)
    if datatype in ('bigint','integer','smallint'): return int(value)
    if datatype in ('double precision','real'): return float(value)
    return value


async def restore(path, dsn):
    with zipfile.ZipFile(path) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        if manifest.get('format')!='snowman-backup-v1': raise ValueError('Unsupported backup format')
        names=[item['name'] for item in manifest['tables']]
        if len(names)!=len(set(names)) or any(n not in BACKUP_TABLES for n in names): raise ValueError('Unexpected tables in backup')
        if any(i.file_size>256*1024*1024 for i in archive.infolist()) or sum(i.file_size for i in archive.infolist())>512*1024*1024:
            raise ValueError('Backup exceeds the restore size limit')
        connection=await asyncpg.connect(dsn)
        try:
            async with connection.transaction():
                await connection.execute('SELECT pg_advisory_xact_lock(9173003)')
                await connection.execute(SCHEMA+UI_SCHEMA+FULFILLMENT_SCHEMA)
                for table in BACKUP_TABLES:
                    if await connection.fetchval(f'SELECT EXISTS(SELECT 1 FROM "{table}" LIMIT 1)'):
                        raise ValueError('Destination database is not empty. Restore was cancelled; no existing data was deleted.')
                manifest_rows={i['name']:i['rows'] for i in manifest['tables']}
                for table in BACKUP_TABLES:
                    if table not in names: continue
                    metadata=await connection.fetch("SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position",table)
                    columns={r['column_name']:r['data_type'] for r in metadata}
                    count=0
                    with archive.open('data/'+table+'.jsonl') as source:
                        for raw in source:
                            row=json.loads(raw)
                            if not row or any(k not in columns for k in row): raise ValueError('Unexpected columns in '+table)
                            keys=list(row)
                            placeholders=','.join('$'+str(i+1) for i in range(len(keys)))
                            fieldnames=','.join('"'+k+'"' for k in keys)
                            await connection.execute(f'INSERT INTO "{table}"({fieldnames}) VALUES({placeholders})',*(decode_field(row[k],columns[k]) for k in keys))
                            count+=1
                    if count!=manifest_rows[table]: raise ValueError('Row count mismatch in '+table)
                    if 'id' in columns:
                        sequence=await connection.fetchval("SELECT pg_get_serial_sequence($1,'id')",table)
                        if sequence:
                            last=await connection.fetchval(f'SELECT max(id) FROM "{table}"')
                            await connection.execute('SELECT setval($1::regclass,$2,$3)',sequence,max(1,last or 1),last is not None)
                # A replacement bot has different message IDs and must wait for users to start it.
                await connection.execute('DELETE FROM admin_sessions')
                await connection.execute("UPDATE users SET state='{}'::jsonb")
                await connection.execute('UPDATE delivery_live SET active=false')
                await connection.execute('UPDATE invoice_messages SET finished=true')
                await connection.execute("UPDATE jobs SET status='uncertain' WHERE status IN ('queued','sending')")
                await connection.execute("DELETE FROM settings WHERE key LIKE 'offset:%' OR key LIKE 'identity:%'")
            return manifest
        finally: await connection.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backup')
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--inspect',action='store_true')
    group.add_argument('--restore-empty',action='store_true')
    args=parser.parse_args()
    if args.inspect:
        with zipfile.ZipFile(args.backup) as archive:
            print(json.dumps(json.loads(archive.read('manifest.json')),indent=2))
        return
    dsn=os.getenv('DATABASE_URL','').strip()
    if not dsn: raise SystemExit('Set DATABASE_URL for the EMPTY destination database.')
    try: result=asyncio.run(restore(args.backup,dsn))
    except Exception as error: raise SystemExit('Restore stopped: '+type(error).__name__+'. Check the backup and destination; keep credentials private.')
    print('Restored backup from',result['created'])
    print('Old queued notifications are paused for review. Configure hosting variables, start the service and test. Users must start a replacement Telegram bot before it can contact them.')


if __name__=='__main__': main()
