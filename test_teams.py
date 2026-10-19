import sys
sys.path.append("/home/ares/Documents/myserver")
import teams_service as ts

print("1. Checking status...")
st = ts.teams_get_status()
print("Status:", st)

print("2. Starting chromium if needed...")
ts.start_chromium()

print("3. Getting screenshot...")
shot = ts.teams_get_screenshot()
if shot:
    print(f"Got screenshot: {len(shot)} bytes")
    with open("/tmp/teams_test_shot.jpg", "wb") as f:
        f.write(shot)
else:
    print("No screenshot returned")

print("4. Re-checking status...")
st = ts.teams_get_status()
print("Status after start:", st)
