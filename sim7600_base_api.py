import serial
import time
import re
from enum import Enum

class SIM7600BaseApi:

    class ActionState(Enum):
        NONE = 1
        GPS_SEND = 2
        GPS_RECV = 3
        MARK_SEND = 4
        MARK_RECV = 5
        MARK_LIST_RECV = 6

    class TransmitState(Enum):
        RESP_WAIT = 1
        COMPLETE = 2

    class ReceiveState(Enum):
        COMPLETE = 1
        INCOMPLETE = 2
        PROCESSED = 3
        FINISHED = 4

    class MQTTSendState(Enum):
        IDLE = 1
        TOPIC_LEN = 2
        TOPIC = 3
        PAYLOAD_LEN = 4
        PAYLOAD = 5
        PUB = 6
        PUB_DONE = 7

    class MQTTReceiveState(Enum):
        IDLE = 0
        RECEIVED_TOPIC_MARK = 1
        RECEIVED_PAYLOAD_MARK = 2

    class GPSReadState(Enum):
        IDLE = 1
        GETTING_LOC = 2

    class ResponseCode(Enum):
        NONE = 1
        OK = 2
        ERROR = 3
        POINTER = 4
        CGPSINFO = 5

    def __init__(self):
        self.MAX_RECV = 1024
        self.serial_port = "/dev/ttyS0"
        self.baud_rate = 115200

        self.read_buffer = bytearray(self.MAX_RECV)
        self.transmit_state = self.TransmitState.COMPLETE
        self.receive_state = self.ReceiveState.COMPLETE
        self.action_state = self.ActionState.NONE
        self.mqtt_send_state = self.MQTTSendState.IDLE
        self.mqtt_receive_state = self.MQTTSendState.IDLE
        self.response_code = self.ResponseCode.OK

        self.read_buffer_idx = 0
        self.read_buffer_previous_idx = 0
        self.mqtt_receive_data = ""
        self.command_response = ""

        self.current_write_start = 0
        self.modem = serial.Serial(self.serial_port, self.baud_rate, timeout=2)

        self.mqtt_payload = ""

        self.cell_connected = False
        self.previous_cell_connected = True

    def get_mqtt_send_state(self):
        return self.mqtt_send_state
    
    def get_transmit_state(self):
        return self.transmit_state
    
    def get_receive_state(self):
        return self.receive_state
    
    def get_action_state(self):
        return self.action_state

    def get_read_buffer(self):
        return self.read_buffer
    
    def set_mqtt_send_state(self, state):
        self.mqtt_send_state = state
    
    def set_transmit_state(self, state):
        self.transmit_state = state
    
    def set_receive_state(self, state):
        self.receive_state = state

    def set_action_state(self, state):
        self.action_state = state

    def clean_transmit(self):
        self.transmit_state = self.TransmitState.COMPLETE
        self.mqtt_send_state = self.MQTTSendState.IDLE
        self.read_buffer_previous_idx = self.read_buffer_idx
        self.modem.reset_input_buffer()

    def get_unprocessed_rx_buffer(self):
        if self.read_buffer_idx < self.read_buffer_previous_idx:
            ordered_bytes = self.read_buffer[self.read_buffer_previous_idx:self.MAX_RECV] + self.read_buffer[0:self.read_buffer_idx]
        else:
            ordered_bytes = self.read_buffer[self.read_buffer_previous_idx:self.read_buffer_idx]
        ordered_data = ordered_bytes.decode("latin-1", errors="ignore")
        return ordered_data
    
    def sim7600_async_read(self):
        waiting_bytes = self.modem.in_waiting

        if waiting_bytes > 0:
            current_read = self.modem.read(waiting_bytes)
            if self.read_buffer_idx + waiting_bytes <= self.MAX_RECV:
                self.read_buffer[self.read_buffer_idx:self.read_buffer_idx + waiting_bytes] = current_read
                self.read_buffer_idx += waiting_bytes
            else:
                overload = (self.read_buffer_idx + waiting_bytes) % self.MAX_RECV
                
                break_point = waiting_bytes - overload
                self.read_buffer[self.read_buffer_idx : self.read_buffer_idx + break_point] = current_read[:break_point]
                self.read_buffer[0 : overload] = current_read[break_point:]
                self.read_buffer_idx = overload
    
    def process_received(self):
        ordered_data = self.get_unprocessed_rx_buffer()
        if not ordered_data:
            return
        found_last = 0
        self.response_code = self.ResponseCode.NONE
        self.command_response = ""
        if self.transmit_state == self.TransmitState.RESP_WAIT and self.read_buffer_previous_idx != self.read_buffer_idx:

            if ordered_data.find("OK\r\n") >= 0:
                self.response_code = self.ResponseCode.OK
                found_current = ordered_data.find("OK\r\n")
                found_last = found_current + len("OK\r\n")
            if ordered_data.find("ERROR\r\n") >= 0:
                self.response_code = self.ResponseCode.ERROR
                found_current = ordered_data.find("ERROR\r\n") + len("ERROR\r\n")
                if found_current > found_last: found_last = found_current 
            if ordered_data.find(">") >= 0:
                self.response_code = self.ResponseCode.POINTER
                found_current = ordered_data.find(">") + 1
                if found_current > found_last: found_last = found_current

            gps_index = ordered_data.find("+CGPSINFO:")
            if gps_index >= 0:
                self.response_code = self.ResponseCode.CGPSINFO
                line_end = ordered_data.find("\r\n", gps_index)
                if line_end >= 0:
                    found_current = line_end + 2
                    if found_current > found_last: 
                        found_last = found_current
            
            if "+CREG:" in ordered_data:
                matches = re.search(r"\+CREG:\s*\d,(\d)", ordered_data)
                if matches:
                    stat = int(matches.group(1))
                    self.previous_cell_connected = self.cell_connected
                    self.cell_connected = (stat == 1 or stat == 5)
                    header_end = matches.end()
                    if header_end > found_last: found_last = header_end

        if self.response_code != self.ResponseCode.NONE:
            print("getting response!")
            print(ordered_data)
            self.transmit_state = self.TransmitState.COMPLETE
            self.command_response = ordered_data
        else: self.command_response = None

        if "+CMQTTRXSTART" in ordered_data:
            start_reg = r"\+CMQTTRXSTART: (\d+),(\d+),(\d+)\r\n"
            matches = re.search(start_reg, ordered_data)
            if matches:
                t_len = int(matches.group(2))
                p_len = int(matches.group(3))
                header_end = matches.end()
                print(f"MQTT start: topic len {t_len}, Payload len {p_len}")
                if header_end > found_last: found_last = header_end

        if "+CMQTTRXTOPIC" in ordered_data:
            topic_reg = r"\+CMQTTRXTOPIC: (\d+),(\d+)\r\n"
            matches = re.search(topic_reg, ordered_data)
            if matches:
                t_len = int(matches.group(2))
                header_end = matches.end()
                if len(ordered_data) >= header_end + t_len:
                    topic = ordered_data[header_end:header_end + t_len]
                    if topic == "monitorer/1/marked":
                        self.mqtt_receive_state = self.MQTTReceiveState.RECEIVED_TOPIC_MARK
                    if header_end > found_last: found_last = header_end + t_len

        if "+CMQTTRXPAYLOAD" in ordered_data:
            payload_reg = r"\+CMQTTRXPAYLOAD: (\d+),(\d+)\r\n"
            matches = re.search(payload_reg, ordered_data)
            if matches:
                p_len = int(matches.group(2))
                header_end = matches.end()
                if len(ordered_data) >= header_end + p_len:
                    if self.mqtt_receive_state == self.MQTTReceiveState.RECEIVED_TOPIC_MARK:
                        self.mqtt_payload = ordered_data[header_end:header_end + p_len]
                        self.mqtt_receive_state = self.MQTTReceiveState.RECEIVED_PAYLOAD_MARK
                    if header_end > found_last: found_last = header_end + p_len      
                    print(self.mqtt_payload)
        if "+CMQTTRXEND" in ordered_data:
            start_reg = r"\+CMQTTRXEND: (\d+)"
            matches = re.search(start_reg, ordered_data)
            if matches:
                header_end = matches.end()
                print(f"MQTT transfer complete")

                if header_end > found_last: found_last = header_end      

        self.read_buffer_previous_idx = (self.read_buffer_previous_idx + found_last) % self.MAX_RECV
            
    
    def send_command(self, command, delay = 1):
        command = f'{command}\r'
        self.modem.write(command.encode())
        self.current_write_start = time.time()
        self.transmit_state = self.TransmitState.RESP_WAIT

    def send_raw_data(self, data):
        self.modem.write(data.encode())
        self.current_write_start = time.time()
        self.transmit_state = self.TransmitState.RESP_WAIT

    def send_command_sleep(self, command, delay = 1):
        command = f'{command}\r'
        self.modem.write(command.encode())
        time.sleep(delay)