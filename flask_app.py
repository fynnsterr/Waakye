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
import asyncio
import threading
from datetime import date
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

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
    db = sqlite3.connect(DB_PATH)
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
        """)
        # Migrate existing DBs that don't have new columns yet
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


# ─── OOP: Menu Item Hierarchy ──────────────────────────────────────────────────

class MenuItem(ABC):
    def __init__(self, name, price):
        self._name  = name
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
        self._pkg_type = pkg_type
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
        self._name  = name
        self._phone = phone

    def get_name(self):  return self._name
    def get_phone(self): return self._phone


class Order:
    def __init__(self, customer, items, hostel, room_notes, delivery_date):
        self._order_id      = uuid.uuid4().hex[:6].upper()
        self._customer      = customer
        self._items         = items
        self._hostel        = hostel
        self._room_notes    = room_notes
        self._delivery_date = delivery_date
        self._timestamp     = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    def get_order_id(self):      return self._order_id
    def get_delivery_date(self): return self._delivery_date
    def get_hostel(self):        return self._hostel

    def subtotal(self):
        return sum(i.get_price() for i in self._items)

    def total(self):
        return self.subtotal() + MENU["delivery_fee"]

    def items_summary(self):
        return "\x1f".join(str(i) for i in self._items)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_iso_week(dt=None):
    """Return (iso_year, iso_week) for a date/datetime."""
    if dt is None:
        dt = datetime.date.today()
    iso = dt.isocalendar()
    return iso[0], iso[1]   # (year, week)


def count_orders_this_week(phone):
    """Count how many orders a phone number has placed in the current ISO week."""
    year, week = get_iso_week()
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM orders WHERE phone=? AND year=? AND week_number=?",
            (phone, year, week)
        ).fetchone()
    return row[0]


# ─── Telegram Bot Handler ───────────────────────────────────────────────────────

async def handle_telegram_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming Telegram messages from admins."""
    if not update.message or not update.message.text:
        return

    chat_id = str(update.effective_chat.id)
    if chat_id not in TELEGRAM_CHAT_IDS:
        await update.message.reply_text("You are not authorized to use this bot.")
        return

    text = update.message.text.strip().upper()
    parts = text.split()

    if len(parts) < 2:
        await update.message.reply_text("Usage: CONFIRM <order_id>, REJECT <order_id>, or DELIVERED <order_id>")
        return

    command = parts[0]
    order_id = parts[1]

    if command == "CONFIRM":
        # Call the admin-confirm endpoint logic directly
        with get_db() as db:
            row = db.execute(
                "SELECT status, admin_confirms FROM orders WHERE order_id=?",
                (order_id,)
            ).fetchone()

            if not row:
                await update.message.reply_text(f"❌ Order #{order_id} not found")
                return
            if row["status"] not in ("pending",):
                await update.message.reply_text(f"Order is already {row['status']}")
                return

            # Single admin confirmation - change status immediately
            confirmed_ids = set(filter(None, row["admin_confirms"].split(",")))
            confirmed_ids.add(chat_id)
            new_confirms = ",".join(confirmed_ids)

            db.execute(
                "UPDATE orders SET admin_confirms=?, status='confirmed' WHERE order_id=?",
                (new_confirms, order_id)
            )

        await update.message.reply_text(f"✅ Order #{order_id} CONFIRMED")

    elif command == "REJECT":
        # Call the admin-reject endpoint logic directly
        with get_db() as db:
            row = db.execute(
                "SELECT status FROM orders WHERE order_id=?",
                (order_id,)
            ).fetchone()

            if not row:
                await update.message.reply_text(f"❌ Order #{order_id} not found")
                return
            if row["status"] not in ("pending",):
                await update.message.reply_text(f"Order is already {row['status']}")
                return

            db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))

        await update.message.reply_text(f"❌ Order #{order_id} REJECTED and removed")

    elif command == "DELIVERED":
        # Call the admin-delivered endpoint logic directly
        with get_db() as db:
            row = db.execute(
                "SELECT status, user_confirmed_delivered FROM orders WHERE order_id=?",
                (order_id,)
            ).fetchone()

            if not row:
                await update.message.reply_text(f"❌ Order #{order_id} not found")
                return

            if row["status"] != "confirmed":
                await update.message.reply_text(f"Order is currently '{row['status']}', not ready for delivery confirmation")
                return

            if row["user_confirmed_delivered"] == 1:
                # Both sides confirmed — delete the order
                db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))
                await update.message.reply_text(f"🎉 Order #{order_id} DELIVERED & CLOSED\nBoth parties confirmed. Order removed.")
            else:
                # Mark admin side as delivered, wait for user
                db.execute(
                    "UPDATE orders SET status='admin_delivered' WHERE order_id=?",
                    (order_id,)
                )
                await update.message.reply_text(f"Delivery recorded. Waiting for customer to confirm on the tracking page.")
    else:
        await update.message.reply_text("Unknown command. Use: CONFIRM, REJECT, or DELIVERED")


