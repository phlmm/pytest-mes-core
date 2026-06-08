from pytest_mes_core.instruments.fnirsi_dps150 import DPS150
import time

psu = DPS150(port="/dev/ttyACM3", baud=115200)
psu.connect()
psu.set_voltage(12.0)
psu.set_current(2.0)
psu.output_on()

state = psu.read_state()
if state:
    print(f"Drawing {state['output_current']}A at {state['output_voltage']}V")
psu.disconnect()
