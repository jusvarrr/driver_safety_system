import re
import json
import serial
import time
from websocket import create_connection
import socket
import os
import datetime
from sim7600_base_api import SIM7600BaseApi
from collections import deque
from enum import Enum
import select

class TelemetryControl:

    class ToFlaskType(Enum):
        NONE = 1
        COORDS = 2
        MARK = 3

    def __init__(self):
        self.sim7600 = SIM7600BaseApi()
        #self.dev_id = "RPi-Bakalauras-2026"
        self.moving_speed = 100
        self.send_rate = 30
        self.send_rate_manual = 0
        self.is_send_rate_manual = False

        self.hub_socket_path = '/tmp/system_hub.sock'
        self.hub_sock = None
        self.hub_buffer = b""

        self.map_server_url = '10.3.141.1'
        self.map_server_port = 5000
        self.monitor_system_api = '172.161.129.0'
        self.mark_queue = deque()
        self.location_queue = deque()

        self.current_location_json = {}
        self.current_long = 0
        self.current_lat = 0
        self.location_valid = False
        
        self.timeout_cnt = 0
        self.error_cnt = 0

        self.last_updated_to_maps = 0
        self.last_updated_to_cloud = 0

        self.gnss_signal = 0
        self.notified_no_gnss = False

        self.last_creg_check = 0
        self.last_reconnect_try = 0
        self.mqtt_needs_reconnect = False
        self.data_mode = 'NONE'
        self.fallback_to_gnss = True

        self.ws = None

        self.connection_toggle_commanded = False

        self.connect_to_hub()

    def connect_to_hub(self):
        try:
            self.hub_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.hub_sock.connect(self.hub_socket_path)
            self.hub_sock.setblocking(False)
            print("Successfully connected to System Hub Broker.")
        except Exception as e:
            print(f"Failed to connect to Broker, will retry: {e}")
            self.hub_sock = None

    def update_server(self, data_type, payload):
        if self.hub_sock is None:
            self.connect_to_hub()
            if self.hub_sock is None:
                return

        try:
            if data_type == self.ToFlaskType.COORDS:
                if self.location_valid and self.data_mode == 'GPS':
                    self.notified_no_gnss = False
                    msg = {"topic": "conn_stat/gnss", "data": {"state": 1, "lon": self.current_long, "lat": self.current_lat}}
                    self.hub_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))
                    
                    msg = {"topic": "location/gnss", "data": {"lon": self.current_long, "lat": self.current_lat}}
                    self.hub_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))
                else:
                    if not self.notified_no_gnss and not self.location_valid:
                        msg = {"topic": "conn_stat/gnss", "data": {"state": 0, "lon": self.current_long, "lat": self.current_lat}}
                        self.hub_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))
                        self.notified_no_gnss = True
                        print("GNSS Lost. Passing tracking baton to IMU Dead Reckoning.")
                
            elif data_type == self.ToFlaskType.MARK:
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError as e:
                        print(f"Failed to parse mark payload: {e}")
                        return
                if isinstance(payload, dict):
                    if 'long' in payload:
                        payload['lon'] = payload.pop('long')
                    elif 'longitude' in payload:
                        payload['lon'] = payload.pop('longitude')
                
                msg = {"topic": "marks/cloud", "data": payload}
                self.hub_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))

        except BlockingIOError:
            pass
        except Exception as ex: 
            print(f"Failed to send data to hub: {ex}")
            self.hub_sock = None

    def read_uds(self):
        if self.hub_sock is None:
            return
        try:
            r_socks, _, _ = select.select([self.hub_sock], [], [], 0.01)
            if r_socks:
                chunk = self.hub_sock.recv(4096)
                if not chunk:
                    print("Broker disconnected.")
                    self.hub_sock = None
                    return
                
                self.hub_buffer += chunk
                while b"\n" in self.hub_buffer:
                    line, self.hub_buffer = self.hub_buffer.split(b"\n", 1)
                    if line.strip():
                        msg = json.loads(line.decode('utf-8'))
                        topic = msg.get("topic")
                        data = msg.get("data")
                        
                        if topic in ["marks/local", "button/loc"]:
                            print(f"Received new marking event from system ({topic}), pushing to SIM7600 queue...")
                            mark_to_send = json.dumps({
                                "name": data.get('name', 'Emergency mark'),
                                "lon": data.get('lon', self.current_long),
                                "lat": data.get('lat', self.current_lat),
                                "info": data.get('info', 'Quick action mark'),
                                "type": data.get('type', 'markedEmergency')
                            })
                            if data.get('sync_cloud', False) or topic == "button/loc":
                                self.mark_queue.append(mark_to_send)

                        if topic == "location/dr":
                            print(f"Applying DR location update: {data}")
                            self.data_mode = 'MANUAL'

                            self.current_lat = data['lat']
                            self.current_long = data['lon']

                            self.update_server(self.ToFlaskType.COORDS, None)

                        if topic == "location/manual_correction":
                            self.fallback_to_gnss = False

                        if topic == "location/manual_correction_off":
                            self.fallback_to_gnss = True
                            
                        elif topic == "button/cell":
                            self.connection_toggle_commanded = True

        except BlockingIOError:
            pass
        except Exception as e:
            print(f"Error reading from hub: {e}")
            self.hub_sock = None

    def convert_gps(self, input, dir=''):
        pos = input.find('.') - 2
        degrees = input[0:pos]
        minutes = input[pos:]
        result = float(degrees) + (float(minutes) / 60)
        if dir == 'S' or dir == 'W':
            result = result * -1
        return result

    def parse_gps(self, input):
        # This regex makes course optional by using (?:,(?P<course>[\d.]+))?
        regex = r"\+CGPSINFO: (?P<lat>[\d.]+),(?P<latdir>[NS]),(?P<long>[\d.]+),(?P<longdir>[EW]),(?P<date>\d+),(?P<time>[\d.]+),(?P<alt>[\d.]+),(?P<speed>[\d.]+)(?:,(?P<course>[\d.]+))?"
        
        matches = re.finditer(regex, input)
        match_list = list(matches)

        if not match_list:
            self.location_valid = False
            return {}  # preserve last known position so DR anchor stays valid
        else:
            match = match_list[0]
            now = datetime.datetime.now()
            
            lat_val = self.convert_gps(match.group('lat'))
            long_val = self.convert_gps(match.group('long'), match.group('longdir'))
            date_raw = match.group('date')
            time_raw = match.group('time')
            alt_val = match.group('alt')
            speed_val = match.group('speed')
            course_val = match.group('course') if match.group('course') else 0.0

            year = int(now.year / 100) * 100 + int(date_raw[4:6])
            date_str = f"{year}-{date_raw[2:4]}-{date_raw[0:2]}"
            
            time_clean = time_raw.split('.')[0]
            time_str = f"{time_clean[0:2]}:{time_clean[2:4]}:{time_clean[4:6]}"
            
            gps = {
                'lat': lat_val, 
                'lon': long_val,
                'altitude': float(alt_val),
                'speed': float(speed_val), 
                'course': float(course_val), 
                'time': f"{date_str}T{time_str}.000Z",
                'source' : "GPS"
            }
            self.location_valid = True
            self.current_lat = lat_val
            self.current_long = long_val
            self.current_location_json = gps
            return gps
        
    def get_tracker_id(self):
        file_path = "tracker_id.txt"
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                return f.read().strip()
        
    def get_send_to_cloud_rate(self, speed):
        #speed = kph
        if speed <= 5: send_rate = 900
        elif speed <= 10: send_rate = 600
        elif speed <= 30: send_rate = 480
        elif speed <= 80: send_rate = 120
        else: send_rate = 60
        
        return send_rate

    def setup_mqtt(self):
        self.sim7600.send_command_sleep("AT+CMQTTDISC=0,60", 1)
        self.sim7600.send_command_sleep("AT+CMQTTREL=0", 1)
        self.sim7600.send_command_sleep("AT+CMQTTSTOP", 1)
        self.sim7600.send_command_sleep("AT+CMQTTSTART", 2)
        self.sim7600.send_command_sleep('AT+CMQTTACCQ=0,"000015"', 2)
        self.sim7600.send_command_sleep(f'AT+CMQTTCONNECT=0,"tcp://172.161.129.0:1883",60,1', 5)

        topic = "monitorer/1/marked"
        self.sim7600.send_command_sleep(f'AT+CMQTTSUB=0,{len(topic)},1', 2)
        self.sim7600.send_command_sleep(topic, 1)
        self.error_cnt = 0
        print(f"Subscribed to {topic}")

    def sim7600_setup(self):
        self.sim7600.send_command_sleep("AT", 1)
        self.sim7600.send_command_sleep("AT+CGPS=1,1", 1)
        print("SIM7600 ready for collecting GPS data")

        self.sim7600.send_command_sleep("AT")
        self.sim7600.send_command_sleep("AT+CCID", 3)
        self.sim7600.send_command_sleep("AT+CREG?", 3)
        self.sim7600.send_command_sleep("AT+CGATT=1")
        self.sim7600.send_command_sleep("AT+CGACT=1,1")
        self.sim7600.cell_connected = True
        print("SIM7600 ready for sending to cloud")

        self.sim7600.modem.reset_input_buffer()

    def sim7600_disconnect_from_services(self):
        self.sim7600.send_command_sleep("AT+CMQTTDISC=0,60", 1)
        self.sim7600.send_command_sleep("AT+CMQTTREL=0", 1)
        self.sim7600.send_command_sleep("AT+CGACT=0,1")

    def sim7600_restore_services(self):
        now = time.time()
        print("Restoring...")

        if now - self.last_creg_check > 15 and self.sim7600.get_action_state() == self.sim7600.ActionState.NONE:
            print("Check netw...")
            self.sim7600.send_command("AT+CREG?")
            self.last_creg_check = now

        if not self.sim7600.cell_connected:
            if now - self.last_reconnect_try > 20:
                print("Network not connected. Reseting cell...")
                self.sim7600.send_command_sleep("AT+CGATT=1", 1)
                self.sim7600.send_command_sleep("AT+CGACT=1,1", 1)
                self.last_reconnect_try = now
                self.mqtt_needs_reconnect = True
        else:
            if self.mqtt_needs_reconnect:
                print("Network reconnected, starting MQTT...")
                self.setup_mqtt()
                self.mqtt_needs_reconnect = False

    def sim7600_read_gps(self):
        self.sim7600.send_command("AT+CGPSINFO")

    def handle_timeout(self):
        action_state = self.sim7600.get_action_state()
        print("Timeout, resetting state")
        print("action_state", action_state)
        print("transmit_state",self.sim7600.get_transmit_state())
        print("mqtt_state",self.sim7600.get_mqtt_send_state())
        print("receive_state",self.sim7600.get_receive_state())
        self.sim7600.clean_transmit()
        if action_state == self.sim7600.ActionState.MARK_SEND or action_state == self.sim7600.ActionState.GPS_SEND:
            print("Set to retry sending mark")
        else:
            self.sim7600.set_action_state(self.sim7600.ActionState.NONE)

    def handle_modem_response(self, res, action_state):
        if "ERROR" in res and action_state in [self.sim7600.ActionState.GPS_SEND, self.sim7600.ActionState.MARK_SEND]:
            self.error_cnt += 1
            return -1
        elif action_state == self.sim7600.ActionState.GPS_RECV:
            if "+CGPSINFO" in res:
                self.parse_gps(res)
                if self.location_valid and self.data_mode == 'MANUAL':
                    self.data_mode = 'GPS'
                    self.notified_no_gnss = False
                    print("GNSS recovered. Switching back to GPS mode.")
                self.update_server(self.ToFlaskType.COORDS, self.current_location_json)
                self.sim7600.set_action_state(self.sim7600.ActionState.NONE)
        elif action_state in [self.sim7600.ActionState.GPS_SEND, self.sim7600.ActionState.MARK_SEND] and self.sim7600.mqtt_send_state == self.sim7600.MQTTSendState.PUB_DONE:
            if "OK" in res and self.sim7600.get_mqtt_send_state() == self.sim7600.MQTTSendState.PUB_DONE:
                self.error_cnt = 0
                self.timeout_cnt = 0
                self.sim7600.set_action_state(self.sim7600.ActionState.NONE)
                self.sim7600.set_mqtt_send_state(self.sim7600.MQTTSendState.IDLE)
                print("Response in pub done got OK")
        return 0

    def send_mqtt_to_cloud_api_sim7600(self, topic, pl):
        transmit_state = self.sim7600.get_transmit_state()

        if transmit_state == self.sim7600.TransmitState.RESP_WAIT:
            return

        mqtt_state = self.sim7600.get_mqtt_send_state()

        if mqtt_state == self.sim7600.MQTTSendState.IDLE:
            self.sim7600.send_command(f'AT+CMQTTTOPIC=0,{len(topic)}')
            self.sim7600.set_mqtt_send_state(self.sim7600.MQTTSendState.TOPIC_LEN)

        elif mqtt_state == self.sim7600.MQTTSendState.TOPIC_LEN:
            self.sim7600.send_raw_data(topic)
            self.sim7600.set_mqtt_send_state(self.sim7600.MQTTSendState.TOPIC)

        elif mqtt_state == self.sim7600.MQTTSendState.TOPIC:
            self.sim7600.send_command(f'AT+CMQTTPAYLOAD=0,{len(pl)}')
            self.sim7600.set_mqtt_send_state(self.sim7600.MQTTSendState.PAYLOAD_LEN)

        elif mqtt_state == self.sim7600.MQTTSendState.PAYLOAD_LEN:
            self.sim7600.send_raw_data(pl)
            self.sim7600.set_mqtt_send_state(self.sim7600.MQTTSendState.PUB)
        
        elif mqtt_state == self.sim7600.MQTTSendState.PUB:
            self.sim7600.send_command("AT+CMQTTPUB=0,1,60")
            self.sim7600.set_mqtt_send_state(self.sim7600.MQTTSendState.PUB_DONE)
            action_state = self.sim7600.get_action_state()
            if action_state == self.sim7600.ActionState.MARK_SEND:
                self.mark_queue.popleft()
            elif action_state == self.sim7600.ActionState.GPS_SEND:
                self.location_queue.popleft()

        elif mqtt_state == self.sim7600.MQTTSendState.PUB_DONE:
            if self.sim7600.action_state in [self.sim7600.ActionState.MARK_SEND, self.sim7600.ActionState.GPS_SEND]: 
                self.error_cnt = 0

    def toggle_connection(self):
        self.connection_toggle_commanded = False

        msg = {"topic": "conn_stat/cell", "data": {"state": self.sim7600.cell_connected}}
        self.hub_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))

        if self.sim7600.cell_connected:
            self.sim7600_disconnect_from_services()
            self.sim7600.cell_connected = False
            print("Executing button - disconnect from cellular")
            
        else:
            self.sim7600_setup()
            self.setup_mqtt()
            self.sim7600.cell_connected = True
            print("Executing button - connect to cellular")

    def run(self):
        self.dev_id = self.get_tracker_id()
        if (self.dev_id is None):
            if self.ws:
                self.ws.close()
            print("Configuration is needed to create identification text file!")
            return

        self.sim7600_setup()
        self.setup_mqtt()

        while True:
            now = time.time()
            if self.hub_sock is None:
                self.connect_to_hub()

            self.read_uds()
            self.sim7600.sim7600_async_read()
            self.sim7600.process_received()
            res = self.sim7600.command_response

            action_state = self.sim7600.get_action_state()

            if res is not None:
                if (self.handle_modem_response(res, action_state) < 0):
                    continue
                action_state = self.sim7600.get_action_state()

            if self.sim7600.mqtt_receive_state == self.sim7600.MQTTReceiveState.RECEIVED_PAYLOAD_MARK:
                self.update_server(self.ToFlaskType.MARK, self.sim7600.mqtt_payload)
                self.sim7600.mqtt_receive_state = self.sim7600.MQTTReceiveState.IDLE

            if self.error_cnt > 10:
                self.sim7600.clean_transmit()
                self.mqtt_needs_reconnect = True
                #self.sim7600_restore_services()
                continue

            #self.sim7600_restore_services()

            send_rate_no_manual = self.get_send_to_cloud_rate(self.moving_speed)
            to_cloud_rate = self.send_rate_manual if self.is_send_rate_manual else send_rate_no_manual

            if now - self.last_updated_to_cloud > to_cloud_rate:
                print(f"Time to send... {self.current_lat} {self.current_long}")
                if (self.current_lat > 0 and self.current_long > 0):
                    self.location_queue.append(json.dumps({"id": self.dev_id, "lat": self.current_lat, "long": self.current_long}))
                self.last_updated_to_cloud = now
                
            if self.connection_toggle_commanded and action_state == self.sim7600.ActionState.NONE:
                self.toggle_connection()
                continue

            gps_poll_rate = 1 if self.data_mode == 'GPS' else 5
            if self.fallback_to_gnss and now - self.last_updated_to_maps > gps_poll_rate and action_state == self.sim7600.ActionState.NONE:
                self.sim7600.set_action_state(self.sim7600.ActionState.GPS_RECV)
                self.sim7600_read_gps()
                self.last_updated_to_maps = now
                action_state = self.sim7600.get_action_state()

            elif self.sim7600.cell_connected and self.mark_queue and action_state == self.sim7600.ActionState.NONE:
                self.sim7600.set_action_state(self.sim7600.ActionState.MARK_SEND)
                action_state = self.sim7600.get_action_state()
            
            elif self.sim7600.cell_connected and self.location_queue and action_state == self.sim7600.ActionState.NONE:
                self.sim7600.set_action_state(self.sim7600.ActionState.GPS_SEND)
                action_state = self.sim7600.get_action_state()

            elif self.sim7600.cell_connected and self.location_queue:
                print("for some reason cell connected fucked")

            if (action_state == self.sim7600.ActionState.GPS_SEND and self.location_queue):
                self.send_mqtt_to_cloud_api_sim7600(f"driver/location/{self.dev_id}", self.location_queue[0])

            if (action_state == self.sim7600.ActionState.MARK_SEND and self.mark_queue):
                self.send_mqtt_to_cloud_api_sim7600(f"driver/mark_location/{self.dev_id}", self.mark_queue[0])
            
            if self.sim7600.get_transmit_state() == self.sim7600.TransmitState.RESP_WAIT and now - self.sim7600.current_write_start > 5:
                self.handle_timeout()
            time.sleep(0.1)

if __name__ == "__main__":
    app = TelemetryControl()
    try:
        app.run()
    except KeyboardInterrupt:
        print("Stopping...")
        if app.ws: app.ws.close()
        app.sim7600.modem.close()