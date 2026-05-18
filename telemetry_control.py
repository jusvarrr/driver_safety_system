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
        self.dev_id = "RPi-Bakalauras-2026"
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
                if self.location_valid:
                    self.notified_no_gnss = False
                    msg = {"topic": "conn_stat/gnss", "data": {"state": 1, "lon": self.current_long, "lat": self.current_lat}}
                    self.hub_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))
                    
                    msg = {"topic": "location/gnss", "data": {"lon": self.current_long, "lat": self.current_lat}}
                    self.hub_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))
                else:
                    if not self.notified_no_gnss and self.current_long and self.current_lat:
                        msg = {"topic": "conn_stat/gnss", "data": {"state": 0, "lon": self.current_long, "lat": self.current_lat}}
                        self.hub_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))
                        self.notified_no_gnss = True
                        print("[Telemetry] GNSS Lost. Passing tracking baton to IMU Dead Reckoning.")
                
            elif data_type == self.ToFlaskType.MARK:
                if payload and isinstance(payload, dict):
                    if 'long' in payload:
                        payload['lon'] = payload.pop('long')
                    elif 'longitude' in payload:
                        payload['lon'] = payload.pop('longitude')
                
                msg = {"topic": "marks/cloud", "data": payload}
                self.hub_sock.sendall((json.dumps(msg) + "\n").encode('utf-8'))

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
                        
                        if topic in ["marks/local", "button/loc"] and data:
                            print(f"Received new marking event from system ({topic}), pushing to SIM7600 queue...")
                            mark_to_send = json.dumps({
                                "name": data.get('name', 'Emergency mark'),
                                "lon": data.get('lon', self.current_long),
                                "lat": data.get('lat', self.current_lat),
                                "info": data.get('info', 'Quick action mark'),
                                "type": data.get('type', 'markedEmergency')
                            })
                            if data.get('sync_cloud', False):
                                self.mark_queue.append(mark_to_send)

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
            self.current_lat, self.current_long = 54.8985, 23.9036 #testing
            return {}
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
                'time': f"{date_str}T{time_str}.000Z"
            }
            self.location_valid = True
            self.current_lat = lat_val
            self.current_long = long_val
            self.current_location_json = gps
            return gps
        
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
        print("SIM7600 ready for sending to cloud")

    def sim7600_read_gps(self):
        self.sim7600.send_command("AT+CGPSINFO")

    def send_mqtt_to_cloud_api_sim7600(self, topic, pl):
        #if not (self.current_location_json and self.location_valid is True):
        #    return

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
            if self.mark_queue:
                self.mark_queue.popleft()

        elif mqtt_state == self.sim7600.MQTTSendState.PUB_DONE:
            if self.sim7600.action_state == self.sim7600.ActionState.MARK_SEND: 
                self.error_cnt = 0


    def run(self):
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
                if "ERROR" in res and action_state in [self.sim7600.ActionState.GPS_SEND, self.sim7600.ActionState.MARK_SEND]:
                    self.error_cnt += 1
                    continue
                elif action_state == self.sim7600.ActionState.GPS_RECV:
                    if "+CGPSINFO" in res:
                        self.parse_gps(res)
                        self.update_server(self.ToFlaskType.COORDS, self.current_location_json)
                        self.sim7600.set_action_state(self.sim7600.ActionState.NONE)
                elif action_state in [self.sim7600.ActionState.GPS_SEND, self.sim7600.ActionState.MARK_SEND] and self.sim7600.mqtt_send_state == self.sim7600.MQTTSendState.PUB_DONE:
                    if "OK" in res and self.sim7600.get_mqtt_send_state() == self.sim7600.MQTTSendState.PUB_DONE:
                        self.error_cnt = 0
                        self.timeout_cnt = 0
                        self.sim7600.set_action_state(self.sim7600.ActionState.NONE)
                        self.sim7600.set_mqtt_send_state(self.sim7600.MQTTSendState.IDLE)
                        print("Response in pub done got OK")
                action_state = self.sim7600.get_action_state()

            if self.sim7600.mqtt_receive_state == self.sim7600.MQTTReceiveState.RECEIVED_PAYLOAD_MARK:
                self.update_server(self.ToFlaskType.MARK, self.sim7600.mqtt_payload)
                self.sim7600.mqtt_receive_state = self.sim7600.MQTTReceiveState.IDLE

            if self.error_cnt > 10:
                self.sim7600.clean_transmit()
                print("Executing MQTT reconnection...")
                self.setup_mqtt()
                continue

            send_rate_no_manual = self.get_send_to_cloud_rate(self.moving_speed)
            to_cloud_rate = self.send_rate_manual if self.is_send_rate_manual else send_rate_no_manual
                
            if now - self.last_updated_to_maps > 1 and action_state == self.sim7600.ActionState.NONE:
                self.sim7600.set_action_state(self.sim7600.ActionState.GPS_RECV)
                action_state = self.sim7600.get_action_state()
                self.sim7600_read_gps()
                self.last_updated_to_maps = now
                action_state = self.sim7600.get_action_state()

            elif self.mark_queue and action_state == self.sim7600.ActionState.NONE:
                self.sim7600.set_action_state(self.sim7600.ActionState.MARK_SEND)
                action_state = self.sim7600.get_action_state()
            
            elif (now - self.last_updated_to_cloud > to_cloud_rate) and action_state == self.sim7600.ActionState.NONE:
                self.sim7600.set_action_state(self.sim7600.ActionState.GPS_SEND)
                action_state = self.sim7600.get_action_state()
                self.last_updated_to_cloud = now

            if (action_state == self.sim7600.ActionState.GPS_SEND):
                self.send_mqtt_to_cloud_api_sim7600(f"driver/location/{self.dev_id}", json.dumps({"id":self.dev_id,"lat":self.current_lat,"long":self.current_long}))

            if (action_state == self.sim7600.ActionState.MARK_SEND and self.mark_queue):
                self.send_mqtt_to_cloud_api_sim7600(f"driver/mark_location/{self.dev_id}", self.mark_queue[0])
            
            if self.sim7600.get_transmit_state() == self.sim7600.TransmitState.RESP_WAIT:
                if now - self.sim7600.current_write_start > 5:
                    print("Timeout, resetting state")
                    print("action_state",action_state)
                    print("transmit_state",self.sim7600.get_transmit_state())
                    print("mqtt_state",self.sim7600.get_mqtt_send_state())
                    print("receive_state",self.sim7600.get_receive_state())

                    self.sim7600.clean_transmit()

                    self.timeout_cnt += 1

                    if action_state == self.sim7600.ActionState.MARK_SEND or action_state == self.sim7600.ActionState.GPS_SEND:
                        print("Set to retry sending mark")

                    else:
                        self.sim7600.set_action_state(self.sim7600.ActionState.NONE)
                else: self.timeout_cnt = 0

            time.sleep(0.1)

if __name__ == "__main__":
    app = TelemetryControl()
    try:
        app.run()
    except KeyboardInterrupt:
        print("Stopping...")
        if app.ws: app.ws.close()
        app.sim7600.modem.close()