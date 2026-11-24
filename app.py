import sqlite3, io, base64, uuid, csv, os
from datetime import date, timedelta
from functools import wraps
# pyrefly: ignore [missing-import]
from flask import (Flask, g, render_template, request, redirect,
    url_for, session, flash, Response)
# pyrefly: ignore [missing-import]
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "smartcanteen-supersecret-2024-change-me")
DB = os.environ.get("DB_PATH", "canteen_v2.db")
STATUSES = ["Placed", "Preparing", "Ready", "Collected"]
SLOTS = [f"{h:02d}:{m:02d}" for h in range(8, 21) for m in (0, 15, 30, 45)]
SLOT_CAP = 10
LOW_STOCK = 10


# =================== DATABASE ===================
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()

def one(sql, a=()):
    return db().execute(sql, a).fetchone()

def many(sql, a=()):
    return db().execute(sql, a).fetchall()

def run(sql, a=()):
    c = db().execute(sql, a)
    db().commit()
    return c

def init_db():
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, college_id TEXT,
        email TEXT UNIQUE, password TEXT, role TEXT DEFAULT 'student',
        wallet REAL DEFAULT 0, phone TEXT DEFAULT '', bio TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS menu_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, category TEXT,
        price REAL, available INTEGER DEFAULT 1, stock INTEGER DEFAULT 50,
        veg INTEGER DEFAULT 1, description TEXT DEFAULT '', image_url TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS coupons(
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE,
        percent INTEGER, min_order REAL DEFAULT 0, active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, token TEXT,
        subtotal REAL, discount REAL DEFAULT 0, total REAL, coupon TEXT,
        pickup_slot TEXT, payment_method TEXT, payment_status TEXT,
        status TEXT DEFAULT 'Placed', notes TEXT DEFAULT '',
        order_date TEXT DEFAULT (date('now','localtime')),
        created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS order_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
        item_id INTEGER, name TEXT, price REAL, qty INTEGER);
    CREATE TABLE IF NOT EXISTS ratings(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, item_id INTEGER,
        order_id INTEGER, rating INTEGER, comment TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(order_id, item_id));
    CREATE TABLE IF NOT EXISTS wallet_txn(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
        note TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS announcements(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, content TEXT,
        type TEXT DEFAULT 'info', active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS favorites(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, item_id INTEGER, UNIQUE(user_id, item_id));
    CREATE TABLE IF NOT EXISTS notifications(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        message TEXT, link TEXT DEFAULT '', is_read INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')));
    """)
    # Migrations for existing databases
    for stmt in [
        "ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN bio TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN created_at TEXT DEFAULT (datetime('now','localtime'))",
        "ALTER TABLE coupons ADD COLUMN min_order REAL DEFAULT 0",
        "ALTER TABLE coupons ADD COLUMN created_at TEXT DEFAULT (datetime('now','localtime'))",
        "ALTER TABLE orders ADD COLUMN notes TEXT DEFAULT ''",
        "ALTER TABLE menu_items ADD COLUMN image_url TEXT DEFAULT ''",
    ]:
        try:
            con.execute(stmt)
        except Exception:
            pass
    # Seed admin
    if not con.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        con.execute("INSERT INTO users(name,college_id,email,password,role) VALUES(?,?,?,?,?)",
                    ("Admin", "ADMIN001", "admin@canteen.com",
                     generate_password_hash("admin123"), "admin"))
    # Seed menu items
    if not con.execute("SELECT 1 FROM menu_items").fetchone():
        items = [
            ("Samosa", "Snacks", 15, 40, 1, "Crispy potato-filled pastry, served with green chutney"),
            ("Veg Sandwich", "Snacks", 30, 30, 1, "Grilled with chutney, veggies & cheese"),
            ("Paneer Puff", "Snacks", 25, 35, 1, "Flaky pastry stuffed with spiced paneer filling"),
            ("French Fries", "Snacks", 40, 25, 1, "Crispy golden fries with seasoning"),
            ("Chicken Roll", "Snacks", 50, 20, 0, "Spicy chicken wrap in a flaky paratha"),
            ("Spring Roll", "Snacks", 35, 30, 1, "Crispy veg spring rolls with dipping sauce"),
            ("Veg Thali", "Meals", 70, 30, 1, "2 Rotis, dal, sabzi, rice, papad & pickle"),
            ("Chole Bhature", "Meals", 60, 25, 1, "Spicy chickpeas with 2 fluffy bhature"),
            ("Fried Rice", "Meals", 65, 25, 1, "Mixed vegetable fried rice Chinese style"),
            ("Chicken Biryani", "Meals", 90, 15, 0, "Hyderabadi dum biryani with raita"),
            ("Paneer Butter Masala", "Meals", 85, 20, 1, "Rich creamy paneer in tomato gravy + roti"),
            ("Dal Khichdi", "Meals", 55, 30, 1, "Comforting dal with rice & ghee"),
            ("Pav Bhaji", "Meals", 50, 25, 1, "Spiced vegetable curry with buttered pav"),
            ("Tea", "Beverages", 10, 100, 1, "Hot masala chai — the perfect companion"),
            ("Coffee", "Beverages", 20, 80, 1, "Creamy hot milk coffee"),
            ("Cold Drink", "Beverages", 25, 60, 1, "Chilled Pepsi / Coke / Sprite"),
            ("Fresh Lime Soda", "Beverages", 30, 50, 1, "Sweet or salted fresh lime with soda"),
            ("Mango Lassi", "Beverages", 35, 40, 1, "Thick alphonso mango yogurt drink"),
            ("Gulab Jamun", "Desserts", 20, 50, 1, "2 soft pieces in warm rose syrup"),
            ("Ice Cream Cup", "Desserts", 30, 40, 1, "2 scoops — vanilla, chocolate or strawberry"),
            ("Rasgulla", "Desserts", 25, 35, 1, "2 spongy chenna balls in sugar syrup"),
        ]
        con.executemany(
            "INSERT INTO menu_items(name,category,price,stock,veg,description) VALUES(?,?,?,?,?,?)",
            items)
    # Seed coupons
    if not con.execute("SELECT 1 FROM coupons").fetchone():
        con.executemany("INSERT INTO coupons(code,percent,min_order) VALUES(?,?,?)",
                        [("WELCOME10", 10, 0), ("COLLEGE20", 20, 100), ("FEST15", 15, 50)])
    # Seed announcement
    if not con.execute("SELECT 1 FROM announcements").fetchone():
        con.execute("INSERT INTO announcements(title,content,type) VALUES(?,?,?)",
                    ("Welcome to Smart Canteen!", "Order food online and skip the queue! Use code WELCOME10 for 10% off your first order.", "success"))
    con.commit()
    con.close()


# =================== HELPERS ===================
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session:
            flash("Please sign in to continue.")
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if session.get("role") != "admin":
            flash("Admin access only.")
            return redirect(url_for("login"))
        return f(*a, **k)
    return w

def qr_data_uri(text):
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def add_notification(uid, message, link=""):
    run("INSERT INTO notifications(user_id,message,link) VALUES(?,?,?)", (uid, message, link))

@app.context_processor
def inject():
    d = {"wallet_bal": 0, "ready_count": 0, "cart_count": 0,
         "unread_notif": 0, "active_announcements": []}
    if session.get("uid"):
        if session.get("role") != "admin":
            u = one("SELECT wallet FROM users WHERE id=?", (session["uid"],))
            if u:
                d["wallet_bal"] = u["wallet"]
            d["ready_count"] = one(
                "SELECT COUNT(*) FROM orders WHERE user_id=? AND status='Ready'",
                (session["uid"],))[0]
            d["cart_count"] = sum(session.get("cart", {}).values())
            d["unread_notif"] = one(
                "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
                (session["uid"],))[0]
        d["active_announcements"] = many(
            "SELECT * FROM announcements WHERE active=1 ORDER BY id DESC LIMIT 3")
    return d

def cart_summary():
    rows, sub = [], 0
    cart = dict(session.get("cart", {}))
    for iid, qty in list(cart.items()):
        it = one("SELECT * FROM menu_items WHERE id=?", (iid,))
        if it:
            rows.append(dict(id=it["id"], name=it["name"], price=it["price"], qty=qty,
                             sub=it["price"] * qty, stock=it["stock"],
                             available=it["available"], veg=it["veg"]))
            sub += it["price"] * qty
        else:
            cart.pop(iid, None)
            session["cart"] = cart
    disc, code = 0, session.get("coupon")
    if code:
        c = one("SELECT * FROM coupons WHERE code=? AND active=1", (code,))
        if c and sub >= (c["min_order"] or 0):
            disc = round(sub * c["percent"] / 100)
        else:
            session.pop("coupon", None)
            code = None
    return rows, sub, disc, sub - disc, code

def queue_info():
    today = date.today().isoformat()
    n = one("SELECT COUNT(*) FROM orders WHERE status IN ('Placed','Preparing') AND order_date=?", (today,))[0]
    return n, 5 + n * 2

def slot_status():
    counts = {r["pickup_slot"]: r["c"] for r in many(
        "SELECT pickup_slot, COUNT(*) c FROM orders WHERE order_date=? AND status!='Cancelled' "
        "GROUP BY pickup_slot", (date.today().isoformat(),))}
    return [(s, counts.get(s, 0)) for s in SLOTS]

def wallet_add(uid, amount, note):
    run("UPDATE users SET wallet=wallet+? WHERE id=?", (amount, uid))
    run("INSERT INTO wallet_txn(user_id,amount,note) VALUES(?,?,?)", (uid, amount, note))

def cancel_order(oid):
    o = one("SELECT * FROM orders WHERE id=?", (oid,))
    if not o or o["status"] != "Placed":
        return False
    for it in many("SELECT * FROM order_items WHERE order_id=?", (oid,)):
        run("UPDATE menu_items SET stock=stock+?, "
            "available=CASE WHEN stock+?>0 THEN 1 ELSE available END WHERE id=?",
            (it["qty"], it["qty"], it["item_id"]))
    pay = o["payment_status"]
    if pay == "Paid":
        wallet_add(o["user_id"], o["total"], f"Refund for order {o['token']}")
        pay = "Refunded"
        add_notification(o["user_id"],
            f"₹{o['total']:.0f} refunded to your wallet for cancelled order {o['token']}.",
            f"/order/{oid}")
    run("UPDATE orders SET status='Cancelled', payment_status=? WHERE id=?", (pay, oid))
    return True


# =================== AUTH ===================
@app.route("/")
def index():
    if session.get("role") == "admin":
        return redirect(url_for("admin_home"))
    return redirect(url_for("menu") if "uid" in session else url_for("login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        college_id = request.form.get("college_id", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not all([name, college_id, email, password]):
            flash("All fields are required.")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.")
            return render_template("register.html")
        try:
            run("INSERT INTO users(name,college_id,email,password) VALUES(?,?,?,?)",
                (name, college_id, email, generate_password_hash(password)))
            uid = one("SELECT id FROM users WHERE email=?", (email,))["id"]
            add_notification(uid,
                "🎉 Welcome to Smart Canteen! Use code WELCOME10 for 10% off your first order.", "/menu")
            flash("Account created successfully! Welcome aboard 🎉")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("This email is already registered. Try logging in instead.")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = one("SELECT * FROM users WHERE email=?",
                (request.form.get("email", "").lower(),))
        if u and check_password_hash(u["password"], request.form.get("password", "")):
            session.clear()
            session.update(uid=u["id"], name=u["name"], role=u["role"])
            flash(f"Welcome back, {u['name']}! 👋")
            return redirect(url_for("index"))
        flash("Invalid email or password. Please try again.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("You've been signed out successfully.")
    return redirect(url_for("login"))


# =================== STUDENT ===================
RATING_SQL = ("SELECT m.*, "
              "(SELECT ROUND(AVG(rating),1) FROM ratings r WHERE r.item_id=m.id) avg_rating, "
              "(SELECT COUNT(*) FROM ratings r WHERE r.item_id=m.id) n_rating "
              "FROM menu_items m ")

@app.route("/menu")
@login_required
def menu():
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "")
    veg = request.args.get("veg")
    sql, args = RATING_SQL + "WHERE 1=1", []
    if q:
        sql += " AND (m.name LIKE ? OR m.description LIKE ?)"; args.extend([f"%{q}%", f"%{q}%"])
    if cat:
        sql += " AND m.category=?"; args.append(cat)
    if veg:
        sql += " AND m.veg=1"
    items = many(sql + " ORDER BY m.category, m.name", args)
    cats = {}
    for i in items:
        cats.setdefault(i["category"], []).append(i)
    fav_ids = set()
    fav = popular = []
    if session.get("role") != "admin":
        fav_ids = {r["item_id"] for r in many(
            "SELECT item_id FROM favorites WHERE user_id=?", (session["uid"],))}
        if not (q or cat or veg):
            fav = many(RATING_SQL +
                       "WHERE m.id IN (SELECT oi.item_id FROM order_items oi "
                       "JOIN orders o ON o.id=oi.order_id WHERE o.user_id=? "
                       "AND o.status!='Cancelled' GROUP BY oi.item_id "
                       "ORDER BY SUM(oi.qty) DESC LIMIT 4)", (session["uid"],))
            popular = many(RATING_SQL +
                           "WHERE m.id IN (SELECT oi.item_id FROM order_items oi "
                           "JOIN orders o ON o.id=oi.order_id WHERE o.status!='Cancelled' "
                           "GROUP BY oi.item_id ORDER BY SUM(oi.qty) DESC LIMIT 4)")
    categories = [r[0] for r in many(
        "SELECT DISTINCT category FROM menu_items ORDER BY category")]
    n, wait = queue_info()
    return render_template("menu.html", cats=cats, fav=fav, popular=popular,
                           categories=categories, q=q, cat=cat, veg=veg,
                           queue=n, wait=wait, fav_ids=fav_ids)

@app.route("/menu/favorite/<int:iid>")
@login_required
def toggle_favorite(iid):
    existing = one("SELECT id FROM favorites WHERE user_id=? AND item_id=?",
                   (session["uid"], iid))
    if existing:
        run("DELETE FROM favorites WHERE user_id=? AND item_id=?", (session["uid"], iid))
    else:
        run("INSERT OR IGNORE INTO favorites(user_id,item_id) VALUES(?,?)",
            (session["uid"], iid))
    return redirect(request.referrer or url_for("menu"))

@app.route("/favorites")
@login_required
def favorites():
    items = many(RATING_SQL +
                 "WHERE m.id IN (SELECT item_id FROM favorites WHERE user_id=?) "
                 "ORDER BY m.category, m.name", (session["uid"],))
    fav_ids = {i["id"] for i in items}
    return render_template("favorites.html", items=items, fav_ids=fav_ids)

@app.route("/cart")
@login_required
def cart():
    rows, sub, disc, total, code = cart_summary()
    return render_template("cart.html", rows=rows, sub=sub, disc=disc, total=total,
                           code=code, slots=slot_status(), cap=SLOT_CAP)

@app.route("/cart/add/<int:iid>")
@login_required
def cart_add(iid):
    it = one("SELECT * FROM menu_items WHERE id=?", (iid,))
    cart = session.get("cart", {})
    have = cart.get(str(iid), 0)
    if not it or not it["available"] or it["stock"] <= 0:
        flash("This item is currently not available.")
    elif have + 1 > it["stock"]:
        flash(f"Only {it['stock']} units of '{it['name']}' in stock.")
    else:
        cart[str(iid)] = have + 1
        session["cart"] = cart
        flash(f"✓ {it['name']} added to cart!")
    return redirect(request.referrer or url_for("menu"))

@app.route("/cart/dec/<int:iid>")
@login_required
def cart_dec(iid):
    cart = session.get("cart", {})
    if str(iid) in cart:
        cart[str(iid)] -= 1
        if cart[str(iid)] <= 0:
            del cart[str(iid)]
    session["cart"] = cart
    return redirect(url_for("cart"))

@app.route("/cart/remove/<int:iid>")
@login_required
def cart_remove(iid):
    cart = session.get("cart", {})
    cart.pop(str(iid), None)
    session["cart"] = cart
    return redirect(url_for("cart"))

@app.route("/cart/coupon", methods=["POST"])
@login_required
def cart_coupon():
    code = request.form.get("code", "").strip().upper()
    rows, sub, _, _, _ = cart_summary()
    c = one("SELECT * FROM coupons WHERE code=? AND active=1", (code,))
    if c:
        min_o = c["min_order"] or 0
        if sub < min_o:
            flash(f"Minimum order ₹{min_o:.0f} required for coupon {code}.")
        else:
            session["coupon"] = code
            flash(f"🎉 Coupon {code} applied! Save {c['percent']}% off.")
    else:
        flash("Invalid or expired coupon code.")
    return redirect(url_for("cart"))

@app.route("/cart/coupon/clear")
@login_required
def coupon_clear():
    session.pop("coupon", None)
    return redirect(url_for("cart"))

@app.route("/reorder/<int:oid>")
@login_required
def reorder(oid):
    o = one("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, session["uid"]))
    if not o:
        return redirect(url_for("orders"))
    items = many("SELECT * FROM order_items WHERE order_id=?", (oid,))
    cart, added, skipped = {}, [], []
    for it in items:
        mi = one("SELECT * FROM menu_items WHERE id=?", (it["item_id"],))
        if mi and mi["available"] and mi["stock"] > 0:
            qty = min(it["qty"], mi["stock"])
            cart[str(it["item_id"])] = qty
            added.append(it["name"])
        else:
            skipped.append(it["name"])
    session["cart"] = cart
    if added:
        flash(f"✓ {len(added)} item(s) added to cart from previous order.")
    if skipped:
        flash(f"⚠ Skipped (unavailable): {', '.join(skipped)}")
    return redirect(url_for("cart"))

@app.route("/notifications")
@login_required
def notifications():
    notifs = many(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50",
        (session["uid"],))
    run("UPDATE notifications SET is_read=1 WHERE user_id=?", (session["uid"],))
    return render_template("notifications.html", notifs=notifs)

@app.route("/notifications/mark-read", methods=["POST"])
@login_required
def notifications_read():
    run("UPDATE notifications SET is_read=1 WHERE user_id=?", (session["uid"],))
    return redirect(request.referrer or url_for("menu"))

@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    rows, sub, disc, total, code = cart_summary()
    if not rows:
        flash("Your cart is empty.")
        return redirect(url_for("menu"))
    for r in rows:
        if not r["available"] or r["qty"] > r["stock"]:
            flash(f"'{r['name']}' is not available in that quantity. Please update your cart.")
            return redirect(url_for("cart"))
    slot = request.form.get("slot", "")
    if slot not in SLOTS:
        flash("Invalid pickup slot selected.")
        return redirect(url_for("cart"))
    booked = dict(slot_status()).get(slot, 0)
    if booked >= SLOT_CAP:
        flash("That pickup slot is full. Please choose another time.")
        return redirect(url_for("cart"))
    method = request.form.get("payment", "Cash")
    notes = request.form.get("notes", "").strip()[:200]
    if method == "UPI" and not request.form.get("upi_id", "").strip():
        flash("Please enter your UPI ID to proceed.")
        return redirect(url_for("cart"))
    if method == "Wallet":
        bal = one("SELECT wallet FROM users WHERE id=?", (session["uid"],))["wallet"]
        if bal < total:
            flash(f"Insufficient wallet balance. Need ₹{total:.0f}, have ₹{bal:.0f}. Please top up.")
            return redirect(url_for("cart"))
    # Unique token
    for _ in range(10):
        token = "TKN-" + uuid.uuid4().hex[:6].upper()
        if not one("SELECT 1 FROM orders WHERE token=?", (token,)):
            break
    pay_status = "Pending" if method == "Cash" else "Paid"
    oid = run(
        "INSERT INTO orders(user_id,token,subtotal,discount,total,coupon,"
        "pickup_slot,payment_method,payment_status,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (session["uid"], token, sub, disc, total, code, slot, method, pay_status, notes)
    ).lastrowid
    for r in rows:
        run("INSERT INTO order_items(order_id,item_id,name,price,qty) VALUES(?,?,?,?,?)",
            (oid, r["id"], r["name"], r["price"], r["qty"]))
        run("UPDATE menu_items SET stock=stock-?, "
            "available=CASE WHEN stock-?<=0 THEN 0 ELSE available END WHERE id=?",
            (r["qty"], r["qty"], r["id"]))
    if method == "Wallet":
        wallet_add(session["uid"], -total, f"Payment for order {token}")
    session["cart"] = {}
    session.pop("coupon", None)
    add_notification(session["uid"],
        f"✅ Order {token} placed! Pickup at {slot}.", f"/order/{oid}")
    flash(f"🎉 Order placed! Token: {token}")
    return redirect(url_for("order_detail", oid=oid))

@app.route("/orders")
@login_required
def orders():
    status_filter = request.args.get("status", "")
    sql = "SELECT * FROM orders WHERE user_id=?"
    args = [session["uid"]]
    if status_filter:
        sql += " AND status=?"; args.append(status_filter)
    sql += " ORDER BY id DESC"
    rows = many(sql, args)
    return render_template("orders.html", orders=rows, status_filter=status_filter,
                           statuses=STATUSES + ["Cancelled"])

@app.route("/order/<int:oid>")
@login_required
def order_detail(oid):
    o = one("SELECT * FROM orders WHERE id=?", (oid,))
    if not o or (o["user_id"] != session["uid"] and session["role"] != "admin"):
        return redirect(url_for("orders"))
    items = many("SELECT * FROM order_items WHERE order_id=?", (oid,))
    rated = {r["item_id"]: r for r in many("SELECT * FROM ratings WHERE order_id=?", (oid,))}
    today = date.today().isoformat()
    ahead = one(
        "SELECT COUNT(*) FROM orders WHERE status IN ('Placed','Preparing') "
        "AND id<? AND order_date=?", (oid, today))[0]
    return render_template("order_detail.html", o=o, items=items, statuses=STATUSES,
                           rated=rated, qr=qr_data_uri(o["token"]),
                           ahead=ahead, wait=5 + ahead * 2)

@app.route("/order/<int:oid>/cancel", methods=["POST"])
@login_required
def order_cancel(oid):
    o = one("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, session["uid"]))
    if o and cancel_order(oid):
        msg = "Order cancelled successfully."
        if o["payment_status"] == "Paid":
            msg += f" ₹{o['total']:.0f} has been refunded to your wallet."
        flash(msg)
    else:
        flash("This order cannot be cancelled. Only 'Placed' orders are cancellable.")
    return redirect(url_for("order_detail", oid=oid))

@app.route("/order/<int:oid>/rate", methods=["POST"])
@login_required
def order_rate(oid):
    o = one("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, session["uid"]))
    if o and o["status"] == "Collected":
        for it in many("SELECT * FROM order_items WHERE order_id=?", (oid,)):
            r = request.form.get(f"r{it['item_id']}")
            if r and r.isdigit() and 1 <= int(r) <= 5:
                run("INSERT OR REPLACE INTO ratings(user_id,item_id,order_id,rating,comment) "
                    "VALUES(?,?,?,?,?)",
                    (session["uid"], it["item_id"], oid, int(r),
                     request.form.get(f"c{it['item_id']}", "")[:500]))
        flash("⭐ Thanks for your feedback! It helps us serve you better.")
    return redirect(url_for("order_detail", oid=oid))

@app.route("/wallet", methods=["GET", "POST"])
@login_required
def wallet():
    if request.method == "POST":
        try:
            amt = float(request.form.get("amount", 0))
            upi_id = request.form.get("upi_id", "").strip()
            if 10 <= amt <= 5000 and upi_id:
                wallet_add(session["uid"], amt, f"Top-up via UPI ({upi_id})")
                add_notification(session["uid"],
                    f"✅ ₹{amt:.0f} added to your wallet via UPI.", "/wallet")
                flash(f"✓ ₹{amt:.0f} added to your wallet successfully!")
            else:
                flash("Enter a valid amount (₹10–₹5,000) and UPI ID.")
        except (ValueError, TypeError):
            flash("Invalid amount entered.")
        return redirect(url_for("wallet"))
    u = one("SELECT * FROM users WHERE id=?", (session["uid"],))
    txns = many(
        "SELECT * FROM wallet_txn WHERE user_id=? ORDER BY id DESC LIMIT 30",
        (session["uid"],))
    stats = one(
        "SELECT COUNT(*) c, COALESCE(SUM(total),0) t, COALESCE(SUM(discount),0) d "
        "FROM orders WHERE user_id=? AND status!='Cancelled'", (session["uid"],))
    monthly = many(
        "SELECT strftime('%Y-%m', created_at) month, "
        "SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END) spent "
        "FROM wallet_txn WHERE user_id=? GROUP BY month ORDER BY month DESC LIMIT 6",
        (session["uid"],))
    return render_template("wallet.html", u=u, txns=txns, stats=stats, monthly=monthly)

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    u = one("SELECT * FROM users WHERE id=?", (session["uid"],))
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update":
            name = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip()[:20]
            bio = request.form.get("bio", "").strip()[:200]
            if name:
                run("UPDATE users SET name=?, phone=?, bio=? WHERE id=?",
                    (name, phone, bio, session["uid"]))
                session["name"] = name
                flash("✓ Profile updated successfully!")
            else:
                flash("Name cannot be empty.")
        elif action == "password":
            old_pw = request.form.get("old_password", "")
            new_pw = request.form.get("new_password", "")
            if not check_password_hash(u["password"], old_pw):
                flash("Current password is incorrect.")
            elif len(new_pw) < 6:
                flash("New password must be at least 6 characters.")
            else:
                run("UPDATE users SET password=? WHERE id=?",
                    (generate_password_hash(new_pw), session["uid"]))
                flash("✓ Password changed successfully!")
        return redirect(url_for("profile"))
    stats = one(
        "SELECT COUNT(*) c, COALESCE(SUM(total),0) t, COALESCE(SUM(discount),0) d "
        "FROM orders WHERE user_id=? AND status!='Cancelled'", (session["uid"],))
    fav_count = one("SELECT COUNT(*) FROM favorites WHERE user_id=?", (session["uid"],))[0]
    recent = many("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5",
                  (session["uid"],))
    top_items = many(
        "SELECT oi.name, SUM(oi.qty) qty FROM order_items oi "
        "JOIN orders o ON o.id=oi.order_id "
        "WHERE o.user_id=? AND o.status!='Cancelled' "
        "GROUP BY oi.item_id ORDER BY qty DESC LIMIT 5", (session["uid"],))
    return render_template("profile.html", u=u, stats=stats, fav_count=fav_count,
                           recent=recent, top_items=top_items)


# =================== ADMIN ===================
@app.route("/admin")
@admin_required
def admin_home():
    today = date.today().isoformat()
    s = one(
        "SELECT COUNT(*) c, COALESCE(SUM(total),0) t, COALESCE(SUM(discount),0) d "
        "FROM orders WHERE order_date=? AND status!='Cancelled'", (today,))
    pending = one("SELECT COUNT(*) FROM orders WHERE status IN ('Placed','Preparing')")[0]
    students = one("SELECT COUNT(*) FROM users WHERE role='student'")[0]
    low = many("SELECT * FROM menu_items WHERE stock<=? ORDER BY stock", (LOW_STOCK,))
    avg = one("SELECT ROUND(AVG(rating),1) FROM ratings")[0]
    week_days = [(date.today() - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    rev_map = {r["order_date"]: r["t"] for r in many(
        "SELECT order_date, SUM(total) t FROM orders "
        "WHERE status!='Cancelled' GROUP BY order_date")}
    week_rev = [rev_map.get(d, 0) for d in week_days]
    recent_orders = many(
        "SELECT o.*, u.name uname FROM orders o JOIN users u ON u.id=o.user_id "
        "ORDER BY o.id DESC LIMIT 8")
    cat_stats = many(
        "SELECT m.category, SUM(oi.qty) qty, SUM(oi.qty*oi.price) rev "
        "FROM order_items oi JOIN orders o ON o.id=oi.order_id "
        "JOIN menu_items m ON m.id=oi.item_id "
        "WHERE o.order_date=? AND o.status!='Cancelled' "
        "GROUP BY m.category ORDER BY rev DESC", (today,))
    return render_template("admin_home.html", s=s, pending=pending, students=students,
                           low=low, avg=avg, week_days=week_days, week_rev=week_rev,
                           recent_orders=recent_orders, cat_stats=cat_stats)

@app.route("/admin/analytics")
@admin_required
def admin_analytics():
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    rev_map = {r["order_date"]: r["t"] for r in many(
        "SELECT order_date, SUM(total) t FROM orders WHERE status!='Cancelled' GROUP BY order_date")}
    ord_map = {r["order_date"]: r["c"] for r in many(
        "SELECT order_date, COUNT(*) c FROM orders WHERE status!='Cancelled' GROUP BY order_date")}
    top = many(
        "SELECT oi.name, SUM(oi.qty) q, SUM(oi.qty*oi.price) rev "
        "FROM order_items oi JOIN orders o ON o.id=oi.order_id "
        "WHERE o.status!='Cancelled' GROUP BY oi.name ORDER BY q DESC LIMIT 8")
    hours = many(
        "SELECT strftime('%H', created_at) h, COUNT(*) c FROM orders "
        "WHERE status!='Cancelled' GROUP BY h ORDER BY h")
    pay = many(
        "SELECT payment_method m, COUNT(*) c, COALESCE(SUM(total),0) t "
        "FROM orders WHERE status!='Cancelled' GROUP BY m")
    cat_rev = many(
        "SELECT m.category, SUM(oi.qty*oi.price) rev FROM order_items oi "
        "JOIN orders o ON o.id=oi.order_id JOIN menu_items m ON m.id=oi.item_id "
        "WHERE o.status!='Cancelled' GROUP BY m.category ORDER BY rev DESC")
    top_rated = many(
        "SELECT m.name, ROUND(AVG(r.rating),1) avg, COUNT(*) n "
        "FROM ratings r JOIN menu_items m ON m.id=r.item_id "
        "GROUP BY r.item_id HAVING n>=1 ORDER BY avg DESC LIMIT 6")
    total_students = one("SELECT COUNT(*) FROM users WHERE role='student'")[0]
    total_revenue = one("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='Cancelled'")[0]
    total_orders = one("SELECT COUNT(*) FROM orders WHERE status!='Cancelled'")[0]
    data = dict(
        days=days, revenue=[rev_map.get(d, 0) for d in days],
        orders_count=[ord_map.get(d, 0) for d in days],
        top_names=[r["name"] for r in top], top_qty=[r["q"] for r in top],
        hours=[f"{r['h']}:00" for r in hours], hour_counts=[r["c"] for r in hours],
        pay_names=[r["m"] for r in pay], pay_counts=[r["c"] for r in pay],
        cat_names=[r["category"] for r in cat_rev], cat_rev=[r["rev"] for r in cat_rev],
        rated_names=[r["name"] for r in top_rated],
        rated_avg=[r["avg"] for r in top_rated],
    )
    return render_template("admin_analytics.html", data=data,
                           total_students=total_students,
                           total_revenue=total_revenue, total_orders=total_orders)

@app.route("/admin/menu", methods=["GET", "POST"])
@admin_required
def admin_menu():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Item name is required.")
            return redirect(url_for("admin_menu"))
        try:
            price = float(request.form.get("price", 0))
            stock = int(request.form.get("stock", 0))
            if price < 0 or stock < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("Invalid price or stock value.")
            return redirect(url_for("admin_menu"))
        run("INSERT INTO menu_items(name,category,price,stock,veg,description) VALUES(?,?,?,?,?,?)",
            (name, request.form.get("category", "Snacks"), price, stock,
             1 if request.form.get("veg") else 0, request.form.get("description", "")[:300]))
        flash(f"✓ '{name}' added to menu.")
        return redirect(url_for("admin_menu"))
    items = many("SELECT * FROM menu_items ORDER BY category, name")
    all_cats = ["Snacks", "Meals", "Beverages", "Desserts"]
    existing_cats = [r[0] for r in many("SELECT DISTINCT category FROM menu_items")]
    categories = sorted(set(all_cats + existing_cats))
    return render_template("admin_menu.html", items=items, low=LOW_STOCK, categories=categories)

@app.route("/admin/menu/edit/<int:iid>", methods=["GET", "POST"])
@admin_required
def admin_edit(iid):
    it = one("SELECT * FROM menu_items WHERE id=?", (iid,))
    if not it:
        flash("Item not found.")
        return redirect(url_for("admin_menu"))
    if request.method == "POST":
        try:
            price = float(request.form.get("price", 0))
            stock = int(request.form.get("stock", 0))
            if price < 0 or stock < 0:
                raise ValueError
        except (ValueError, TypeError):
            flash("Invalid price or stock value.")
            return render_template("admin_edit.html", it=it)
        run("UPDATE menu_items SET name=?,category=?,price=?,stock=?,veg=?,description=?,available=? WHERE id=?",
            (request.form.get("name", "").strip(), request.form.get("category", "Snacks"),
             price, stock, 1 if request.form.get("veg") else 0,
             request.form.get("description", "")[:300], 1 if stock > 0 else 0, iid))
        flash("✓ Item updated successfully.")
        return redirect(url_for("admin_menu"))
    return render_template("admin_edit.html", it=it)

@app.route("/admin/menu/restock/<int:iid>", methods=["POST"])
@admin_required
def admin_restock(iid):
    try:
        add = int(request.form.get("add", 0))
        if add <= 0:
            raise ValueError
    except (ValueError, TypeError):
        flash("Invalid stock amount.")
        return redirect(url_for("admin_menu"))
    run("UPDATE menu_items SET stock=stock+?, available=1 WHERE id=?", (add, iid))
    item = one("SELECT name FROM menu_items WHERE id=?", (iid,))
    flash(f"✓ Restocked '{item['name']}' by +{add} units.")
    return redirect(url_for("admin_menu"))

@app.route("/admin/menu/toggle/<int:iid>", methods=["POST"])
@admin_required
def admin_toggle(iid):
    run("UPDATE menu_items SET available=1-available WHERE id=?", (iid,))
    return redirect(url_for("admin_menu"))

@app.route("/admin/menu/delete/<int:iid>", methods=["POST"])
@admin_required
def admin_delete(iid):
    item = one("SELECT name FROM menu_items WHERE id=?", (iid,))
    if item:
        run("DELETE FROM menu_items WHERE id=?", (iid,))
        run("DELETE FROM favorites WHERE item_id=?", (iid,))
        flash(f"'{item['name']}' removed from menu.")
    return redirect(url_for("admin_menu"))

@app.route("/admin/orders")
@admin_required
def admin_orders():
    q = request.args.get("token", "").strip().upper()
    st = request.args.get("status", "")
    date_filter = request.args.get("date", "")
    sql = ("SELECT o.*, u.name uname, u.college_id FROM orders o "
           "JOIN users u ON u.id=o.user_id WHERE 1=1")
    args = []
    if q:
        sql += " AND o.token LIKE ?"; args.append(f"%{q}%")
    if st:
        sql += " AND o.status=?"; args.append(st)
    if date_filter:
        sql += " AND o.order_date=?"; args.append(date_filter)
    rows = many(sql + " ORDER BY o.id DESC LIMIT 100", args)
    items = {}
    if rows:
        ids = tuple(o["id"] for o in rows)
        placeholders = ",".join("?" * len(ids))
        for oi in many(f"SELECT * FROM order_items WHERE order_id IN ({placeholders})", ids):
            items.setdefault(oi["order_id"], []).append(f"{oi['name']} ×{oi['qty']}")
    return render_template("admin_orders.html", orders=rows, statuses=STATUSES,
                           q=q, st=st, date_filter=date_filter, items=items)

@app.route("/admin/orders/<int:oid>/status", methods=["POST"])
@admin_required
def admin_status(oid):
    new_st = request.form.get("status", "")
    if new_st not in STATUSES:
        return redirect(request.referrer or url_for("admin_orders"))
    o = one("SELECT * FROM orders WHERE id=? AND status!='Cancelled'", (oid,))
    if o:
        old_idx = STATUSES.index(o["status"]) if o["status"] in STATUSES else -1
        new_idx = STATUSES.index(new_st)
        if new_idx >= old_idx:  # Only forward transitions
            run("UPDATE orders SET status=? WHERE id=?", (new_st, oid))
            if new_st == "Collected":
                run("UPDATE orders SET payment_status='Paid' WHERE id=? "
                    "AND payment_status='Pending'", (oid,))
            if new_st == "Ready":
                add_notification(o["user_id"],
                    f"🔔 Order {o['token']} is READY for pickup at {o['pickup_slot']}!",
                    f"/order/{oid}")
            elif new_st == "Collected":
                add_notification(o["user_id"],
                    f"✅ Order {o['token']} collected. Please rate your food!",
                    f"/order/{oid}")
    return redirect(request.referrer or url_for("admin_orders"))

@app.route("/admin/orders/<int:oid>/cancel", methods=["POST"])
@admin_required
def admin_cancel(oid):
    o = one("SELECT * FROM orders WHERE id=?", (oid,))
    if cancel_order(oid):
        if o:
            add_notification(o["user_id"],
                f"Your order {o['token']} was cancelled by the canteen admin.",
                f"/order/{oid}")
        flash("Order cancelled and amount refunded to wallet (if applicable).")
    else:
        flash("Only 'Placed' orders can be cancelled.")
    return redirect(request.referrer or url_for("admin_orders"))

@app.route("/admin/coupons", methods=["GET", "POST"])
@admin_required
def admin_coupons():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        try:
            percent = int(request.form.get("percent", 0))
            min_order = float(request.form.get("min_order", 0))
            if not (1 <= percent <= 100):
                flash("Discount percent must be between 1 and 100.")
                return redirect(url_for("admin_coupons"))
        except (ValueError, TypeError):
            flash("Invalid discount value.")
            return redirect(url_for("admin_coupons"))
        try:
            run("INSERT INTO coupons(code,percent,min_order) VALUES(?,?,?)",
                (code, percent, min_order))
            flash(f"✓ Coupon '{code}' created.")
        except sqlite3.IntegrityError:
            flash("Coupon code already exists.")
        return redirect(url_for("admin_coupons"))
    coupons = many(
        "SELECT c.*, (SELECT COUNT(*) FROM orders o WHERE o.coupon=c.code AND o.status!='Cancelled') uses "
        "FROM coupons c ORDER BY c.id DESC")
    return render_template("admin_coupons.html", coupons=coupons)

@app.route("/admin/coupons/toggle/<int:cid>", methods=["POST"])
@admin_required
def coupon_toggle(cid):
    run("UPDATE coupons SET active=1-active WHERE id=?", (cid,))
    return redirect(url_for("admin_coupons"))

@app.route("/admin/coupons/delete/<int:cid>", methods=["POST"])
@admin_required
def coupon_delete(cid):
    run("DELETE FROM coupons WHERE id=?", (cid,))
    flash("Coupon deleted.")
    return redirect(url_for("admin_coupons"))

@app.route("/admin/students")
@admin_required
def admin_students():
    q = request.args.get("q", "").strip()
    sql = ("SELECT u.*, "
           "(SELECT COUNT(*) FROM orders o WHERE o.user_id=u.id AND o.status!='Cancelled') n, "
           "(SELECT COALESCE(SUM(total),0) FROM orders o WHERE o.user_id=u.id AND o.status!='Cancelled') spent "
           "FROM users u WHERE role='student'")
    args = []
    if q:
        sql += " AND (u.name LIKE ? OR u.college_id LIKE ? OR u.email LIKE ?)"
        args.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
    sql += " ORDER BY spent DESC"
    rows = many(sql, args)
    return render_template("admin_students.html", students=rows, q=q)

@app.route("/admin/students/<int:uid>/credit", methods=["POST"])
@admin_required
def admin_credit(uid):
    try:
        amt = float(request.form.get("amount", 0))
        if not (0 < amt <= 5000):
            raise ValueError
    except (ValueError, TypeError):
        flash("Invalid amount.")
        return redirect(url_for("admin_students"))
    u = one("SELECT name FROM users WHERE id=?", (uid,))
    wallet_add(uid, amt, "Credit by admin (cash deposit)")
    add_notification(uid, f"₹{amt:.0f} has been credited to your wallet by admin.", "/wallet")
    flash(f"✓ ₹{amt:.0f} credited to {u['name']}'s wallet.")
    return redirect(url_for("admin_students"))

@app.route("/admin/feedback")
@admin_required
def admin_feedback():
    item_filter = request.args.get("item", "")
    sql = ("SELECT r.*, u.name uname, m.name iname FROM ratings r "
           "JOIN users u ON u.id=r.user_id LEFT JOIN menu_items m ON m.id=r.item_id")
    args = []
    if item_filter:
        sql += " WHERE r.item_id=?"; args.append(item_filter)
    rows = many(sql + " ORDER BY r.id DESC LIMIT 100", args)
    avgs = many(
        "SELECT m.id, m.name, ROUND(AVG(r.rating),1) a, COUNT(*) n, "
        "SUM(CASE WHEN r.rating>=4 THEN 1 ELSE 0 END) positive "
        "FROM ratings r JOIN menu_items m ON m.id=r.item_id GROUP BY m.id ORDER BY a DESC")
    menu_items_list = many("SELECT id, name FROM menu_items ORDER BY name")
    return render_template("admin_feedback.html", rows=rows, avgs=avgs,
                           menu_items_list=menu_items_list, item_filter=item_filter)

def report_data(day):
    s = one("SELECT COUNT(*) c, COALESCE(SUM(total),0) t, COALESCE(SUM(discount),0) d "
            "FROM orders WHERE order_date=? AND status!='Cancelled'", (day,))
    top = many("SELECT oi.name, SUM(oi.qty) qty, SUM(oi.qty*oi.price) revenue "
               "FROM order_items oi JOIN orders o ON o.id=oi.order_id "
               "WHERE o.order_date=? AND o.status!='Cancelled' "
               "GROUP BY oi.name ORDER BY qty DESC", (day,))
    pay_breakdown = many(
        "SELECT payment_method, COUNT(*) c, COALESCE(SUM(total),0) t "
        "FROM orders WHERE order_date=? AND status!='Cancelled' GROUP BY payment_method", (day,))
    return s, top, pay_breakdown

@app.route("/admin/report")
@admin_required
def admin_report():
    day = request.args.get("date", date.today().isoformat())
    s, top, pay_breakdown = report_data(day)
    cancelled = one("SELECT COUNT(*) FROM orders WHERE order_date=? AND status='Cancelled'",
                    (day,))[0]
    hourly = many(
        "SELECT strftime('%H', created_at) h, COUNT(*) c, COALESCE(SUM(total),0) t "
        "FROM orders WHERE order_date=? AND status!='Cancelled' GROUP BY h ORDER BY h", (day,))
    return render_template("admin_report.html", s=s, top=top, day=day, cancelled=cancelled,
                           pay_breakdown=pay_breakdown, hourly=hourly)

@app.route("/admin/report.csv")
@admin_required
def admin_report_csv():
    day = request.args.get("date", date.today().isoformat())
    s, top, _ = report_data(day)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"Smart Canteen — Sales Report for {day}"])
    w.writerow([f"Total Orders: {s['c']}", f"Revenue: Rs.{s['t']:.2f}", f"Discounts: Rs.{s['d']:.2f}"])
    w.writerow([])
    w.writerow(["Item", "Qty Sold", "Revenue"])
    for t in top:
        name = str(t["name"])
        if name and name[0] in ("=", "+", "-", "@"):
            name = "'" + name  # Prevent formula injection
        w.writerow([name, t["qty"], f"Rs.{t['revenue']:.2f}"])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=sales_{day}.csv"})

@app.route("/admin/announcements", methods=["GET", "POST"])
@admin_required
def admin_announcements():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        atype = request.form.get("type", "info")
        if not title or not content:
            flash("Title and content are required.")
        else:
            run("INSERT INTO announcements(title,content,type) VALUES(?,?,?)",
                (title, content, atype))
            flash(f"✓ Announcement '{title}' posted.")
        return redirect(url_for("admin_announcements"))
    announcements = many("SELECT * FROM announcements ORDER BY id DESC")
    return render_template("admin_announcements.html", announcements=announcements)

@app.route("/admin/announcements/<int:aid>/toggle", methods=["POST"])
@admin_required
def announcement_toggle(aid):
    run("UPDATE announcements SET active=1-active WHERE id=?", (aid,))
    return redirect(url_for("admin_announcements"))

@app.route("/admin/announcements/<int:aid>/delete", methods=["POST"])
@admin_required
def announcement_delete(aid):
    run("DELETE FROM announcements WHERE id=?", (aid,))
    flash("Announcement deleted.")
    return redirect(url_for("admin_announcements"))


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG", "1") == "1")
