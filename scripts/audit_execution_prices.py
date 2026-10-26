"""Read-only, field-aware price and trade-card inventory (no vendor calls)."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import re
import subprocess


def audit(root: Path, values: set[float]) -> dict:
    inventory = subprocess.run(
        ['rg', '--files', '-uu', str(root), '-g', '!.git/**', '-g', '!**/.git/**',
         '-g', '!**/.venv*/**', '-g', '!**/node_modules/**'], text=True, encoding='utf-8', capture_output=True
    )
    files = inventory.stdout.splitlines()
    counts = Counter()
    hits, cards, errors, dates, text_hits = [], [], [], [], []
    if inventory.returncode:
        errors.append({'inventory_error': inventory.stderr})
    def walk(obj, path, pointer='', context=None):
        if isinstance(obj, dict):
            ctx = {k: obj[k] for k in ('contract','contract_code','product','exchange',
                   'trade_date','source_date','price_type','series_key') if k in obj}
            ctx = {**(context or {}), **ctx}
            if any(k in obj for k in ('idea_id','trade_ideas','trade_cards','legs')):
                cards.append({'file':path,'pointer':pointer,'keys':list(obj)})
            for k,v in obj.items():
                walk(v,path,pointer+'/'+k,ctx)
        elif isinstance(obj,list):
            for i,v in enumerate(obj): walk(v,path,pointer+'/'+str(i),context)
        elif isinstance(obj,(int,float)) and not isinstance(obj,bool) and obj in values:
            field=pointer.rsplit('/',1)[-1]
            counts[field]+=1
            hits.append({'file':path,'pointer':pointer,'value':obj,'context':context})
    for name in files:
        p=Path(name)
        if p.suffix.lower() not in {'.json','.md','.txt','.log','.yaml','.yml','.py','.raw','.csv','.parquet'}:
            continue
        counts['files_scanned']+=1
        try:
            if p.suffix=='.parquet':
                import pyarrow.parquet as pq
                frame=pq.ParquetFile(p).read().to_pandas()
                for column in frame.select_dtypes(include='number').columns:
                    for idx in frame.index[frame[column].isin(values)]:
                        row=frame.loc[idx]
                        context={k:str(row[k]) for k in ('trade_date','source_date','contract','underlying_contract','product','exchange') if k in frame}
                        walk(float(row[column]),str(p),f'/rows/{idx}/{column}',context)
                counts['parquet_rows']+=len(frame)
                continue
            text=p.read_text(encoding='utf-8')
            if p.suffix=='.json':
                payload=json.loads(text)
                if isinstance(payload,dict) and payload.get('trade_date'): dates.append(str(payload['trade_date']))
                walk(payload,str(p))
            elif p.suffix=='.csv':
                for i,row in enumerate(csv.DictReader(text.splitlines())):
                    for key,value in row.items():
                        try:
                            row[key]=float(value)
                        except (TypeError,ValueError):
                            pass
                    walk(row,str(p),'/rows/'+str(i))
            for i,line in enumerate(text.splitlines(),1):
                if re.search(r'idea_\d{8}_\d+|SA(?:701|2701).*JM2701|JM2701.*SA(?:701|2701)',line):
                    cards.append({'file':str(p),'line':i,'text':line[:600]})
                if p.suffix in {'.md','.txt','.log','.raw','.yml','.yaml'}:
                    numbers=re.findall(r'(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w.])',line)
                    if any(float(n.replace(',','')) in values for n in numbers):
                        text_hits.append({'file':str(p),'line':i,'text':line[:600]})
        except (UnicodeError,ValueError,OSError) as exc:
            errors.append({'file':str(p),'error':type(exc).__name__})
    return {'root':str(root),'counts':dict(counts),'date_range': [min(dates),max(dates)] if dates else [],
            'numeric_hits':hits,'text_hits':text_hits,'card_candidates':cards,'unreadable':errors,
            'limits':'Numeric equality is not lineage. Binary archives and unreachable Git history are not covered; card candidates require semantic review.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('roots',nargs='+',type=Path)
    parser.add_argument('--values',nargs='+',type=float,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps([audit(r,set(args.values)) for r in args.roots],ensure_ascii=False,indent=2),encoding='utf-8')
