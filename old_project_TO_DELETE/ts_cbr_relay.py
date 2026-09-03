#!/usr/bin/python3
"""Pad an incoming MPEG-TS UDP stream to a constant DVB channel rate."""

import argparse
import collections
import select
import socket
import time


TS_SIZE = 188
PACKETS_PER_DATAGRAM = 7


def null_packet(counter):
    return bytes((0x47, 0x1F, 0xFF, 0x10 | (counter & 0x0F))) + bytes(184)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-port", type=int, default=10000)
    parser.add_argument("--destination", default="192.168.0.50")
    parser.add_argument("--port", type=int, default=8282)
    parser.add_argument("--bitrate", type=int, default=744968)
    args = parser.parse_args()

    incoming = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    incoming.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    incoming.bind(("127.0.0.1", args.listen_port))
    incoming.setblocking(False)
    outgoing = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    packets = collections.deque()
    null_cc = 0
    interval = TS_SIZE * PACKETS_PER_DATAGRAM * 8.0 / args.bitrate
    next_send = time.monotonic() + interval
    destination = (args.destination, args.port)
    print("CBR relay {} b/s -> {}:{}".format(args.bitrate, args.destination, args.port), flush=True)

    while True:
        timeout = max(0.0, next_send - time.monotonic())
        readable, _, _ = select.select([incoming], [], [], timeout)
        if readable:
            while True:
                try:
                    data = incoming.recv(65535)
                except BlockingIOError:
                    break
                usable = len(data) - (len(data) % TS_SIZE)
                for offset in range(0, usable, TS_SIZE):
                    packet = data[offset:offset + TS_SIZE]
                    if packet and packet[0] == 0x47:
                        packets.append(packet)

        now = time.monotonic()
        while now >= next_send:
            datagram = bytearray()
            for _ in range(PACKETS_PER_DATAGRAM):
                if packets:
                    datagram.extend(packets.popleft())
                else:
                    datagram.extend(null_packet(null_cc))
                    null_cc = (null_cc + 1) & 0x0F
            outgoing.sendto(datagram, destination)
            next_send += interval
        if next_send < now - interval:
            next_send = now + interval


if __name__ == "__main__":
    main()
