import py_compile
for f in ['app.py', 'neesa_lw.py', 'neesa_kot.py', 'attendance_unified.py', 'my_portal.py']:
    py_compile.compile(f, doraise=True)
print('OK')
