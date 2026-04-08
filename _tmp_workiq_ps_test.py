import subprocess
cmd=["pwsh","-NoProfile","-Command","$q=$args[0]; & ''C:\\Users\\phtopnes\\AppData\\Roaming\\npm\\workiq.cmd'' ask -q $q","Reply with exactly: ok"]
proc=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
print('RC', proc.returncode)
print('OUT', proc.stdout)
print('ERR', proc.stderr)
