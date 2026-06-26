import serial
import time

port = "/dev/serial/by-id/usb-wch.cn_USB_Quad_Serial_0123456789-if04"
baudrate = 115200

bash_snippet = (
    'attempt=1; media_found=0; while [ $attempt -le 10 ]; do '
    'emmc_base=""; '
    'for b in /sys/block/*boot0; do '
    'if [ -e "$b" ]; then '
    'b_name="${b##*/}"; emmc_base="${b_name%boot0}"; break; '
    'fi; done; '
    'for d in /sys/block/sd* /sys/block/mmcblk*; do '
    'if [ -d "$d" ]; then '
    'd_name="${d##*/}"; '
    'if [ -n "$emmc_base" ]; then '
    'case "$d_name" in "$emmc_base"*) continue;; esac; '
    'fi; '
    'if [ -f "$d/removable" ] && [ "$(cat $d/removable)" = "1" ]; then '
    'media_found=1; break; fi; '
    'case "$d_name" in mmcblk*) '
    'if [ -f "$d/size" ] && [ "$(cat $d/size)" -gt 0 ]; then '
    'media_found=1; break; fi;; esac; '
    'fi; done; '
    'if [ "$media_found" = "1" ]; then break; fi; '
    'attempt=$((attempt+1)); sleep 1; done; '
    'if [ "$media_found" = "1" ]; then echo "__MES_MEDIA_OK__"; '
    'else echo "__MES_MEDIA_MISSING__"; fi\n'
)

print(f"Connecting to {port} at {baudrate} baud...")
try:
    with serial.Serial(port, baudrate, timeout=2) as ser:
        print("Flushing buffers...")
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        
        print("Sending newline to get prompt...")
        ser.write(b'\n')
        time.sleep(0.5)
        print(ser.read_all().decode(errors='replace'))
        
        print(f"Sending test snippet: {bash_snippet}")
        ser.write(bash_snippet.encode('utf-8'))
        
        print("Waiting for response (up to 15 seconds)...")
        start_time = time.time()
        while time.time() - start_time < 15:
            data = ser.read(1024)
            if data:
                print(data.decode(errors='replace'), end='', flush=True)
                if b'__MES_MEDIA_OK__' in data or b'__MES_MEDIA_MISSING__' in data:
                    print("\n\n--- Result detected! ---")
                    break
            time.sleep(0.1)
except Exception as e:
    print(f"Error: {e}")
