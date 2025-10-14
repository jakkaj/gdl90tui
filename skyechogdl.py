import socket
import sys
from pathlib import Path

# Add the gdl90 library to the Python path
gdl90_path = Path(__file__).parent / "gdl90"
if gdl90_path.exists():
    sys.path.insert(0, str(gdl90_path))

from gdl90 import decoder  # from etdey/gdl90 (cloned via make install)

UDP_IP = "0.0.0.0"    # your machine IP on the SkyEcho Wi-Fi, or 0.0.0.0 to bind all
UDP_PORT = 4000

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))

dec = decoder.Decoder()
print("SkyEcho GDL90 Receiver started. Listening on {}:{}".format(UDP_IP, UDP_PORT))
print("Waiting for data...")

try:
    while True:
        data, addr = sock.recvfrom(4096)
        # addBytes() automatically decodes and prints messages
        dec.addBytes(data)
except KeyboardInterrupt:
    # Handle Ctrl-C gracefully
    print("\nReceiver stopped by user")
    sock.close()
except Exception as e:
    print(f"\nError: {e}")
    sock.close()
