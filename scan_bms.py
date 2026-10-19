import serial
import time
import struct

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
print(f"=== BMS SCANNER on {port} ===")

# Test combinations
baudrates = [9600, 19200, 115200]
parities = [serial.PARITY_NONE, serial.PARITY_EVEN]

found = False

for baud in baudrates:
    for par in parities:
        par_str = "None" if par == serial.PARITY_NONE else "Even"
        print(f"\n--- Testing Baud: {baud}, Parity: {par_str} ---")
        try:
            ser = serial.Serial(port, baudrate=baud, parity=par, timeout=0.3)
        except Exception as e:
            print(f"Failed open port: {e}")
            continue

        # 1. Modbus RTU probes on addresses 0, 1, 2, 3
        for slave_id in [1, 2, 0, 3, 4, 15]:
            # Read 0x0000 2 regs
            pdu = struct.pack(">BBHH", slave_id, 0x03, 0x0000, 2)
            frame = pdu + crc16(pdu)
            ser.reset_input_buffer()
            ser.write(frame)
            time.sleep(0.08)
            resp = ser.read(100)
            if resp:
                print(f"🔥 FOUND RESPONSE! Slave {slave_id}, Baud {baud}, Parity {par_str}, Modbus 0x03: {resp.hex(' ')}")
                found = True
                break
            
            # Read 0x04 (Input registers)
            pdu = struct.pack(">BBHH", slave_id, 0x04, 0x0000, 2)
            frame = pdu + crc16(pdu)
            ser.reset_input_buffer()
            ser.write(frame)
            time.sleep(0.08)
            resp = ser.read(100)
            if resp:
                print(f"🔥 FOUND RESPONSE! Slave {slave_id}, Baud {baud}, Parity {par_str}, Modbus 0x04: {resp.hex(' ')}")
                found = True
                break

        # 2. Telecom YD/T 1363.3 / Pace BMS ASCII probes (~21014642E00200FD36\r or similar)
        # Standard broadcast / address 1 info
        ascii_probes = [
            b"~21014642E00200FD36\r", # Pace BMS analog data
            b"~20014642E00200FD37\r",
            b"~25014642E00200FD32\r",
            b"~200146000000FDA5\r", # Handshake / serial num
            b"~210146000000FDA4\r",
            b"\x7E\x32\x30\x30\x31\x34\x36\x34\x32\x45\x30\x30\x32\x30\x30\x46\x44\x33\x37\x0D",
            b"info\r\n",
            b"help\r\n",
            b"MEAS\r\n"
        ]
        for probe in ascii_probes:
            ser.reset_input_buffer()
            ser.write(probe)
            time.sleep(0.1)
            resp = ser.read(100)
            if resp:
                print(f"🔥 FOUND ASCII RESPONSE! Baud {baud}, Parity {par_str}: {resp}")
                found = True
                break

        ser.close()
        if found:
            break
    if found:
        break

if not found:
    print("\n❌ Belum ada respon dari kabel.")
    print("Kemungkinan terbesar:")
    print("1. Kabel A dan B terbalik pada baut dongle USB.")
    print("2. Kabel belum terpasang kencang di pin 1 dan pin 2 RJ45.")
    print("3. Baterai dalam mode Sleep (tekan tombol RST/Power di baterai 3 detik agar BMS bangun).")
