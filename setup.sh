#!/bin/bash

# You must change channel to output of iw dev wlan0 info and password to your chosen one

if [ "$EUID" -ne 0 ]; then
  echo "Please use sudo: sudo ./setup.sh"
  exit 1
fi

REAL_USER="${SUDO_USER:-$USER}"
USER_HOME=$(getent passwd "$REAL_USER" | cut -d: -f6)
PROJ_DIR="$(pwd)"
FIX_NET_SCRIPT="$PROJ_DIR/fix_network.sh"

echo "1/6 Installing required packages"
apt-get update
apt-get install -y python3-flask python3-picamera2 python3-opencv libgpiod-dev git build-essential hostapd dnsmasq rng-tools iptables-persistent python3-venv git-lfs

echo "2/6 Pulling tiles from lfs"
cd "$PROJ_DIR" || exit
sudo -u "$REAL_USER" git lfs install
sudo -u "$REAL_USER" git lfs pull

echo "3/6 Preparing python venv"
VENV_PATH="$USER_HOME/.venv_driver_assist"
if [ ! -d "$VENV_PATH" ]; then
    python3 -m venv --system-site-packages "$VENV_PATH"
    chown -R "$REAL_USER":"$REAL_USER" "$VENV_PATH"
fi

source "$VENV_PATH/bin/activate"
pip3 install --upgrade pip
if [ -f "$PROJ_DIR/requirements.txt" ]; then
    pip3 install -r "$PROJ_DIR/requirements.txt"
fi
deactivate

echo "4/6 Compiling C code for sensor and actuator control"
cd "$PROJ_DIR" || exit
make
if [ -f "sensor_ctrl" ]; then
    chmod +x sensor_ctrl
fi

echo "5/6 Wi-Fi access point config"

if ! grep -q "interface uap0" /etc/dhcpcd.conf; then
    cat << 'EOF' >> /etc/dhcpcd.conf
slaac private
interface uap0
    static ip_address=10.3.141.1/24
    nohook wpa_supplicant
EOF
fi

cat << 'EOF' > /etc/dnsmasq.conf
interface=uap0
except-interface=wlan0
bind-dynamic
dhcp-range=10.3.141.50,10.3.141.255,12h
dhcp-option=option:dns-server,8.8.8.8
EOF

cat << 'EOF' > /etc/hostapd/hostapd.conf
interface=uap0
driver=nl80211
ssid=RPi_DSM_Monitor
hw_mode=g
channel=--change!!!
country_code=LT
wpa=2
wpa_passphrase=--change!!!
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF

sed -i 's|#DAEMON_CONF=""|DAEMON_CONF="/etc/hostapd/hostapd.conf"|g' /etc/default/hostapd

cat << 'EOF' > /etc/udev/rules.d/90-wireless.rules
ACTION=="add", SUBSYSTEM=="net", KERNEL=="wlan0", RUN+="/sbin/iw dev wlan0 interface add uap0 type __ap"
EOF

if [ -f "$FIX_NET_SCRIPT" ]; then
    chmod +x "$FIX_NET_SCRIPT"
    chown "$REAL_USER":"$REAL_USER" "$FIX_NET_SCRIPT"
else
    echo "Warning: fix_network.sh not found!"
fi

systemctl unmask hostapd dnsmasq
systemctl disable hostapd dnsmasq

echo "6/6 sudo crontab config"
TMP_CRON=$(mktemp)
crontab -l > "$TMP_CRON" 2>/dev/null

sed -i '/driver_safety_system/d' "$TMP_CRON"
sed -i '/fix_network.sh/d' "$TMP_CRON"
sed -i '/power_save off/d' "$TMP_CRON"

cat << EOF >> "$TMP_CRON"
@reboot /usr/sbin/iw dev wlan0 set power_save off
@reboot sleep 30 && $FIX_NET_SCRIPT > $PROJ_DIR/ap_log.txt 2>&1
@reboot sleep 45 && cd $PROJ_DIR && taskset -c 0,1 $VENV_PATH/bin/python3 uds-broker.py >> $PROJ_DIR/driver_safety_system.log 2>&1
@reboot sleep 45 && cd $PROJ_DIR && taskset -c 0,1 ./sensor_ctrl >> $PROJ_DIR/driver_safety_system.log 2>&1
@reboot sleep 48 && cd $PROJ_DIR/mapweb && taskset -c 0,1 $VENV_PATH/bin/python3 -m flask --app map_serv.py run --host=10.3.141.1 --port=5000 >> $PROJ_DIR/driver_safety_system.log 2>&1
@reboot sleep 50 && cd $PROJ_DIR && taskset -c 0,1 $VENV_PATH/bin/python3 telemetry_control.py >> $PROJ_DIR/driver_safety_system.log 2>&1
@reboot sleep 52 && cd $PROJ_DIR && taskset -c 2,3 $VENV_PATH/bin/python3 drowsiness_analysis.py >> $PROJ_DIR/driver_safety_system.log 2>&1
EOF

crontab "$TMP_CRON"
rm "$TMP_CRON"

chmod +x $PROJ_DIR/*.py $PROJ_DIR/mapweb/*.py 2>/dev/null
chown -R "$REAL_USER":"$REAL_USER" "$PROJ_DIR"

echo " Setup complete! "
echo " Location: $PROJ_DIR"
#echo " Sistem reboot..."
sleep 2
#reboot