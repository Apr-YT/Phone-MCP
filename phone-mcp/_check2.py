import sys
with open(r'C:\Users\AprYT\.workbuddy\phone-mcp\server.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

issues = []

# 1. Check _frida_run - does it correctly handle su -c with spaces in args?
for i, line in enumerate(lines):
    if 'def _frida_run' in line:
        body = ''.join(lines[i:i+8])
        # The cmd uses su -c with string concatenation - this is fragile
        if 'su -c' in body and 'shlex' not in body:
            # Check if extra_args could contain spaces
            pass  # This is OK for simple args but risky for complex ones
        print(f'[INFO] L{i+1}: _frida_run implementation')
        for j in range(i, min(i+8, len(lines))):
            print(f'  {lines[j].rstrip()}')
        break

# 2. Check t_frida_script - does it properly escape script content for shell?
print()
for i, line in enumerate(lines):
    if 'def t_frida_script' in line:
        body = ''.join(lines[i:i+30])
        if 'shlex.quote' in body:
            print(f'[OK] L{i+1}: t_frida_script uses shlex.quote')
        else:
            issues.append(f'[WARN] L{i+1}: t_frida_script does NOT use shlex.quote for script path')
        
        # Check if script content could break shell command
        if 'su -c' in body and 'quote' in body:
            print(f'[OK] L{i+1}: script path is quoted')
        break

# 3. Check t_frida_read_mem - Rhai script correctness
print()
for i, line in enumerate(lines):
    if 'def t_frida_read_mem' in line:
        body = ''.join(lines[i:i+25])
        # The Rhai script uses read_memory which expects (address, size)
        # But address is a string like "0x7f12345000" - Rhai needs it as int
        if 'read_memory(' in body:
            # Check if address is passed as string or int
            for j in range(i, min(i+25, len(lines))):
                if 'read_memory(' in lines[j]:
                    print(f'[CHECK] L{j+1}: {lines[j].strip()}')
                    # In Rhai, "0x..." string is NOT auto-converted to int
                    # Need to use string_to_int or hex parsing
                    if 'address' in lines[j] and 'to_string' not in lines[j]:
                        issues.append(f'[BUG] L{j+1}: read_memory address may not be parsed as int in Rhai (string "0x..." != int)')
        break

# 4. Check t_frida_scan_mem - Rhai search_bytes blob creation
print()
for i, line in enumerate(lines):
    if 'def t_frida_scan_mem' in line:
        body = ''.join(lines[i:i+25])
        for j in range(i, min(i+25, len(lines))):
            if 'search_bytes(' in lines[j]:
                print(f'[CHECK] L{j+1}: {lines[j].strip()}')
        break

# 5. Check t_frida_write_mem - write_memory correctness
print()
for i, line in enumerate(lines):
    if 'def t_frida_write_mem' in line:
        body = ''.join(lines[i:i+25])
        for j in range(i, min(i+25, len(lines))):
            if 'write_memory(' in lines[j]:
                print(f'[CHECK] L{j+1}: {lines[j].strip()}')
        break

# 6. Check t_frida_inject - does it pass lib path correctly?
print()
for i, line in enumerate(lines):
    if 'def t_frida_inject' in line:
        body = ''.join(lines[i:i+15])
        for j in range(i, min(i+15, len(lines))):
            if '_frida_run' in lines[j]:
                print(f'[CHECK] L{j+1}: {lines[j].strip()}')
                if j+1 < len(lines):
                    print(f'  L{j+2}: {lines[j+1].strip()}')
        break

print()
if issues:
    print('ISSUES:')
    for issue in issues:
        print(f'  {issue}')
else:
    print('NO ISSUES')
