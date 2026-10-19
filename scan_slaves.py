import serial
import struct
import time

def crc16(data):
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for _ in range(8):
            if (crc & 1) != 0:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack("<H", crc)

port = "/dev/ttyUSB0"
ser = serial.Serial(port, baudrate=9600, parity=serial.PARITY_EVEN, bytesize=8, stopbits=1, timeout=0.4)

print("Scanning Modbus RTU Slave IDs 1..16...")
found = []
for slave in range(1, 17):
    # Try FC 04 start 0x0080 count 2 (read pack voltage & busbar voltage)
    pdu = struct.pack(">BBHH", slave, 4, 0x0080, 2)
    frame = pdu + crc16(pdu)
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    time.sleep(0.05)
    ser.write(frame)
    time.sleep(0.12)
    resp = ser.read(9)
    if len(resp) >= 9 and resp[0] == slave and resp[1] == 4:
        v = struct.unpack(">H", resp[3:5])[0] * 0.01
        print(f"  [+] Responding Slave {slave}: Pack Voltage = {v:.2f} V")
        found.append(slave)
    else:
        # Also try FC 03 just in case
        pdu3 = struct.pack(">BBHH", slave, 3, 0x0000, 2)
        frame3 = pdu3 + crc16(pdu3)
        ser.reset_input_buffer()
        time.sleep(0.05)
        ser.write(frame3)
        time.sleep(0.12)
        resp3 = ser.read(9)
        if len(resp3) >= 9 and resp3[0] == slave and resp3[1] == 3:
            print(f"  [+] Responding Slave {slave} (FC 03): len={len(resp3)}")
            found.append(slave)
        else:
            pass

print(f"\nScan complete. Responding slaves: {found}")
ser.close()
