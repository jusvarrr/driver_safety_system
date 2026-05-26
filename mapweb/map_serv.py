from flask import Flask, g, json, jsonify, abort, Response, current_app, render_template, send_file, request
from flask_cors import CORS
from flask_sock import Sock
import socket
import os
import pandas as pd
import sqlite3
import webbrowser
import time
import select

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

COMPUTER_IP = "10.3.141.1"
app = Flask(__name__)
CORS(app)
app.config['JSON_SORT_KEYS'] = False
sock = Sock(app)
DB_FILE = './tiles.mbtiles'

HUB_SOCKET_PATH='/tmp/system_hub.sock'

def send_to_hub(topic, data):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(HUB_SOCKET_PATH)
        payload = {"topic": topic, "data": data}
        s.sendall((json.dumps(payload) + "\n").encode('utf-8'))
        s.close()
        return True
    except:
        return False
    
@app.route('/manual_correct', methods=['POST'])
def manual_correct():
    payload = request.get_json()
    if not payload:
        return jsonify({"status": "error", "message": "Missing configuration data"}), 400
    
    data = {"lon": payload.get("lon"),
            "lat": payload.get("lat")
        }
    
    try:
        if send_to_hub("location/manual_correction", data):
            return jsonify({"status": "success", "message": "Manual correction broadcasted"})
        else:
            return jsonify({"status": "error", "message": "Failed to send to system hub"}), 500
        
    except socket.error as e:
        print(f"UDS connection failure: {e}")
        return jsonify({"status": "error", "message": "UDS Broker unreachable"}), 500
    
@app.route('/off_manual_correct', methods=['POST'])
def off_manual_correct():    
    data = {}
    try:
        if send_to_hub("location/manual_correction_off", data):
            return jsonify({"status": "ok", "message": "Manual correction off broadcasted"})
        else:
            return jsonify({"status": "error", "message": "Failed to send to system hub"}), 500
        
    except socket.error as e:
        print(f"UDS connection failure: {e}")
        return jsonify({"status": "error", "message": "UDS Broker unreachable"}), 500
    
@app.route('/set_mode', methods=['POST'])
def set_mode():
    payload = request.get_json()
    mode = payload.get("mode", "GPS") 
    success = send_to_hub("system/gps_mode", mode)
    
    if success:
        return jsonify({"status": "success", "mode": mode})
    return jsonify({"status": "error", "message": "Hub unreachable"}), 500

@app.route("/mark", methods=["POST"])
def save_mark():
    data = request.get_json()
    
    try:
        send_to_hub("marks/local", data)
    except Exception as e:
        print(e)

    return jsonify({"status": "ok"})

@app.before_request
def before_request():
    g.db = sqlite3.connect(DB_FILE)

@app.teardown_request
def teardown_request(exception):
    if hasattr(g, 'db'):
        g.db.close()

@app.route("/")
def index():
    return render_template("app.html")

@app.route("/style.json")
def styles():
    base_url = f"http://{COMPUTER_IP}:5000"
    with open("./styles/styles/basic-v8.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return jsonify(data)
    
@app.route("/tiles.json")
def tiles():
    with open(r"./jsons/tiles.json", "r", encoding = "utf-8") as f:
        data = json.load(f)
    return jsonify(data)

@app.route("/sprites.json")
def sprites():
    with open(r"styles/sprites/basic-v8.json", "r", encoding = "utf-8") as f:
        data = json.load(f)
    return jsonify(data)

@app.route("/fonts/<fontstack>/<range>")
def query_glyphs(fontstack, range):
    with open(f"./styles/glyphs/{fontstack}/{range}.pbf", "rb") as f:
        data = f.read()
    return Response(data, mimetype="application/x-protobuf")

@app.route("/sprites.png")
def sprites_img():
    file = "./styles/sprites/basic-v8.png"
    return send_file(file, mimetype='image/png', headers={"Content-Encoding": "gzip"})

@app.route("/tiles/<int:z>/<int:x>/<int:y>.pbf")
def query_tile(z, x, y):
    start_time = time.time()
    query = 'SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?;'
    tms_y = 2**z - 1 - y
    cur = g.db.execute(query, (z, x, tms_y))
    result = cur.fetchone()
    if not result:
        abort(404)
    duration = time.time() - start_time
    print(f"Tile {z}/{x}/{y} fetched in: {duration:.4f} s")
    return Response(result[0], mimetype="application/x-protobuf", headers={"Content-Encoding": "gzip"})

@sock.route('/ws')
def handle_ws(ws):
    print("[WS] Client connected")
    hub_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    try:
        hub_sock.connect(HUB_SOCKET_PATH)
        hub_sock.setblocking(False)
        print("[WS] Connected to broker")
    except Exception as e:
        print("[WS] Failed to connect to broker:", e)
        return
    hub_buffer = b""
    try:
        while True:
            try:
                data = ws.receive(timeout=0.01)
                if data:
                    print("[WS] FROM FRONTEND:", data)
                    try:
                        msg = json.loads(data)
                        topic = msg.get("topic")
                        if topic:
                            send_to_hub(topic, msg.get("data"))

                    except Exception as e:
                        print("[WS] JSON ERROR:", e)

            except:
                pass

            try:
                chunk = hub_sock.recv(4096)
                if not chunk:
                    raise Exception("Broker disconnected")

                hub_buffer += chunk
                while b"\n" in hub_buffer:

                    line, hub_buffer = hub_buffer.split(b"\n", 1)

                    if line.strip():
                        decoded = line.decode('utf-8')
                        print("[WS] TO FRONTEND:", decoded)
                        ws.send(decoded)
            except BlockingIOError:
                pass
            except Exception as e:
                print("[WS] Broker read error:", e)
                break
            time.sleep(0.01)

    except Exception as e:
        print("[WS] Connection closed:", e)

    finally:
        hub_sock.close()
        print("[WS] Closed")

if __name__ == "__main__":
    os.makedirs("templates", exist_ok=True)
    print(f"Serveris paleistas: http://{COMPUTER_IP}:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)