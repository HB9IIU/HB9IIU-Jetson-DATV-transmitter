key=$(cat /sys/kernel/config/usb_gadget/composite_gadget/strings/0x409/serialnumber)
dt_root="dt/pluto/$key"
cmd_root="cmd/pluto/$key"
tunip="44.0.0.3"
pcforward="192.168.1.39"

$(mosquitto_pub -t $cmd_root/tx/gain -m -25)
$(mosquitto_pub -t $cmd_root/tx/mute -m 0)

#$(mosquitto_pub -t $cmd_root/tx/frequency -m 2404.750e6)
$(mosquitto_pub -t $cmd_root/tx/frequency -m 2403.250e6)
$(mosquitto_pub -t $cmd_root/tx/dvbs2/sr -m 333000)
#$(mosquitto_pub -t $cmd_root/tx/dvbs2/sr -m 125000)
$(mosquitto_pub -t $cmd_root/tx/nco -m 000)

#$(mosquitto_pub -t $cmd_root/tx/stream/mode -m dvbs2-ts)
$(mosquitto_pub -t $cmd_root/tx/stream/mode -m dvbs2-gse)
$(mosquitto_pub -t $cmd_root/tx/dvbs2/frame -m short)
$(mosquitto_pub -t $cmd_root/tx/dvbs2/constel -m qpsk)
#$(mosquitto_pub -t $cmd_root/tx/dvbs2/constel -m 8psk)
$(mosquitto_pub -t $cmd_root/tx/dvbs2/fec -m 4/5)
$(mosquitto_pub -t $cmd_root/tx/dvbs2/pilots -m 1)
#$(mosquitto_pub -t $cmd_root/tx/dvbs2/agcgain -m -30.0)
#$(mosquitto_pub -t $cmd_root/tx/dvbs2/fecmode -m variable)


$(mosquitto_pub -t $cmd_root/rx/frequency -m 2403.250e6)
#$(mosquitto_pub -t $cmd_root/rx/gain -m 65.0)
# ****************************
# Receiver Lognmynd for F5OEO with mqtt
# ****************************
#./setrx.sh 744750 1000

#sysctl -w net.ipv4.ip_forward=1
sleep 4
ifconfig gse0 $tunip
route add -net 44.0.0.0/24 gw $tunip

iptables -A FORWARD -i eth0 -o gse0 -d $tunip/24 -j ACCEPT
iptables -A FORWARD -i lo -o gse0 -d $tunip/24 -j ACCEPT
iptables -t nat -A POSTROUTING -o gse0  -j MASQUERADE
#iptables -A FORWARD -i eth0 -o gse0 -m state --state ESTABLISHED,RELATED -j ACCEPT
#iptables -A FORWARD -p udp -d 44.0.0.4 --dport 1234 -j ACCEPT
iptables -t nat -A PREROUTING -p udp -i gse0 --dport 1234 -j DNAT --to-destination $pcforward:1234
iptables -t nat -A PREROUTING -p tcp -i gse0 --dport 8073 -j DNAT --to-destination 192.168.1.46:8073
iptables -t nat -A POSTROUTING -o eth0  -j MASQUERADE
iptables -A FORWARD -i gse0 -o eth0 -d $tunip/24 -j ACCEPT