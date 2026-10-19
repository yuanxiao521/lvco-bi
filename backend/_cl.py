import subprocess, sys
sys.stdout.reconfigure(encoding='utf-8')
sql = ("update canvases set blocks = ("
       "select coalesce(jsonb_agg(b), '[]'::jsonb) from jsonb_array_elements(blocks) b "
       "where coalesce(b->>'title','') not like '%粒度验证%' "
       "and coalesce(b->>'title','') not like '%验收-%' "
       "and coalesce(b->>'title','') not like '%排序验证%' "
       "and coalesce(b->>'title','') not like '%测试%') "
       "where id='fe110f65-1b5e-4055-9799-5e7bf2329e42' returning jsonb_array_length(blocks);")
out = subprocess.run(['docker','exec','-i','lvco-postgres','psql','-U','lvco','-d','lvco_bi','-t','-A','-c', sql], capture_output=True, timeout=60)
print('rc=', out.returncode)
print('out=', out.stdout.decode('utf-8','replace').strip())
print('err=', out.stderr.decode('utf-8','replace').strip()[:200])