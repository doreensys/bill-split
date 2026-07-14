import sqlite3
import random
import string
from datetime import datetime

DB_PATH = "billsplit.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS rooms (
        room_id TEXT PRIMARY KEY,
        created_by TEXT,
        tax REAL DEFAULT 0,
        tip REAL DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id TEXT NOT NULL,
        name TEXT NOT NULL,
        amount_paid REAL DEFAULT 0,
        FOREIGN KEY (room_id) REFERENCES rooms(room_id)
    );

    CREATE TABLE IF NOT EXISTS items (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id TEXT NOT NULL,
        item_name TEXT NOT NULL,
        price REAL NOT NULL,
        FOREIGN KEY (room_id) REFERENCES rooms(room_id)
    );

    CREATE TABLE IF NOT EXISTS selections (
        user_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, item_id),
        FOREIGN KEY (user_id) REFERENCES users(user_id),
        FOREIGN KEY (item_id) REFERENCES items(item_id)
    );
    """)
    conn.commit()
    conn.close()


def generate_room_code():
    """Generates a code like ABCD12, retries if it's already taken."""
    conn = get_db()
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4)) + "".join(
            random.choices(string.digits, k=2)
        )
        exists = conn.execute(
            "SELECT 1 FROM rooms WHERE room_id = ?", (code,)
        ).fetchone()
        if not exists:
            conn.close()
            return code


def create_room(host_name, items, tax=0, tip=0):
    """items = [{"name": "Pizza", "price": 60}, ...]"""
    conn = get_db()
    code = generate_room_code()
    conn.execute(
        "INSERT INTO rooms (room_id, created_by, tax, tip, created_at) VALUES (?, ?, ?, ?, ?)",
        (code, host_name, tax, tip, datetime.utcnow().isoformat()),
    )
    cur = conn.execute(
        "INSERT INTO users (room_id, name) VALUES (?, ?)", (code, host_name)
    )
    host_id = cur.lastrowid
    for item in items:
        conn.execute(
            "INSERT INTO items (room_id, item_name, price) VALUES (?, ?, ?)",
            (code, item["name"], item["price"]),
        )
    conn.commit()
    conn.close()
    return code, host_id


def set_amount_paid(user_id, amount):
    """Each person self-reports what they personally fronted at the register
    (0 if they didn't pay anything upfront). Multiple people can report
    nonzero amounts -- that's what makes the settle-up worth computing."""
    conn = get_db()
    conn.execute(
        "UPDATE users SET amount_paid = ? WHERE user_id = ?", (amount, user_id)
    )
    conn.commit()
    conn.close()


def room_exists(code):
    conn = get_db()
    row = conn.execute("SELECT 1 FROM rooms WHERE room_id = ?", (code,)).fetchone()
    conn.close()
    return row is not None


def add_user(code, name):
    conn = get_db()
    cur = conn.execute("INSERT INTO users (room_id, name) VALUES (?, ?)", (code, name))
    conn.commit()
    user_id = cur.lastrowid
    conn.close()
    return user_id


def toggle_selection(user_id, item_id):
    conn = get_db()
    existing = conn.execute(
        "SELECT 1 FROM selections WHERE user_id = ? AND item_id = ?",
        (user_id, item_id),
    ).fetchone()
    if existing:
        conn.execute(
            "DELETE FROM selections WHERE user_id = ? AND item_id = ?",
            (user_id, item_id),
        )
        selected = False
    else:
        conn.execute(
            "INSERT INTO selections (user_id, item_id) VALUES (?, ?)",
            (user_id, item_id),
        )
        selected = True
    conn.commit()
    conn.close()
    return selected


