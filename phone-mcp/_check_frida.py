import sys
with open(r'C:\Users\AprYT\.workbuddy\phone-mcp\server.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

issues = []

# 1. Check _FRIDA_BIN path
for i, line in enumerate(lines):
    if '_FRIDA_BIN' in line and '=' in line and 'def' not in line:
        print(f'[OK] L{i+1}: {line.strip()}')

# 2. Check _frida_run uses run_adb correctly
for i, line in enumerate(lines):
    if 'def _frida_run' in line:
        body = ''.join(lines[i:i+8])
        if 'run_adb' in body:
            print(f'[OK] L{i+1}: _frida_run uses run_adb')
        else:
            issues.append(f'[BUG] L{i+1}: _frida_run does NOT use run_adb')

# 3. Check each t_frida_* function exists and calls _frida_run
frida_funcs = ['t_frida_inject', 't_frida_attach', 't_frida_script', 
               't_frida_read_mem', 't_frida_write_mem', 't_frida_scan_mem', 't_frida_stealth']
for func in frida_funcs:
    found = False
    for i, line in enumerate(lines):
        if f'def {func}(' in line:
            found = True
            body = ''.join(lines[i:i+15])
            if '_frida_run' in body:
                print(f'[OK] L{i+1}: {func} calls _frida_run')
            else:
                issues.append(f'[BUG] L{i+1}: {func} does NOT call _frida_run')
            break
    if not found:
        issues.append(f'[MISSING] {func} not defined')

# 4. Check TOOLS registration
registered = set()
in_tools = False
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith('TOOLS = ['):
        in_tools = True
    if in_tools and '"name": "phone_frida_' in stripped:
        name = stripped.split('"')[3]
        registered.add(name)
        print(f'[OK] L{i+1}: registered {name}')

expected = {'phone_frida_inject', 'phone_frida_attach', 'phone_frida_script',
            'phone_frida_read_mem', 'phone_frida_write_mem', 'phone_frida_scan_mem', 'phone_frida_stealth'}
missing_reg = expected - registered
if missing_reg:
    issues.append(f'[BUG] Missing TOOLS registration: {missing_reg}')
else:
    print(f'[OK] All 7 frida tools registered in TOOLS')

# 5. Check for SHOT_DIR vs SHOOT_DIR typo
for i, line in enumerate(lines):
    if 'SHOOT_DIR' in line and 'frida' in ''.join(lines[max(0,i-5):i+5]):
        issues.append(f'[BUG] L{i+1}: SHOOT_DIR should be SHOT_DIR')

# 6. Check version
for i, line in enumerate(lines):
    if 'version' in line and '0.10' in line:
        print(f'[OK] L{i+1}: version 0.10.0')

print()
if issues:
    print('ISSUES FOUND:')
    for issue in issues:
        print(f'  {issue}')
else:
    print('NO ISSUES FOUND - all checks passed')
