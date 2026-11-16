import {api} from './client.js';
export const ledger={
  state:()=>api('/ledger'),
  observations:(limit=300)=>api('/ledger/observations?limit='+limit),
  observe:data=>api('/ledger/observations',{method:'POST',body:JSON.stringify(data)}),
  correct:(id,data)=>api('/ledger/observations/'+id+'/correction',{method:'POST',body:JSON.stringify(data)}),
  merges:()=>api('/ledger/merges'),
  preview:(source_token,target_token)=>api('/ledger/merges/preview',{method:'POST',body:JSON.stringify({source_token,target_token})}),
  merge:data=>api('/ledger/merges',{method:'POST',body:JSON.stringify(data)}),
  revoke:(id,reason)=>api('/ledger/merges/'+id+'/revoke',{method:'POST',body:JSON.stringify({reason})}),
  adjudications:(status='')=>api('/ledger/adjudications'+(status?'?status='+status:'')),
  decide:(id,decision,note)=>api('/ledger/adjudications/'+id+'/decide',{method:'POST',body:JSON.stringify({decision,note})}),
  frames:()=>api('/ledger/frames'),
  setFrame:(zone,size)=>api('/ledger/frames/'+encodeURIComponent(zone),{method:'PUT',body:JSON.stringify({size})}),
  importDataset:id=>api('/ledger/import-dataset/'+id,{method:'POST'}),
  batches:()=>api('/ledger/batches'),
  batch:id=>api('/ledger/batches/'+id),
  createBatch:name=>api('/ledger/batches',{method:'POST',body:JSON.stringify({name})}),
  recompute:id=>api('/ledger/batches/'+id+'/recompute',{method:'POST'}),
  publish:(id,key)=>api('/ledger/batches/'+id+'/publish',{method:'POST',body:JSON.stringify({idempotency_key:key})}),
  history:()=>api('/ledger/history')
};
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