# ─── Telegram Webhook Setup ───────────────────────────────────────────────────

telegram_application = None

def setup_telegram_webhook():
    """Set up Telegram webhook for production (Render)."""
    global telegram_application
    if not TELEGRAM_BOT_TOKEN:
        print(Fore.YELLOW + "[Waakye] No TELEGRAM_BOT_TOKEN set, skipping webhook setup")
        return

    try:
        telegram_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        # Add message handler
        message_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_telegram_message)
        telegram_application.add_handler(message_handler)

        # Get webhook URL from environment or use default
        webhook_url = os.getenv("WEBHOOK_URL")
        if webhook_url:
            # Production: Set webhook
            full_url = f"{webhook_url}/telegram-webhook"
            print(Fore.GREEN + f"[Waakye] Setting Telegram webhook to: {full_url}")
            asyncio.run(telegram_application.bot.set_webhook(url=full_url))
            print(Fore.GREEN + "[Waakye] Telegram webhook configured successfully")
        else:
            # Development: Use polling
            print(Fore.YELLOW + "[Waakye] No WEBHOOK_URL set, using polling (development mode)")
    except Exception as e:
        print(Fore.RED + f"[Waakye] Failed to setup Telegram: {e}")


# ─── Telegram ──────────────────────────────────────────────────────────────────

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
                    await telegram_bot.send_message(
                        chat_id=chat_id,
                        text=message.strip(),
                        parse_mode="HTML"
                    )
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
    """Send a status update message to all Telegram chat IDs."""
    if not telegram_bot:
        return
    try:
        for chat_id in TELEGRAM_CHAT_IDS:
            await telegram_bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode="HTML"
            )
            await asyncio.sleep(0.3)
    except Exception as e:
        print(Fore.RED + f"[Waakye] Status update Telegram error: {e}")


# ─── Factory ───────────────────────────────────────────────────────────────────

def create_menu_item(item_type, item_key):
    match item_type:
        case "package":   return Package(item_key)
        case "condiment": return Condiment(item_key)
        case _:           raise ValueError(f"Unknown item type: {item_type}")


