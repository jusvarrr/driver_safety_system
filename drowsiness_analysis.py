import cv2
from picamera2 import Picamera2
import socket
import json
import time
import signal
import sys

def connect():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect("/tmp/system_hub.sock")
    return s

try:
    hub_sock = connect()
except Exception:
    print("broker not ready, will try reconnecting")
    hub_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

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
        hub_sock = connect()

def cam_term(signum, frame):
    try:
        picam2.stop()
    except Exception as e:
        print("error turning cam off")
    sys.exit(0)

face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
profile_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_profileface.xml')
eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')
mouth_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_smile.xml')

picam2 = Picamera2()

signal.signal(signal.SIGINT, cam_term)
signal.signal(signal.SIGTERM, cam_term)

picam2.configure(picam2.create_preview_configuration(main={"format": "BGR888", "size": (640, 480)}))
picam2.start()

sleep_counter = 0

while True:
    frame = picam2.capture_array()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(gray, 1.1, 5)
    
    is_profile = False
    if len(faces) == 0:
        faces = profile_cascade.detectMultiScale(gray, 1.1, 5)
        is_profile = True

    if len(faces) > 0:
        (x, y, w, h) = faces[0]
        
        color = (255, 191, 0) if is_profile else (255, 0, 0)
        label = "Profilis" if is_profile else "Veidas"
        #cv2.rectangle(frame, (x, y), (x+w, y+h), color, 2)
        #cv2.putText(frame, label, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

        roi_gray = gray[y:y+h, x:x+w]
        roi_color = frame[y:y+h, x:x+w]

        eye_neighbors = 5 if is_profile else 15
        eyes = eye_cascade.detectMultiScale(roi_gray[0:int(h/1.8), :], 1.1, eye_neighbors)
        
        mouths = mouth_cascade.detectMultiScale(roi_gray[int(h/1.6):, :], 1.5, 20)

        #for (ex, ey, ew, eh) in eyes:
        #    cv2.rectangle(roi_color, (ex, ey), (ex+ew, ey+eh), (0, 255, 0), 2)
        #for (mx, my, mw, mh) in mouths:
        #    cv2.rectangle(roi_color, (mx, my+int(h/1.6)), (mx+mw, my+mh+int(h/1.6)), (0, 255, 255), 2)

        if len(eyes) == 0:
            sleep_counter += 1
            if sleep_counter > 10:
                state = 1
                #cv2.putText(frame, "MIEGUISTUMAS", (50, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
                print("drowsy")
        else:
            state = 0
            sleep_counter = 0
            print("awake")
            #cv2.putText(frame, "Budrus", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    else:
        state = 0
        sleep_counter = 0

    if state != last_buzzer_state:
        send_buzzer(state)
        last_buzzer_state = state

    #cv2.imshow("DMS - Profile Support", frame)
    time.sleep(0.01)
    #if cv2.waitKey(1) == ord('q'): break

picam2.stop()
#cv2.destroyAllWindows()