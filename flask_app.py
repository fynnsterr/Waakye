from abc import ABC, abstractmethod
from flask import Flask, render_template, request, jsonify
from colorama import Fore, init
import os
import phonenumbers
from phonenumbers import carrier as ph_carrier, is_valid_number, NumberParseException
import json
import uuid
import sqlite3
import datetime
import sys
import threading
import queue
import time
from datetime import date
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError
import asyncio

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

init(autoreset=True)
app = Flask(__name__, template_folder=os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_IDS = os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if os.getenv("TELEGRAM_CHAT_IDS") else []
ADMIN_KEY = os.getenv("ADMIN_KEY", "changeme")
telegram_bot = None
telegram_app = None

if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS:
    try:
        telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
        TELEGRAM_CHAT_IDS = [chat_id.strip() for chat_id in TELEGRAM_CHAT_IDS if chat_id.strip()]
        print(Fore.GREEN + f"[Waakye] Telegram bot initialized. Notifications will be sent to: {TELEGRAM_CHAT_IDS}")
    except Exception as e:
        print(Fore.RED + f"[Waakye] Failed to initialize Telegram bot: {e}")

_MENU_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "menu.json")
with open(_MENU_PATH, "r", encoding="utf-8") as _f:
    MENU = json.load(_f)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "waakye_data.db")
WEEKLY_ORDER_LIMIT = 5

