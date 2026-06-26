"""
TCP/UDP Packet Sink
--------------------
Listens on TCP and UDP port 9999.
Accepts all incoming connections and discards data (packet sink).
Exists so gen-tcpudp has a real target, making TCP RST visible in Wireshark
when this container is stopped (Failure Visibility analysis).
"""

import socket, threading, time

PORT = 9999


def tcp_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", PORT))
    s.listen(128)
    print(f"[sink] TCP listening on :{PORT}")
    while True:
        conn, addr = s.accept()
        threading.Thread(target=drain, args=(conn,), daemon=True).start()


def drain(conn):
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                break
    except Exception:
        pass
    finally:
        conn.close()


def udp_server():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", PORT))
    print(f"[sink] UDP listening on :{PORT}")
    while True:
        try:
            s.recvfrom(65536)
        except Exception:
            pass


threading.Thread(target=tcp_server, daemon=True).start()
threading.Thread(target=udp_server, daemon=True).start()

print("[sink] Ready")
while True:
    time.sleep(1)
