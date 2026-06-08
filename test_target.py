import time
from pytest_mes_core.instruments.fnirsi_dps150 import DPS150

print("Initializing...")
psu = DPS150(port="/dev/serial/by-id/usb-Artery_AT32_Virtual_Com_Port_114967F44486-if00", baud=115200)
psu.connect()

print("Testing live values...")
state = psu.read_state()
if state:
    print(f"Initial Live Values -> {state['output_voltage']}V, {state['output_current']}A, {state['output_power']}W")

print("Testing presets...")
presets = psu.get_presets()
print("Initial M1:", presets[0])
psu.set_preset(1, 5.0, 1.0)
time.sleep(0.5) # Give it time to register
presets = psu.get_presets()
print("Updated M1:", presets[0])

print("Testing brightness...")
psu.set_brightness(15)
time.sleep(0.5)

print("Enabling output...")
psu.set_voltage(12.0)
psu.set_current(1.5)
psu.output_on()
time.sleep(2.0) # Wait for telemetry push
state = psu.read_state()
if state:
    print(f"Enabled Live Values -> {state['output_voltage']}V, {state['output_current']}A, {state['output_power']}W")

print("Disabling output...")
psu.output_off()
time.sleep(0.5)
psu.disconnect()
print("Success!")
