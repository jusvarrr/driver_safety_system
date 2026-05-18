#!/bin/bash
sudo /usr/sbin/iw dev wlan0 set power_save off
sudo ifconfig uap0 10.3.141.1 netmask 255.255.255.0 up
sudo systemctl restart hostapd
sudo systemctl restart dnsmasq
