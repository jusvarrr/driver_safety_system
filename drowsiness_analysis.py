import cv2
from picamera2 import Picamera2
import socket
import json
import time
import signal
import sys
import numpy as np

def connect():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect("/tmp/system_hub.sock")
    return s

print("Waiting for UDS broker (/tmp/system_hub.sock) to start...", flush=True)
hub_sock = None
while True:
    try:
        hub_sock = connect()
        print("Successfully connected to UDS broker!", flush=True)
        break
    except Exception:
        time.sleep(2)

last_buzzer_state = -1
state = 0

def send_buzzer(state):
    global hub_sock
    try:
        msg = {
            "topic": "alert/buzzer",
            "data": {"state": state}
        }
        hub_sock.sendall((json.dumps(msg) + "\n").encode())
    except Exception:
        try:
            hub_sock.close()
        except:
            pass
        time.sleep(1)
        hub_sock = connect()

def send_cam_on(state):
    global hub_sock
    try:
        msg = {
            "topic": "conn_stat/cam",
            "data": {"state": state}
        }
        hub_sock.sendall((json.dumps(msg) + "\n").encode())
    except Exception:
        try:
            hub_sock.close()
        except:
            pass
        time.sleep(1)
        hub_sock = connect()

def cam_term(signum, frame):
    try:
        picam2.stop()
        send_cam_on(0)
    except Exception as e:
        print("error turning cam off")
    sys.exit(0)

landmark_color = [
        (255,   0,   0), # right eye
        (  0,   0, 255), # left eye
        (  0, 255,   0), # nose tip
        (255,   0, 255), # right mouth corner
        (  0, 255, 255)  # left mouth corner
    ]

detector = cv2.FaceDetectorYN.create("face_detection_yunet_2026may.onnx", "", (320, 240))
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
face_alt_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_alt.xml')
face_alt2_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_alt2.xml')
eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')
eye_glasses = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye_tree_eyeglasses.xml')

picam2 = Picamera2()

signal.signal(signal.SIGINT, cam_term)
signal.signal(signal.SIGTERM, cam_term)

picam2.configure(picam2.create_preview_configuration(main={"format": "BGR888", "size": (320, 240)}))
print("size 320x240")
picam2.start()
send_cam_on(1)
sleep_counter = 0

origin = ""

while True:
    frame = picam2.capture_array()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    faces = face_cascade.detectMultiScale(gray, 1.1, 5)
    face_model = 'default'

    if len(faces) > 0:
        (x, y, w, h) = faces[0]

        color = (255, 0, 0)
        label = "Face"
        cv2.rectangle(frame, (x, y), (x+w, y+h), color, 2)
        cv2.putText(frame, label, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

        roi_gray = gray[y:y+h, x:x+w]

        eye_neighbors = 15
        eyes = eye_cascade.detectMultiScale(roi_gray[0:int(h/1.8), :], 1.1, eye_neighbors)
        eyes_model = "basic_eyes"
        if len(eyes) == 0:
            eyes = eye_glasses.detectMultiScale(roi_gray[0:int(h/1.8), :], 1.1, eye_neighbors)
            eyes_model = "glasses"

        for (ex, ey, ew, eh) in eyes:
            cv2.rectangle(frame, (x + ex, y + ey), (x + ex+ew, y + ey+eh), (0, 255, 0), 2)

        if len(eyes) == 0:
            sleep_counter += 1
            if sleep_counter > 7:
                state = 1
                print(f"{face_model}, {eyes_model} - drowsy")
                cv2.putText(frame, "MIEGUISTUMAS", (50, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
        else:
            state = 0
            sleep_counter = 0
            print(f"{face_model}, {eyes_model} - awake")
            cv2.putText(frame, "BUDRUMAS", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    else:
        state = 0
        sleep_counter = 0

    if state != last_buzzer_state:
        send_buzzer(state)
        last_buzzer_state = state

    display_frame = cv2.resize(frame, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)
    cv2.imshow("DMS - Profile Support", display_frame)
    time.sleep(0.2)
    if cv2.waitKey(1) == ord('q'): break

picam2.stop()
cv2.destroyAllWindows()