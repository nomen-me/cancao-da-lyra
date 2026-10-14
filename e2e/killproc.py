import os,sys,signal
pat=sys.argv[1]; me=os.getpid(); par=os.getppid()
for pid in filter(str.isdigit,os.listdir('/proc')):
    pid=int(pid)
    if pid in (me,par): continue
    try: cmd=open(f'/proc/{pid}/cmdline','rb').read().replace(b'\0',b' ').decode()
    except Exception: continue
    if cmd.startswith(pat): os.kill(pid,signal.SIGTERM); print("kill",pid,cmd[:60])