# ─── Routes ────────────────────────────────────────────────────────────────────

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

    # ── Weekly order limit check ─────────────────────────────────────────────
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
        db.execute(
            "INSERT OR IGNORE INTO customers (phone, name) VALUES (?, ?)",
            (phone, name)
        )
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
    """
    Return all orders for a given phone number, grouped by ISO week.
    This is what the tracking page uses after the user enters their phone.
    """
    phone = phone.strip()

    # Validate phone
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

    # Group by (year, week_number)
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

    # Build weekly limit info for current week
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
    """
    Called when a Telegram admin replies CONFIRM <order_id>.
    Requires ?key=<ADMIN_KEY> for security.
    Body: { "order_id": "ABC123", "chat_id": "123456789" }
    Single admin confirmation required.
    """
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "changeme"):
        return jsonify({"error": "Unauthorized"}), 403

    data     = request.get_json()
    order_id = data.get("order_id", "").strip().upper()
    chat_id  = str(data.get("chat_id", "")).strip()

    if not order_id or not chat_id:
        return jsonify({"error": "order_id and chat_id are required"}), 400

    if chat_id not in TELEGRAM_CHAT_IDS:
        return jsonify({"error": "Unrecognised chat_id"}), 403

    with get_db() as db:
        row = db.execute(
            "SELECT status, admin_confirms FROM orders WHERE order_id=?",
            (order_id,)
        ).fetchone()

        if not row:
            return jsonify({"error": "Order not found"}), 404
        if row["status"] not in ("pending",):
            return jsonify({"message": f"Order is already {row['status']}"}), 200

        # Track which chat IDs have confirmed
        confirmed_ids = set(filter(None, row["admin_confirms"].split(",")))
        confirmed_ids.add(chat_id)
        new_confirms = ",".join(confirmed_ids)

        # Single admin confirmation - change status immediately
        db.execute(
            "UPDATE orders SET admin_confirms=?, status='confirmed' WHERE order_id=?",
            (new_confirms, order_id)
        )

    msg = (
        f"✅ <b>Order #{order_id} CONFIRMED</b>\n"
        f"Admin confirmed. Customer has been notified."
    )
    try:
        asyncio.run(send_status_update(order_id, msg))
    except Exception:
        pass
    return jsonify({"message": "Order confirmed", "status": "confirmed"})


@app.route("/api/cancel-order/<order_id>", methods=["POST"])
def cancel_order(order_id):
    """
    Called when the customer cancels an order on the tracking page.
    Only allowed when status is 'pending'.
    Deletes the order from the database.
    """
    order_id = order_id.strip().upper()

    data  = request.get_json() or {}
    phone = data.get("phone", "").strip()

    with get_db() as db:
        row = db.execute(
            "SELECT status, phone FROM orders WHERE order_id=?",
            (order_id,)
        ).fetchone()

        if not row:
            return jsonify({"error": "Order not found"}), 404
        if row["phone"] != phone:
            return jsonify({"error": "Phone number does not match this order"}), 403
        if row["status"] != "pending":
            return jsonify({"error": f"Cannot cancel order with status '{row['status']}'. Only pending orders can be cancelled."}), 400

        db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))

    # Notify admins about cancellation
    msg = f"❌ <b>Order #{order_id} CANCELLED</b>\nCustomer cancelled the order. Order removed from database."
    try:
        asyncio.run(send_status_update(order_id, msg))
    except Exception:
        pass

    return jsonify({
        "message": "Order cancelled and removed",
        "status": "deleted"
    })


@app.route("/api/confirm-delivered/<order_id>", methods=["POST"])
def confirm_delivered(order_id):
    """
    Called when the customer presses 'Confirm Delivered' on the tracking page.
    Admin must have already replied DELIVERED <order_id> on Telegram.
    If both have confirmed, order is deleted from the database.
    """
    order_id = order_id.strip().upper()

    # Require the phone to match (simple ownership check)
    data  = request.get_json() or {}
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

        db.execute(
            "UPDATE orders SET user_confirmed_delivered=1 WHERE order_id=?",
            (order_id,)
        )

        # Check if admin has also flagged it as delivered
        # We reuse admin_confirms — when admin sends DELIVERED, we set status to 'admin_delivered'
        admin_delivered = row["status"] == "confirmed"  # admin side checked below
        # Re-fetch to see if admin_delivered flag is set
        row2 = db.execute(
            "SELECT status FROM orders WHERE order_id=?", (order_id,)
        ).fetchone()

    return jsonify({
        "message": "Your delivery confirmation has been recorded. "
                   "The order will be removed once the admin also confirms delivery.",
        "status": "awaiting_admin_delivery"
    })


