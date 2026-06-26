def update_ini_content(ini_content: str, section: str, updates: dict) -> str:
    lines = ini_content.splitlines()
    out_lines = []
    in_target_section = False
    section_found = False
    updated_keys = set()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            current_section = stripped[1:-1]
            if current_section == section:
                in_target_section = True
                section_found = True
            else:
                if in_target_section:
                    # Leaving target section, append any missing keys
                    for k, v in updates.items():
                        if k not in updated_keys:
                            out_lines.append(f"{k} = {v}")
                            updated_keys.add(k)
                in_target_section = False

        if in_target_section and '=' in line and not stripped.startswith((';', '#')):
            k, v = line.split('=', 1)
            key_stripped = k.strip()
            if key_stripped in updates:
                out_lines.append(f"{k.rstrip()}= {updates[key_stripped]}")
                updated_keys.add(key_stripped)
                continue

        out_lines.append(line)

    if in_target_section:
        for k, v in updates.items():
            if k not in updated_keys:
                out_lines.append(f"{k} = {v}")
                updated_keys.add(k)

    if not section_found:
        if out_lines and out_lines[-1].strip() != "":
            out_lines.append("")
        out_lines.append(f"[{section}]")
        for k, v in updates.items():
            out_lines.append(f"{k} = {v}")

    return "\n".join(out_lines) + "\n"

test_ini = """# Main comment
[General]
a = 1
# This is b
b = 2

[CalibrationValues]
# Old offset
offset = 10
"""

print(update_ini_content(test_ini, "CalibrationValues", {"offset": "20", "gain": "1.5"}))
print("-----")
print(update_ini_content(test_ini, "NewSection", {"x": "y"}))
