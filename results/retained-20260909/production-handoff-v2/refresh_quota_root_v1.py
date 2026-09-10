#!/usr/bin/env python3
import hashlib,importlib.util,json,os,pathlib,subprocess,sys,time
P=pathlib.Path(__file__).resolve().parent
controller=P.parent/'storage-reclamation-v1/production_storage_controller_v2.py'
assert hashlib.sha256(controller.read_bytes()).hexdigest()=='08bd4c2cfe75b0c49bb987cd3993c5438c710f7c9f6cf12fa3c6ddc2b1bdf208'
spec=importlib.util.spec_from_file_location('quota_controller',controller);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
raw=subprocess.run(['ssh','-F','/dev/null','-o','BatchMode=yes','-o','ConnectTimeout=10','-S','/tmp/jriverah3-pace-fresh.sock','jriverah3@128.61.254.151','lfs quota -u jriverah3 /storage/ice1'],capture_output=True,text=True,check=True,timeout=30).stdout
v={'schema':'assignment.pace-quota-receipt.v1','status':'PASS','captured_epoch':time.time(),'available_bytes':m.pace_available(raw),'raw_output':raw}
out=pathlib.Path(sys.argv[1]);out.parent.mkdir(parents=True,exist_ok=True);tmp=out.with_name(out.name+'.tmp-'+str(os.getpid()))
with tmp.open('x') as s:json.dump(v,s,indent=2);s.write('\n');s.flush();os.fsync(s.fileno())
os.replace(tmp,out)
print(json.dumps({'status':'PASS','available_bytes':v['available_bytes'],'receipt':str(out)}))