def get_room_state(code):
    conn = get_db()
    room = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (code,)).fetchone()
    if not room:
        conn.close()
        return None

    users = conn.execute(
        "SELECT user_id, name, amount_paid FROM users WHERE room_id = ?", (code,)
    ).fetchall()
    items = conn.execute(
        "SELECT item_id, item_name, price FROM items WHERE room_id = ?", (code,)
    ).fetchall()
    selections = conn.execute(
        """SELECT s.user_id, s.item_id FROM selections s
           JOIN items i ON s.item_id = i.item_id
           WHERE i.room_id = ?""",
        (code,),
    ).fetchall()
    conn.close()

    sel_by_item = {}
    for s in selections:
        sel_by_item.setdefault(s["item_id"], []).append(s["user_id"])

    return {
        "room_id": room["room_id"],
        "tax": room["tax"],
        "tip": room["tip"],
        "users": [
            {"user_id": u["user_id"], "name": u["name"], "amount_paid": u["amount_paid"]}
            for u in users
        ],
        "items": [
            {
                "item_id": i["item_id"],
                "name": i["item_name"],
                "price": i["price"],
                "selected_by": sel_by_item.get(i["item_id"], []),
            }
            for i in items
        ],
    }


def calculate_split(code):
    """Splits each item evenly among the people who selected it.
    Tax/tip are distributed proportionally to each person's subtotal.
    Items nobody selected are flagged so the group notices."""
    state = get_room_state(code)
    if not state:
        return None

    user_totals = {u["user_id"]: 0.0 for u in state["users"]}
    unclaimed_items = []
    subtotal = 0.0

    for item in state["items"]:
        subtotal += item["price"]
        n = len(item["selected_by"])
        if n == 0:
            unclaimed_items.append(item["name"])
            continue
        share = item["price"] / n
        for uid in item["selected_by"]:
            if uid in user_totals:
                user_totals[uid] += share

    tax_tip = (state["tax"] or 0) + (state["tip"] or 0)
    grand_total = subtotal + tax_tip
    total_fronted = sum(u["amount_paid"] or 0 for u in state["users"])

    breakdown = []
    net_balances = []  # what this app actually decides: who owes who, minimized
    for u in state["users"]:
        base = user_totals[u["user_id"]]
        proportional_extra = (base / subtotal * tax_tip) if subtotal > 0 else 0
        share = base + proportional_extra
        amount_fronted = u["amount_paid"] or 0
        breakdown.append(
            {
                "name": u["name"],
                "subtotal": round(base, 2),
                "tax_tip_share": round(proportional_extra, 2),
                "total": round(share, 2),
                "amount_paid": round(amount_fronted, 2),
            }
        )
        # positive net = this person still owes money into the group (debtor)
        # negative net = this person is owed money back (creditor)
        net_balances.append({"name": u["name"], "net": share - amount_fronted})

    settlements = simplify_debts(net_balances)

    return {
        "breakdown": breakdown,
        "unclaimed_items": unclaimed_items,
        "subtotal": round(subtotal, 2),
        "tax": state["tax"],
        "tip": state["tip"],
        "grand_total": round(grand_total, 2),
        "total_fronted": round(total_fronted, 2),
        "settlements": settlements,
    }


def simplify_debts(net_balances):
    """Takes each person's net balance (positive = owes, negative = owed) and
    returns the minimum-ish set of payments to settle everyone up.

    This is the classic 'debt simplification' problem: finding the true
    minimum number of transactions is NP-hard, so we use the standard
    greedy heuristic (same approach Splitwise uses) -- repeatedly match
    the biggest creditor with the biggest debtor.
    """
    creditors = [
        [b["name"], -b["net"]] for b in net_balances if b["net"] < -0.01
    ]
    debtors = [
        [b["name"], b["net"]] for b in net_balances if b["net"] > 0.01
    ]
    creditors.sort(key=lambda x: x[1], reverse=True)
    debtors.sort(key=lambda x: x[1], reverse=True)

    settlements = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        debtor_name, debt_amt = debtors[i]
        creditor_name, credit_amt = creditors[j]
        payment = round(min(debt_amt, credit_amt), 2)

        if payment > 0.01:
            settlements.append(
                {"from": debtor_name, "to": creditor_name, "amount": payment}
            )

        debtors[i][1] -= payment
        creditors[j][1] -= payment

        if debtors[i][1] < 0.01:
            i += 1
        if creditors[j][1] < 0.01:
            j += 1

    return settlements