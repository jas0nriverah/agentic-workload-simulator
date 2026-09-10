import pathlib,json,hashlib,struct,sqlite3,time
P=pathlib.Path(__file__).resolve().parent;aid='attempt-58a18edf3243fc4c-004-e9f234481206'
c=sqlite3.connect('file:'+str(P/'queue-ring8192-v1/queue.sqlite3')+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;attempt=c.execute('select * from attempts where attempt_id=?',(aid,)).fetchone();assert attempt['status']=='accepted';a=pathlib.Path(attempt['artifact_dir']);trace=a/'runner_attempts/attempt-001/telemetry_v2/linux_work';rows=[json.loads(l) for l in (trace/'raw_aggregates.jsonl').read_text().splitlines()];bnd={x['event_id']:x for x in [json.loads(l) for l in (trace/'action_boundaries.jsonl').read_text().splitlines()]};summary=json.loads((trace/'work_summary.json').read_text());assert summary['perf_buffer']['perf_pages_per_cpu_actual']==8192
raw=trace/'raw_events.bin';h=hashlib.sha256();count=0;offset=0;tokens=set();identities=set();joined=[]
with raw.open('rb') as f:
 for row in sorted(rows,key=lambda x:x['binary_event_stream']['offset_start']):
  e=row['binary_event_stream'];start,end=e['offset_start'],e['offset_end'];assert start==offset and end>=start and (end-start)%400==0;expected=row['event_count'];assert expected==row['required_event_count']==e['record_count']==(end-start)//400;assert row['event_records_complete'] and not row['aggregate_missing'];assert row['perf_lost_events']==0 and not row['event_callback_errors']
  for k in ['lost_event_records','lost_path_records','lost_pending_records','lineage_map_failures']:assert row['raw_aggregate'].get(k,0)==0,(k,row['raw_aggregate'].get(k))
  token=int(row['action_token']);assert token not in tokens;tokens.add(token);boundary=row['boundary'];assert boundary['event_id'] in bnd;assert boundary['command_sha256']==bnd[boundary['event_id']]['command_sha256'];identities.add(row['identity_binding_digest']);left=end-start;n=0
  while left:
   data=f.read(min(left,400*4096));assert data and len(data)%400==0;left-=len(data);h.update(data)
   for i in range(0,len(data),400):assert struct.unpack_from('<Q',data,i)[0]==token;n+=1
  assert n==expected;count+=n;offset=end;joined.append({'event_id':boundary['event_id'],'action_token':token,'event_count':n,'offset_start':start,'offset_end':end})
 assert f.read(1)==b''
assert offset==raw.stat().st_size==summary['raw_event_stream']['bytes_written'];assert count==summary['raw_event_stream']['records_written'];assert h.hexdigest()==summary['raw_event_stream']['sha256'];assert len(identities)==1 and len(joined)==len(bnd)
r={'status':'PASS','scope':'Actual previouslyfailedordinal31 repairedsingle-productionretry raw-only CPU completeness; nextcap4productionwave mustbemonitored','case_id':attempt['case_id'],'attempt_id':aid,'worker_id':attempt['worker_id'],'endpoint_id':attempt['endpoint_id'],'accepted_result_sha256':attempt['result_sha256'],'artifact_manifest_sha256':attempt['artifact_manifest_sha256'],'actual_pages_per_cpu':8192,'raw_packet_count':count,'raw_bytes':offset,'raw_sha256':h.hexdigest(),'action_rows':len(rows),'all_required_counts_match':True,'loss_counters_zero':True,'full_contiguous_stream_covered':True,'boundary_event_and_command_joins':'PASS','identity_binding_digest':next(iter(identities)),'joined_events':joined,'epoch':time.time()}
with (P/'ring8192-single-production-reconstruction-root-v1.json').open('x') as f:json.dump(r,f,indent=2);f.write('\n')
print(json.dumps({k:v for k,v in r.items() if k not in ['joined_events']}))
