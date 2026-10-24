                                            "24h after expiry. "
                                            "Investigate the "
                                            "transaction; do not "
                                            "request another "
                                            "payment before "
                                            "checking."
                                        ),
                                    )

                                    await queue(
                                        conn,
                                        (
                                            "crypto-review-user:"
                                            f"{order['id']}"
                                        ),
                                        "main",
                                        order["user_id"],
                                        (
                                            f"Order #{order['id']} "
                                            "needs payment review. "
                                            "/support"
                                        ),
                                    )

                except Exception as error:
                    LOG.warning(
                        "Crypto order %s: %s",
                        order["id"],
                        type(error).__name__,
                    )

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            raise

        except Exception as error:
            LOG.warning(
                "Payment worker: %s",
                type(error).__name__,
            )

        await asyncio.sleep(60)


async def keep_lock(connection):
    while True:
        await connection.fetchval("SELECT 1")
        await asyncio.sleep(10)


async def start_http(runtime, port=None):
    async def health(request):
        if not runtime["ready"]:
            return web.json_response({"status": runtime["role"]}, status=503)
        try:
            await asyncio.wait_for(db.fetchval("SELECT 1"), timeout=2)
        except Exception:
            return web.json_response({"status": "database_unavailable"}, status=503)
        return web.json_response({"status": runtime["role"], "version": VERSION})
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    selected_port = int(os.getenv("PORT", "10000")) if port is None else port
    await web.TCPSite(runner, "0.0.0.0", selected_port).start()
    LOG.info("HTTP health endpoint bound to 0.0.0.0:%s", selected_port)
    return runner


async def main():
    global db, main_bot, admin_bot, crypto
    runtime = {"ready": False, "role": "starting"}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    runner = await start_http(runtime)
    db = None
    tasks = []
    try:
        main_token = os.environ["MAIN_BOT_TOKEN"].strip()
        admin_token = os.environ["ADMIN_BOT_TOKEN"].strip()
        if main_token == admin_token:
            raise RuntimeError("MAIN_BOT_TOKEN and ADMIN_BOT_TOKEN must be for two different bots")
        dsn = os.getenv("DATABASE_URL", "").strip()
        parsed = urlparse(dsn)
        if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname or parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            raise RuntimeError("Set DATABASE_URL to your hosted PostgreSQL connection URL, not localhost or an empty value")
        if not ADMINS:
            LOG.warning("No ADMIN_IDS configured. The admin bot will show your user ID but keep management locked.")
        db = await asyncpg.create_pool(dsn, min_size=2, max_size=8, command_timeout=30)
        async with db.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(9173003)")
                await connection.execute(SCHEMA)
        for env_name, prefix in [("LOCAL_PAYMENTS_JSON", "method:"), ("FX_RATES_JSON", "rate:")]:
            for code, value in json.loads(os.getenv(env_name, "{}")).items():
                if code not in CURRENCIES:
                    raise ValueError("Unsupported currency in " + env_name)
                value = str(positive(str(value))) if prefix == "rate:" else str(value)
                if len(value)>1800:
                    raise ValueError("Payment instructions too long")
                await db.execute("INSERT INTO settings VALUES($1,$2) ON CONFLICT DO NOTHING", prefix+code, value)
        async with aiohttp.ClientSession() as session:
            main_bot = Telegram(main_token, session)
            admin_bot = Telegram(admin_token, session)
            crypto = Crypto(session)
            customer_info, admin_info = await asyncio.gather(main_bot.call("getMe"), admin_bot.call("getMe"))
            if customer_info["id"] == admin_info["id"]:
                raise RuntimeError("Both tokens identify the same Telegram bot; use a separate admin bot")
            BOT_IDS.update(main=customer_info["id"], admin=admin_info["id"])
            LOG.info("Customer bot: @%s | Admin bot: @%s | Configured admin users: %s", customer_info.get("username"), admin_info.get("username"), len(ADMINS))
            # A verified standby is HTTP-ready before taking the polling lock.
            # This lets Render retire the previous instance during a rolling deploy.
            runtime.update(ready=True, role="standby")
            async with db.acquire() as leader:
                while not await leader.fetchval("SELECT pg_try_advisory_lock(9173001)"):
                    if stop.is_set():
                        return
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        pass
                if stop.is_set():
                    return
                runtime["role"] = "running"
                LOG.info("SnowmanBot started | %s", VERSION)
                tasks = [asyncio.create_task(coro) for coro in (poll(False), poll(True), worker(), payment_worker(), keep_lock(leader))]
                shutdown = asyncio.create_task(stop.wait())
                tasks.append(shutdown)
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    if shutdown not in done:
                        for task in done:
                            task.result()
                        raise RuntimeError("A SnowmanBot background task stopped unexpectedly")
                finally:
                    runtime.update(ready=False, role="stopping")
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        runtime.update(ready=False, role="stopping")
        if db is not None:
            try:
                await asyncio.wait_for(db.close(), timeout=5)
            except Exception:
                db.terminate()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