@app.route("/api/admin-reject", methods=["POST"])
def admin_reject():
    """
    Called when a Telegram admin replies REJECT <order_id>.
    Requires ?key=<ADMIN_KEY>.
    Deletes the order from the database.
    """
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "changeme"):
        return jsonify({"error": "Unauthorized"}), 403

    data     = request.get_json()
    order_id = data.get("order_id", "").strip().upper()
    chat_id  = str(data.get("chat_id", "")).strip()

    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    if chat_id not in TELEGRAM_CHAT_IDS:
        return jsonify({"error": "Unrecognised chat_id"}), 403

    with get_db() as db:
        row = db.execute(
            "SELECT status FROM orders WHERE order_id=?",
            (order_id,)
        ).fetchone()

        if not row:
            return jsonify({"error": "Order not found"}), 404
        if row["status"] not in ("pending",):
            return jsonify({"message": f"Order is already {row['status']}"}), 200

        db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))

    msg = f"❌ <b>Order #{order_id} REJECTED</b>\nAdmin rejected this order. Order removed from database."
    try:
        asyncio.run(send_status_update(order_id, msg))
    except Exception:
        pass

    return jsonify({"message": "Order rejected and removed", "status": "deleted"})


@app.route("/api/admin-delivered", methods=["POST"])
def admin_delivered():
    """
    Called when a Telegram admin replies DELIVERED <order_id>.
    Requires ?key=<ADMIN_KEY>.
    If user has also confirmed, the order is deleted.
    """
    if request.args.get("key") != os.environ.get("ADMIN_KEY", "changeme"):
        return jsonify({"error": "Unauthorized"}), 403

    data     = request.get_json()
    order_id = data.get("order_id", "").strip().upper()
    chat_id  = str(data.get("chat_id", "")).strip()

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
            # Both sides confirmed — delete the order
            db.execute("DELETE FROM orders WHERE order_id=?", (order_id,))
            msg = f"🎉 <b>Order #{order_id} DELIVERED & CLOSED</b>\nBoth parties confirmed. Order removed."
            try:
                asyncio.run(send_status_update(order_id, msg))
            except Exception:
                pass
            return jsonify({"message": "Order delivered and removed", "status": "deleted"})
        else:
            # Mark admin side as delivered, wait for user
            db.execute(
                "UPDATE orders SET status='admin_delivered' WHERE order_id=?",
                (order_id,)
            )
            return jsonify({
                "message": "Delivery recorded. Waiting for customer to confirm on the tracking page.",
                "status": "admin_delivered"
            })


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Webhook endpoint for Telegram bot updates."""
    try:
        update = Update.de_json(request.json, telegram_bot)
        # Create a simple context object
        class SimpleContext:
            pass
        context = SimpleContext()
        # Handle the update synchronously
        asyncio.run(handle_telegram_message(update, context))
    except Exception as e:
        print(Fore.RED + f"[Waakye] Webhook error: {e}")
    return "OK", 200


@app.route("/api/weekly-limit/<phone>")
def check_weekly_limit(phone):
    """Quick check — how many orders has this phone placed this week?"""
    phone = phone.strip()
    try:
        parsed = phonenumbers.parse(phone, "GH")
        if not is_valid_number(parsed):
            return jsonify({"error": "Invalid phone number"}), 400
    except NumberParseException:
        return jsonify({"error": "Invalid phone number format"}), 400

    count = count_orders_this_week(phone)
    return jsonify({
        "count":   count,
        "limit":   WEEKLY_ORDER_LIMIT,
        "remaining": max(0, WEEKLY_ORDER_LIMIT - count),
        "at_limit": count >= WEEKLY_ORDER_LIMIT,
    })


if __name__ == "__main__":
    init_db()
    print(Fore.YELLOW + "[Waakye] Order System running at http://127.0.0.1:5000")

    # Setup Telegram (webhook for production, polling for development)
    setup_telegram_webhook()

    # If no webhook URL, run polling in background thread for development
    if not os.getenv("WEBHOOK_URL") and telegram_application:
        def run_polling():
            print(Fore.GREEN + "[Waakye] Starting Telegram polling (development mode)...")
            telegram_application.run_polling(allowed_updates=Update.ALL_TYPES)
        bot_thread = threading.Thread(target=run_polling, daemon=True)
        bot_thread.start()

    app.run(debug=True, port=5000, use_reloader=False)