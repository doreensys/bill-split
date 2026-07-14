from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import database as db

app = Flask(__name__)
app.secret_key = "dev-secret-change-this"  # replace before deploying anywhere real

db.init_db()


@app.route("/")
def index():
    return render_template("index.html")


# ---------- CREATE ROOM ----------

@app.route("/create", methods=["GET", "POST"])
def create_room():
    if request.method == "GET":
        return render_template("create_room.html")

    data = request.get_json()
    host_name = data.get("host_name", "").strip()
    items = data.get("items", [])
    tax = float(data.get("tax") or 0)
    tip = float(data.get("tip") or 0)

    if not host_name or not items:
        return jsonify({"error": "Name and at least one item are required"}), 400

    code, host_id = db.create_room(host_name, items, tax, tip)
    session["user_id"] = host_id
    session["room_id"] = code
    session["name"] = host_name

    return jsonify({"room_id": code, "redirect": url_for("select_items", code=code)})


# ---------- JOIN ROOM ----------

@app.route("/join", methods=["GET", "POST"])
def join_room():
    if request.method == "GET":
        prefill = request.args.get("code", "")
        return render_template("join_room.html", prefill=prefill)

    data = request.get_json()
    code = data.get("room_id", "").strip().upper()
    name = data.get("name", "").strip()

    if not db.room_exists(code):
        return jsonify({"error": "Room not found. Check the code."}), 404
    if not name:
        return jsonify({"error": "Enter your name"}), 400

    user_id = db.add_user(code, name)
    session["user_id"] = user_id
    session["room_id"] = code
    session["name"] = name

    return jsonify({"redirect": url_for("select_items", code=code)})


# ---------- ITEM SELECTION ----------

@app.route("/room/<code>")
def select_items(code):
    if not db.room_exists(code):
        return redirect(url_for("index"))
    if session.get("room_id") != code:
        return redirect(url_for("join_room", code=code))
    return render_template(
        "select_items.html",
        code=code,
        user_id=session.get("user_id"),
        name=session.get("name"),
    )


@app.route("/api/room/<code>/state")
def room_state(code):
    state = db.get_room_state(code)
    if not state:
        return jsonify({"error": "Room not found"}), 404
    return jsonify(state)


@app.route("/api/room/<code>/toggle", methods=["POST"])
def toggle_item(code):
    data = request.get_json()
    user_id = session.get("user_id")
    item_id = data.get("item_id")
    if not user_id:
        return jsonify({"error": "Not joined to this room"}), 403
    selected = db.toggle_selection(user_id, item_id)
    return jsonify({"selected": selected})


# ---------- FINAL BILL ----------

@app.route("/room/<code>/bill")
def final_bill(code):
    if not db.room_exists(code):
        return redirect(url_for("index"))
    return render_template("final_bill.html", code=code)


@app.route("/api/room/<code>/paid", methods=["POST"])
def report_amount_paid(code):
    data = request.get_json()
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Not joined to this room"}), 403
    try:
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid amount"}), 400
    db.set_amount_paid(user_id, amount)
    return jsonify({"ok": True})


@app.route("/api/room/<code>/bill")
def bill_data(code):
    result = db.calculate_split(code)
    if not result:
        return jsonify({"error": "Room not found"}), 404
    return jsonify(result)


if __name__ == "__main__":
    app.run(debug=True, port=5000)