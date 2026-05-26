import socket
import select
import os
import json
import sqlite3
import threading
from queue import Queue

SOCKET_PATH = '/tmp/system_hub.sock'
DB_TELEM = '/home/justi/telemetry.db'

db_queue = Queue()
req_queue = Queue()
response_queue = Queue()  # db_worker puts responses here; main loop sends them safely

def db_worker():
    conn = sqlite3.connect(DB_TELEM, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS UserLocation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lon REAL,
            lat REAL,
            source TEXT,
            created_at datetime DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS MarkedLocation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            lon REAL,
            lat REAL,
            info TEXT,
            source TEXT,
            type TEXT,
            created_at datetime DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()

    while True:
        if not db_queue.empty():
            query, params = db_queue.get()
            try:
                conn.execute(query, params)
                conn.commit()
            except Exception as e:
                print(f"DB Write error: {e}")
            db_queue.task_done()
            
        if not req_queue.empty():
            client_socket, query, params, req_id = req_queue.get()
            try:
                cursor = conn.cursor()
                cursor.execute(query, params)
                rows = cursor.fetchall()
                columns = [d[0] for d in cursor.description]
                result = [dict(zip(columns, row)) for row in rows]
                
                response = {
                    "topic": "db/response",
                    "req_id": req_id,
                    "data": result
                }
                response_queue.put((client_socket, json.dumps(response).encode('utf-8') + b"\n"))
            except Exception as e:
                try:
                    err_resp = {"topic": "db/response", "req_id": req_id, "error": str(e)}
                    response_queue.put((client_socket, json.dumps(err_resp).encode('utf-8') + b"\n"))
                except:
                    pass
            req_queue.task_done()
            
        threading.Event().wait(0.001)

threading.Thread(target=db_worker, daemon=True).start()

if os.path.exists(SOCKET_PATH):
    os.chmod(SOCKET_PATH, 0o666)

def main():
    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(15)
    server.setblocking(False)

    sockets_list = [server]
    clients = {}

    print(f"UDS server started: {SOCKET_PATH}")

    while True:
        read_sockets, _, _ = select.select(sockets_list, [], [], 0.05)

        # Drain db responses queued by db_worker thread - safe
        while not response_queue.empty():
            try:
                resp_sock, resp_data = response_queue.get_nowait()
                if resp_sock in clients:
                    try:
                        resp_sock.sendall(resp_data)
                    except BlockingIOError:
                        pass  # client buffer full, drop this response
                    except Exception:
                        pass
            except Exception:
                pass

        for notified_socket in read_sockets:
            if notified_socket == server:
                client_socket, _ = server.accept()
                print("[UDS] New client connected")
                client_socket.setblocking(False)
                sockets_list.append(client_socket)
                clients[client_socket] = b""
            else:
                try:
                    data = notified_socket.recv(4096)

                    if not data:
                        print("[UDS] Client disconnected")

                        if notified_socket in sockets_list:
                            sockets_list.remove(notified_socket)
                        if notified_socket in clients:
                            del clients[notified_socket]
                        continue
                    print(f"[UDS] RAW DATA: {data}")
                    clients[notified_socket] += data

                    while b"\n" in clients[notified_socket]:
                        line, clients[notified_socket] = clients[notified_socket].split(b"\n", 1)
                        if not line.strip():
                            continue
                        print(f"[UDS] LINE: {line}")
                        try:
                            msg = json.loads(line.decode('utf-8'))

                        except Exception as e:
                            print("[UDS] JSON ERROR:", e)
                            continue

                        print(f"[UDS] MESSAGE: {msg}")
                        topic = msg.get("topic")
                        payload = msg.get("data")
                        req_id = msg.get("req_id")
                        print(f"[UDS] TOPIC: {topic}")

                        if isinstance(payload, str):
                            try:
                                payload = json.loads(payload)
                            except json.JSONDecodeError:
                                print(f"[UDS] Error: Payload is a string but not valid JSON: {payload}")
                                continue

                        print(f"[UDS] PAYLOAD: {payload}")
                        if topic in ["location/gnss", "location/dr"]:
                            print("[UDS] LOCATION UPDATE RECEIVED")
                            db_queue.put(("INSERT INTO UserLocation (lon, lat, source) VALUES (?, ?, ?)", (
                                payload['lon'], payload['lat'], topic.split('/')[-1])))

                        elif topic in ["marks/local", "marks/cloud"] or topic == "button/loc":
                            print("[UDS] MARK RECEIVED")
                            long_val = payload.get('lon', 0.0)
                            lat_val = payload.get('lat', 0.0)
                            name_val = payload.get('name', 'Emergency mark')
                            info_val = payload.get('info', 'Quick action mark')
                            incoming_type = payload.get('type', 'unclassified')

                            if incoming_type not in ['markedImportant', 'markedDangerous', 'unclassified']:
                                type_val = 'unclassified'
                            else:
                                type_val = incoming_type

                            if topic == "marks/cloud":
                                source_val = "monitor"
                            else:
                                source_val = "driver"

                            if topic == "button/loc":
                                type_val = "markEmergency"

                            db_queue.put(("INSERT INTO MarkedLocation (name, lon, lat, info, source, type) VALUES (?, ?, ?, ?, ?, ?)",
                                (name_val, long_val, lat_val, info_val, source_val, type_val)
                            ))

                        elif topic == "button/del":
                            print("[UDS] DELETE REQUEST")
                            db_queue.put(("DELETE FROM UserLocation", ()))
                            db_queue.put(("DELETE FROM MarkedLocation WHERE source = 'driver'",()))

                        elif topic == "db/query":
                            print("[UDS] DB QUERY")
                            sql_cmd = payload.get("query")
                            sql_params = payload.get("params", ())
                            if sql_cmd.strip().upper().startswith("SELECT"):
                                req_queue.put((
                                    notified_socket,
                                    sql_cmd,
                                    sql_params,
                                    req_id
                                ))
                                continue
                        raw_broadcast = line + b"\n"
                        print(f"[UDS] Broadcasting to {len(clients) - 1} clients")

                        for client in clients:
                            if client != notified_socket:
                                try:
                                    client.sendall(raw_broadcast)
                                    print("[UDS] Broadcast OK")
                                except BlockingIOError:
                                    pass  # client receive buffer temporarily full — keep connection
                                except Exception as e:
                                    print("[UDS] Broadcast failed:", e)

                except Exception as e:
                    print("[UDS] SOCKET ERROR:", e)
                    if notified_socket in sockets_list:
                        sockets_list.remove(notified_socket)
                    if notified_socket in clients:
                        del clients[notified_socket]

if __name__ == "__main__":
    main()