def get_db():
    db = sqlite3.connect(DB_PATH, timeout=10.0)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS customers (
                phone TEXT PRIMARY KEY,
                name  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id              TEXT PRIMARY KEY,
                phone                 TEXT NOT NULL,
                hostel                TEXT NOT NULL,
                room_notes            TEXT,
                delivery_date         TEXT NOT NULL,
                items_summary         TEXT NOT NULL,
                subtotal              REAL NOT NULL,
                total                 REAL NOT NULL,
                timestamp             TEXT NOT NULL,
                status                TEXT NOT NULL DEFAULT 'pending',
                week_number           INTEGER NOT NULL DEFAULT 0,
                year                  INTEGER NOT NULL DEFAULT 0,
                admin_confirms        TEXT NOT NULL DEFAULT '',
                user_confirmed_delivered INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (phone) REFERENCES customers(phone)
            );

            CREATE TABLE IF NOT EXISTS telegram_message_ids (
                message_id INTEGER,
                chat_id TEXT,
                processed_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (message_id, chat_id)
            );
        """)
        existing = {row[1] for row in db.execute("PRAGMA table_info(orders)").fetchall()}
        migrations = {
            "status":                    "ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'",
            "week_number":               "ALTER TABLE orders ADD COLUMN week_number INTEGER NOT NULL DEFAULT 0",
            "year":                      "ALTER TABLE orders ADD COLUMN year INTEGER NOT NULL DEFAULT 0",
            "admin_confirms":            "ALTER TABLE orders ADD COLUMN admin_confirms TEXT NOT NULL DEFAULT ''",
            "user_confirmed_delivered":  "ALTER TABLE orders ADD COLUMN user_confirmed_delivered INTEGER NOT NULL DEFAULT 0",
        }
        for col, sql in migrations.items():
            if col not in existing:
                db.execute(sql)

        tg_table_exists = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_message_ids'"
        ).fetchone()
        if tg_table_exists:
            tg_columns = {row[1] for row in db.execute("PRAGMA table_info(telegram_message_ids)").fetchall()}
            if "chat_id" not in tg_columns:
                db.execute("DROP TABLE telegram_message_ids")
                db.execute("""
                    CREATE TABLE telegram_message_ids (
                        message_id INTEGER,
                        chat_id TEXT,
                        processed_at TEXT NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (message_id, chat_id)
                    )
                """)

# ─── OOP: Menu, Package, Condiment, Customer, Order ─────────────
class MenuItem(ABC):
    def __init__(self, name, price):
        self._name = name
        self._price = price
    @abstractmethod
    def get_price(self): pass
    @abstractmethod
    def get_description(self): pass
    def get_name(self): return self._name
    def __str__(self): return f"{self._name} (₵{self._price:.2f})"

class Package(MenuItem):
    def __init__(self, pkg_type):
        packages = MENU["packages"]
        if pkg_type not in packages:
            raise ValueError(f"Unknown package type: {pkg_type}")
        data = packages[pkg_type]
        super().__init__(data["name"], data["price"])
        self._includes = data["includes"]
    def get_price(self): return self._price
    def get_description(self): return self._includes

class Condiment(MenuItem):
    def __init__(self, cond_key):
        condiments = MENU["condiments"]
        if cond_key not in condiments:
            raise ValueError(f"Unknown condiment: {cond_key}")
        data = condiments[cond_key]
        super().__init__(data["name"], data["price"])
    def get_price(self): return self._price
    def get_description(self): return f"Add-on condiment: {self._name}"

class Customer:
    def __init__(self, name, phone):
        self._name = name
        self._phone = phone
    def get_name(self): return self._name
    def get_phone(self): return self._phone

class Order:
    def __init__(self, customer, items, hostel, room_notes, delivery_date):
        self._order_id = uuid.uuid4().hex[:6].upper()
        self._customer = customer
        self._items = items
        self._hostel = hostel
        self._room_notes = room_notes
        self._delivery_date = delivery_date
        self._timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    def get_order_id(self): return self._order_id
    def get_delivery_date(self): return self._delivery_date
    def get_hostel(self): return self._hostel
    def subtotal(self):
        return sum(i.get_price() for i in self._items)
    def total(self):
        return self.subtotal() + MENU["delivery_fee"]
    def items_summary(self):
        return "\x1f".join(str(i) for i in self._items)

def get_iso_week(dt=None):
    if dt is None:
        dt = datetime.date.today()
    iso = dt.isocalendar()
    return iso[0], iso[1]

def count_orders_this_week(phone):
    year, week = get_iso_week()
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM orders WHERE phone=? AND year=? AND week_number=?",
            (phone, year, week)
        ).fetchone()
    return row[0]

def create_menu_item(item_type, item_key):
    match item_type:
        case "package":   return Package(item_key)
        case "condiment": return Condiment(item_key)
        case _:           raise ValueError(f"Unknown item type: {item_type}")

# ─── Telegram Update Queue (Synchronous Worker) ──────────────────
telegram_queue = queue.Queue()

def process_update_sync(update):
    """Synchronous handler – runs in the background worker thread."""
    import asyncio

    def send(chat_id, text):
        asyncio.run(telegram_bot.send_message(chat_id=chat_id, text=text))

    try:
        print(Fore.CYAN + "[Waakye] Processing update in background thread")
        if not update.message or not update.message.text:
            return

        chat_id = str(update.effective_chat.id)
        print(Fore.CYAN + f"[Waakye] Chat ID: {chat_id}")

        if chat_id not in TELEGRAM_CHAT_IDS:
            send(chat_id, "You are not authorized to use this bot.")
            return

        text = update.message.text.strip().upper()
        parts = text.split()
        print(Fore.CYAN + f"[Waakye] Command: {parts}")

        if len(parts) < 2:
            send(chat_id, "Usage: CONFIRM <order_id>, REJECT <order_id>, or DELIVERED <order_id>")
            return

        command = parts[0]
        order_id = parts[1]

        if command == "CONFIRM":
            with get_db() as db:
                row = db.execute(
                    "SELECT status, admin_confirms FROM orders WHERE order_id=?",
                    (order_id,)
                ).fetchone()
                if not row:
                    send(chat_id, f"❌ Order #{order_id} not found")
                    return
                if row["status"] not in ("pending",):
                    send(chat_id, f"Order is already {row['status']}")
                    return
                confirmed_ids = set(filter(None, row["admin_confirms"].split(",")))
                confirmed_ids.add(chat_id)
                db.execute(
                    "UPDATE orders SET admin_confirms=?, status='confirmed' WHERE order_id=?",
                    (",".join(confirmed_ids), order_id)
                )
            send(chat_id, f"✅ Order #{order_id} CONFIRMED")

        elif command == "REJECT":
            with get_db() as db:
                row = db.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()
                if not row:
                    send(chat_id, f"❌ Order #{order_id} not found")
                    return
                if row["status"] not in ("pending",):
                    send(chat_id, f"Order is already {row['status']}")
                    return
                db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))
            send(chat_id, f"❌ Order #{order_id} REJECTED and removed")

        elif command == "DELIVERED":
            with get_db() as db:
                row = db.execute(
                    "SELECT status, user_confirmed_delivered FROM orders WHERE order_id=?",
                    (order_id,)
                ).fetchone()
                if not row:
                    send(chat_id, f"❌ Order #{order_id} not found")
                    return
                if row["status"] != "confirmed":
                    send(chat_id, f"Order is currently '{row['status']}', not ready for delivery confirmation")
                    return
                if row["user_confirmed_delivered"] == 1:
                    db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))
                    send(chat_id, f"🎉 Order #{order_id} DELIVERED & CLOSED\nBoth parties confirmed. Order removed.")
                else:
                    db.execute("UPDATE orders SET status='admin_delivered' WHERE order_id=?", (order_id,))
                    send(chat_id, "Delivery recorded. Waiting for customer to confirm on the tracking page.")
        else:
            send(chat_id, "Unknown command. Use: CONFIRM, REJECT, or DELIVERED")

    except Exception as e:
        print(Fore.RED + f"[Waakye] Process update error: {e}")
        import traceback
        traceback.print_exc()



def telegram_worker():
    print(Fore.GREEN + "[Waakye] Telegram worker thread started")
    while True:
        try:
            update = telegram_queue.get(timeout=1)
            if update is None:
                break
            print(Fore.CYAN + "[Waakye] Worker picked up update, processing...")
            process_update_sync(update)
            print(Fore.CYAN + "[Waakye] Worker done processing")
        except queue.Empty:
            continue
        except Exception as e:
            print(Fore.RED + f"[Waakye] Worker error: {e}")
            import traceback
            traceback.print_exc()

# ─── Start the worker thread ──────────────────────────────────────
_worker_thread = threading.Thread(target=telegram_worker, daemon=True)
_worker_thread.start()

# ─── Telegram Webhook Setup ──────────────────────────────────────
telegram_application = None

def setup_telegram_webhook():
    global telegram_application
    if not TELEGRAM_BOT_TOKEN:
        print(Fore.YELLOW + "[Waakye] No TELEGRAM_BOT_TOKEN set, skipping webhook setup")
        return
    try:
        telegram_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        message_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: None)
        telegram_application.add_handler(message_handler)

        webhook_url = os.getenv("WEBHOOK_URL")
        if webhook_url:
            full_url = f"{webhook_url}/telegram-webhook"
            print(Fore.GREEN + f"[Waakye] Setting Telegram webhook to: {full_url}")
            # Use asyncio.run() just for this one-time setup
            import asyncio
            asyncio.run(telegram_application.bot.set_webhook(url=full_url))
            print(Fore.GREEN + "[Waakye] Telegram webhook configured successfully")
        else:
            print(Fore.YELLOW + "[Waakye] No WEBHOOK_URL set, using polling (development mode)")
    except Exception as e:
        print(Fore.RED + f"[Waakye] Failed to setup Telegram: {e}")

# ─── Async Telegram Senders (for outgoing notifications) ──────────
async def send_telegram_notification(order, customer, items, final_total):
    if not telegram_bot:
        return
    try:
        message = (
            f"🍛 <b>NEW WAAKYE ORDER</b> 🍛\n\n"
            f"<b>Order ID:</b> #{order.get_order_id()}\n"
            f"<b>Customer:</b> {customer.get_name()}\n"
            f"<b>Phone:</b> {customer.get_phone()}\n"
            f"<b>Hostel:</b> {order.get_hostel()}\n"
            f"<b>Delivery Date:</b> {order.get_delivery_date()}\n\n"
            f"<b>Items:</b>\n"
        )
        for item in items:
            message += f"  • {item}\n"
        message += (
            f"\n<b>Delivery Fee:</b> ₵{MENU['delivery_fee']:.2f}\n"
            f"<b>TOTAL:</b> ₵{final_total:.2f}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Reply <b>CONFIRM {order.get_order_id()}</b> to confirm this order\n"
            f"❌ Reply <b>REJECT {order.get_order_id()}</b> to reject this order\n"
            f"🚚 Reply <b>DELIVERED {order.get_order_id()}</b> to mark as delivered"
        )
        for chat_id in TELEGRAM_CHAT_IDS:
            for attempt in range(3):
                try:
                    await telegram_bot.send_message(chat_id=chat_id, text=message.strip(), parse_mode="HTML")
                    await asyncio.sleep(0.5)
                    break
                except TelegramError as e:
                    if attempt < 2:
                        await asyncio.sleep(1)
                    else:
                        print(Fore.RED + f"[Waakye] Telegram send failed: {e}")
    except Exception as e:
        print(Fore.RED + f"[Waakye] Unexpected Telegram error: {e}")

async def send_status_update(order_id, message_text):
    if not telegram_bot:
        return
    try:
        for chat_id in TELEGRAM_CHAT_IDS:
            await telegram_bot.send_message(chat_id=chat_id, text=message_text, parse_mode="HTML")
            await asyncio.sleep(0.3)
    except Exception as e:
        print(Fore.RED + f"[Waakye] Status update Telegram error: {e}")

# ─── Flask Routes ────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("waakye.html")

@app.route("/api/menu")
def get_menu():
    return jsonify({
        "packages":    {k: {"name": v["name"], "price": v["price"], "includes": v["includes"]}
                        for k, v in MENU["packages"].items()},
        "condiments":  {k: {"name": v["name"], "price": v["price"]}
                        for k, v in MENU["condiments"].items()},
        "delivery_fee": MENU["delivery_fee"],
        "hostels":     MENU["hostels"],
    })

@app.route("/api/order", methods=["POST"])
def place_order():
    data = request.get_json()
    name          = data.get("name", "").strip()
    phone         = data.get("phone", "").strip()
    hostel        = data.get("hostel", "").strip()
    room_notes    = data.get("room_notes", "").strip()
    delivery_date = data.get("delivery_date", "").strip()
    items_data    = data.get("items", [])

    if not delivery_date:
        now = datetime.datetime.now()
        today = now.date()
        current_hour = now.hour
        current_weekday = today.weekday()
        is_past_cutoff = (current_weekday == 4 and current_hour >= 23) or current_weekday >= 5
        if is_past_cutoff:
            days_until_saturday = (5 - current_weekday) % 7
            if days_until_saturday == 0:
                days_until_saturday = 7
            if current_weekday == 5:
                days_until_saturday = 7
        else:
            days_until_saturday = (5 - current_weekday) % 7
            if days_until_saturday == 0:
                days_until_saturday = 7
        delivery_date = (today + datetime.timedelta(days=days_until_saturday)).isoformat()

    if not all([name, phone, hostel]):
        return jsonify({"error": "Missing required fields"}), 400
    if not items_data:
        return jsonify({"error": "No items selected"}), 400

    try:
        parsed_phone = phonenumbers.parse(phone, "GH")
    except NumberParseException:
        return jsonify({"error": "Invalid phone number format"}), 400
    if not is_valid_number(parsed_phone):
        return jsonify({"error": "Invalid Ghanaian phone number"}), 400
    operator = ph_carrier.name_for_number(parsed_phone, "en") or "Unknown"

    if hostel not in MENU["hostels"]:
        return jsonify({"error": "Invalid hostel"}), 400

    weekly_count = count_orders_this_week(phone)
    if weekly_count >= WEEKLY_ORDER_LIMIT:
        return jsonify({
            "error": f"Weekly order limit reached. You can only place {WEEKLY_ORDER_LIMIT} orders per week. "
                     f"You have already placed {weekly_count} order(s) this week."
        }), 429

    try:
        items_with_qty = []
        for i in items_data:
            qty = i.get("qty", 1)
            menu_item = create_menu_item(i["type"], i["key"])
            for _ in range(qty):
                items_with_qty.append(menu_item)
        items = items_with_qty
    except (ValueError, KeyError) as e:
        return jsonify({"error": str(e)}), 400

    order = Order(Customer(name, phone), items, hostel, room_notes, delivery_date)
    subtotal    = order.subtotal()
    final_total = order.total()
    year, week  = get_iso_week()

    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO customers (phone, name) VALUES (?, ?)", (phone, name))
        db.execute(
            """INSERT INTO orders
               (order_id, phone, hostel, room_notes, delivery_date,
                items_summary, subtotal, total, timestamp,
                status, week_number, year, admin_confirms, user_confirmed_delivered)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, '', 0)""",
            (
                order.get_order_id(), phone, hostel, room_notes,
                delivery_date, order.items_summary(),
                subtotal, final_total,
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                week, year,
            )
        )

    if telegram_bot:
        try:
            import asyncio
            asyncio.run(send_telegram_notification(order, Customer(name, phone), items, final_total))
        except Exception as e:
            import traceback
            print(Fore.RED + f"[Waakye] Telegram error: {e}\n{traceback.format_exc()}")

    print(Fore.YELLOW + f"\n{'─'*44}")
    print(Fore.CYAN   + f"  WAAKYE ORDER  #{order.get_order_id()}")
    print(Fore.YELLOW + f"{'─'*44}")
    print(Fore.WHITE  + f"  Customer : {name}")
    print(Fore.WHITE  + f"  Phone    : {phone} ({operator})")
    print(Fore.WHITE  + f"  Hostel   : {hostel}")
    print(Fore.WHITE  + f"  Deliver  : {delivery_date}")
    if room_notes:
        print(Fore.WHITE + f"  Notes    : {room_notes}")
    print(Fore.YELLOW + f"{'─'*44}")
    for item in items:
        print(Fore.WHITE + f"  {item}")
    print(Fore.WHITE  + f"  Delivery fee : ₵{MENU['delivery_fee']:.2f}")
    print(Fore.CYAN   + f"  TOTAL        : ₵{final_total:.2f}")
    print(Fore.YELLOW + f"{'─'*44}\n")

    response_items = []
    for i in items_data:
        qty = i.get("qty", 1)
        menu_item = create_menu_item(i["type"], i["key"])
        response_items.append({
            "name": str(menu_item),
            "price": menu_item.get_price() * qty,
            "qty": qty
        })

    return jsonify({
        "order_id":      order.get_order_id(),
        "subtotal":      subtotal,
        "delivery_fee":  MENU["delivery_fee"],
        "final_total":   final_total,
        "operator":      operator,
        "items":         response_items,
        "weekly_orders": weekly_count + 1,
        "weekly_limit":  WEEKLY_ORDER_LIMIT,
    })

@app.route("/api/orders")
def get_orders():
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "changeme"):
        return jsonify({"error": "Unauthorized"}), 403
    with get_db() as db:
        rows = db.execute("""
            SELECT o.order_id, c.name, o.phone, o.hostel, o.room_notes,
                   o.delivery_date, o.items_summary, o.subtotal, o.total, o.timestamp,
                   o.status, o.week_number, o.year, o.admin_confirms, o.user_confirmed_delivered
            FROM orders o
            JOIN customers c ON c.phone = o.phone
            ORDER BY o.timestamp DESC
        """).fetchall()
    return jsonify([
        {
            "order_id":      r["order_id"],
            "name":          r["name"],
            "phone":         r["phone"],
            "hostel":        r["hostel"],
            "room_notes":    r["room_notes"],
            "delivery_date": r["delivery_date"],
            "items":         r["items_summary"],
            "subtotal":      r["subtotal"],
            "total":         r["total"],
            "timestamp":     r["timestamp"],
            "status":        r["status"],
            "week_number":   r["week_number"],
            "year":          r["year"],
        }
        for r in rows
    ])

@app.route("/track")
def track_order():
    return render_template("track.html")

@app.route("/api/track/<order_id>")
def track_order_api(order_id):
    order_id = order_id.upper().strip()
    with get_db() as db:
        row = db.execute("""
            SELECT o.order_id, c.name, o.phone, o.hostel, o.room_notes,
                   o.delivery_date, o.items_summary, o.subtotal, o.total, o.timestamp,
                   o.status, o.week_number, o.year
            FROM orders o
            JOIN customers c ON c.phone = o.phone
            WHERE o.order_id = ?
        """, (order_id,)).fetchone()
    if not row:
        return jsonify({"error": "Order not found"}), 404
    return jsonify({
        "order_id":      row["order_id"],
        "name":          row["name"],
        "phone":         row["phone"],
        "hostel":        row["hostel"],
        "room_notes":    row["room_notes"],
        "delivery_date": row["delivery_date"],
        "items":         row["items_summary"],
        "subtotal":      row["subtotal"],
        "total":         row["total"],
        "timestamp":     row["timestamp"],
        "status":        row["status"],
        "week_number":   row["week_number"],
        "year":          row["year"],
    })

@app.route("/api/orders-by-phone/<phone>")
def orders_by_phone(phone):
    phone = phone.strip()
    try:
        parsed = phonenumbers.parse(phone, "GH")
        if not is_valid_number(parsed):
            return jsonify({"error": "Invalid phone number"}), 400
    except NumberParseException:
        return jsonify({"error": "Invalid phone number format"}), 400

    with get_db() as db:
        rows = db.execute("""
            SELECT o.order_id, c.name, o.phone, o.hostel, o.room_notes,
                   o.delivery_date, o.items_summary, o.subtotal, o.total, o.timestamp,
                   o.status, o.week_number, o.year
            FROM orders o
            JOIN customers c ON c.phone = o.phone
            WHERE o.phone = ?
            ORDER BY o.timestamp DESC
        """, (phone,)).fetchall()

    if not rows:
        return jsonify({"error": "No orders found for this phone number"}), 404

    weeks = {}
    for r in rows:
        key = f"{r['year']}-W{r['week_number']:02d}"
        if key not in weeks:
            weeks[key] = {"year": r["year"], "week": r["week_number"], "orders": []}
        weeks[key]["orders"].append({
            "order_id":      r["order_id"],
            "name":          r["name"],
            "phone":         r["phone"],
            "hostel":        r["hostel"],
            "room_notes":    r["room_notes"],
            "delivery_date": r["delivery_date"],
            "items":         r["items_summary"],
            "subtotal":      r["subtotal"],
            "total":         r["total"],
            "timestamp":     r["timestamp"],
            "status":        r["status"],
        })

    year, week = get_iso_week()
    current_week_key = f"{year}-W{week:02d}"
    current_week_count = len(weeks.get(current_week_key, {}).get("orders", []))

    return jsonify({
        "weeks": list(weeks.values()),
        "current_week_count": current_week_count,
        "weekly_limit": WEEKLY_ORDER_LIMIT,
    })

@app.route("/api/admin-confirm", methods=["POST"])
def admin_confirm():
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "changeme"):
        return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json()
    order_id = data.get("order_id", "").strip().upper()
    chat_id = str(data.get("chat_id", "")).strip()
    if not order_id or not chat_id:
        return jsonify({"error": "order_id and chat_id are required"}), 400
    if chat_id not in TELEGRAM_CHAT_IDS:
        return jsonify({"error": "Unrecognised chat_id"}), 403

    with get_db() as db:
        row = db.execute("SELECT status, admin_confirms FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            return jsonify({"error": "Order not found"}), 404
        if row["status"] not in ("pending",):
            return jsonify({"message": f"Order is already {row['status']}"}), 200

        confirmed_ids = set(filter(None, row["admin_confirms"].split(",")))
        confirmed_ids.add(chat_id)
        new_confirms = ",".join(confirmed_ids)
        db.execute(
            "UPDATE orders SET admin_confirms=?, status='confirmed' WHERE order_id=?",
            (new_confirms, order_id)
        )

    msg = f"✅ <b>Order #{order_id} CONFIRMED</b>\nAdmin confirmed. Customer has been notified."
    try:
        import asyncio
        asyncio.run(send_status_update(order_id, msg))
    except Exception:
        pass
    return jsonify({"message": "Order confirmed", "status": "confirmed"})

@app.route("/api/cancel-order/<order_id>", methods=["POST"])
def cancel_order(order_id):
    order_id = order_id.strip().upper()
    data = request.get_json() or {}
    phone = data.get("phone", "").strip()

    with get_db() as db:
        row = db.execute("SELECT status, phone FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            return jsonify({"error": "Order not found"}), 404
        if row["phone"] != phone:
            return jsonify({"error": "Phone number does not match this order"}), 403
        if row["status"] != "pending":
            return jsonify({"error": f"Cannot cancel order with status '{row['status']}'. Only pending orders can be cancelled."}), 400
        db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))

    msg = f"❌ <b>Order #{order_id} CANCELLED</b>\nCustomer cancelled the order. Order removed from database."
    try:
        import asyncio
        asyncio.run(send_status_update(order_id, msg))
    except Exception:
        pass
    return jsonify({"message": "Order cancelled and removed", "status": "deleted"})

@app.route("/api/confirm-delivered/<order_id>", methods=["POST"])
def confirm_delivered(order_id):
    order_id = order_id.strip().upper()
    data = request.get_json() or {}
    phone = data.get("phone", "").strip()

    with get_db() as db:
        row = db.execute(
            "SELECT status, admin_confirms, user_confirmed_delivered, phone FROM orders WHERE order_id=?",
            (order_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Order not found"}), 404
        if row["phone"] != phone:
            return jsonify({"error": "Phone number does not match this order"}), 403
        if row["status"] != "confirmed":
            return jsonify({"error": f"Order is currently '{row['status']}', not ready for delivery confirmation"}), 400

        db.execute("UPDATE orders SET user_confirmed_delivered=1 WHERE order_id=?", (order_id,))

    return jsonify({
        "message": "Your delivery confirmation has been recorded. The order will be removed once the admin also confirms delivery.",
        "status": "awaiting_admin_delivery"
    })

@app.route("/api/admin-reject", methods=["POST"])
def admin_reject():
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "changeme"):
        return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json()
    order_id = data.get("order_id", "").strip().upper()
    chat_id = str(data.get("chat_id", "")).strip()
    if not order_id:
        return jsonify({"error": "order_id required"}), 400
    if chat_id not in TELEGRAM_CHAT_IDS:
        return jsonify({"error": "Unrecognised chat_id"}), 403

    with get_db() as db:
        row = db.execute("SELECT status FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not row:
            return jsonify({"error": "Order not found"}), 404
        if row["status"] not in ("pending",):
            return jsonify({"message": f"Order is already {row['status']}"}), 200
        db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))

    msg = f"❌ <b>Order #{order_id} REJECTED</b>\nAdmin rejected this order. Order removed from database."
    try:
        import asyncio
        asyncio.run(send_status_update(order_id, msg))
    except Exception:
        pass
    return jsonify({"message": "Order rejected and removed", "status": "deleted"})

@app.route("/api/admin-delivered", methods=["POST"])
def admin_delivered():
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "changeme"):
        return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json()
    order_id = data.get("order_id", "").strip().upper()
    chat_id = str(data.get("chat_id", "")).strip()
    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    with get_db() as db:
        row = db.execute(
            "SELECT status, user_confirmed_delivered FROM orders WHERE order_id=?",
            (order_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Order not found"}), 404
        if row["status"] != "confirmed":
            return jsonify({"error": f"Order is currently '{row['status']}', not ready for delivery confirmation"}), 400

        if row["user_confirmed_delivered"] == 1:
            db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))
            msg = f"🎉 <b>Order #{order_id} DELIVERED & CLOSED</b>\nBoth parties confirmed. Order removed."
            try:
                import asyncio
                asyncio.run(send_status_update(order_id, msg))
            except Exception:
                pass
            return jsonify({"message": "Order delivered and removed", "status": "deleted"})
        else:
            db.execute("UPDATE orders SET status='admin_delivered' WHERE order_id=?", (order_id,))
            return jsonify({
                "message": "Delivery recorded. Waiting for customer to confirm on the tracking page.",
                "status": "admin_delivered"
            })

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    try:
        raw = request.json
        print(Fore.CYAN + f"[Waakye] Webhook received: {raw}")
        update = Update.de_json(raw, telegram_bot)
        print(Fore.CYAN + f"[Waakye] Update parsed: {update.update_id}")
        telegram_queue.put(update)
        print(Fore.CYAN + f"[Waakye] Update queued")
    except Exception as e:
        print(Fore.RED + f"[Waakye] Webhook error: {e}")
        import traceback
        traceback.print_exc()
    return "OK", 200

@app.route("/api/weekly-limit/<phone>")
def check_weekly_limit(phone):
    phone = phone.strip()
    try:
        parsed = phonenumbers.parse(phone, "GH")
        if not is_valid_number(parsed):
            return jsonify({"error": "Invalid phone number"}), 400
    except NumberParseException:
        return jsonify({"error": "Invalid phone number format"}), 400

    count = count_orders_this_week(phone)
    return jsonify({
        "count": count,
        "limit": WEEKLY_ORDER_LIMIT,
        "remaining": max(0, WEEKLY_ORDER_LIMIT - count),
        "at_limit": count >= WEEKLY_ORDER_LIMIT,
    })

# ─── Main ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    print(Fore.YELLOW + "[Waakye] Order System running at http://127.0.0.1:5000")
    setup_telegram_webhook()

    if not os.getenv("WEBHOOK_URL") and telegram_application:
        def run_polling():
            print(Fore.GREEN + "[Waakye] Starting Telegram polling (development mode)...")
            telegram_application.run_polling(allowed_updates=Update.ALL_TYPES)
        bot_thread = threading.Thread(target=run_polling, daemon=True)
        bot_thread.start()

    app.run(debug=True, port=5000, use_reloader=False)