"""Validate a frozen A2 artifact and derive an A1 resource manifest. No identity allocation."""
from pathlib import Path,PurePosixPath
import json,hashlib,re,shutil
from media_support import Reject,probe,data,digest,fhash,atomic
COLS={'kolaji':'p','poslaniya':'m'}
def safe(base,rel):
 if not isinstance(rel,str) or '\\' in rel or ':' in rel:raise Reject('BRIDGE_PATH')
 p=PurePosixPath(rel)
 if p.is_absolute() or '..' in p.parts or not p.parts or p.as_posix()!=rel or any(x.endswith((' ','.')) for x in p.parts):raise Reject('BRIDGE_PATH')
 result=base.joinpath(*p.parts)
 if not result.resolve().is_relative_to(base.resolve()) or any((p.is_symlink() or p.is_junction()) for p in [result,*result.parents] if p.is_relative_to(base)):raise Reject('BRIDGE_PATH')
 return result

def strict_json(raw):
 def unique(pairs):
  result={}
  for key,value in pairs:
   if key in result:raise Reject('BRIDGE_DUPLICATE_JSON_KEY')
   result[key]=value
  return result
 return json.loads(raw.decode('utf-8-sig','strict'),object_pairs_hook=unique)

def derive(artifact,expected):
 artifact=Path(artifact).resolve()
 actual={}
 for p in artifact.rglob('*'):
  rel=p.relative_to(artifact).as_posix();safe(artifact,rel)
  if p.is_file():
   if p.stat().st_nlink!=1:raise Reject('BRIDGE_LINK')
   actual[rel]=fhash(p)
 if actual!=expected:raise Reject('BRIDGE_ARTIFACT_SET_OR_HASH')
 for rel in actual:safe(artifact,rel)
 raw_catalog=(artifact/'catalog.json').read_bytes()
 if digest(raw_catalog)!=expected.get('catalog.json'):raise Reject('BRIDGE_CATALOG_CHANGED')
 cat=strict_json(raw_catalog)
 if set(cat)!= {'schema','items'} or cat['schema']!=1 or not isinstance(cat['items'],list):raise Reject('BRIDGE_SCHEMA')
 rows=[];seen=set();urls=set();used={'catalog.json'};states={}
 for row in cat['items']:
  basic={'id','collection','page_url','state'};published=basic|{'revision','original','sha256','description','slideshow'}
  col=row.get('collection');key=row.get('id');state=row.get('state')
  if col not in COLS or not re.fullmatch('bgzona-'+COLS[col]+'-[0-9]{5}',str(key)) or key in seen:raise Reject('BRIDGE_ID')
  n=int(key[-5:]);url=f'/{col}/'+('kolaj-' if col=='kolaji' else 'p-')+f'{n:05d}/'
  if n<1 or row['page_url']!=url or url in urls or state not in ('published','archived','removed'):raise Reject('BRIDGE_PAGE_STATE')
  seen.add(key);urls.add(url);states[key]=state
  if state!='published':
   if set(row)!=basic:raise Reject('BRIDGE_PRIVATE_FIELDS')
   continue
  if set(row) not in (published,published|{'og'}):raise Reject('BRIDGE_FIELDS')
  revision=row['revision'];rawpath=row['original']
  if type(revision)!=int or revision<1 or type(row['slideshow'])!=bool or (col!='kolaji' and row['slideshow']):raise Reject('BRIDGE_REV_SLIDES')
  if not isinstance(row['description'],str) or len(row['description'].encode('utf8'))>32768:raise Reject('BRIDGE_TEXT')
  if not re.fullmatch('[0-9a-f]{64}',row['sha256']):raise Reject('BRIDGE_SHA')
  if not rawpath.startswith('/'):raise Reject('BRIDGE_ORIGINAL_PATH')
  rel=rawpath[1:];p=safe(artifact,rel);meta=probe(p.read_bytes(),p.suffix)
  suffix='' if revision==1 else f'-r{revision:04d}-{meta["sha256"][:12]}'
  if rawpath!=f'/{col}/full/{key}{suffix}{meta["ext"]}' or meta['sha256']!=row['sha256']:raise Reject('BRIDGE_ORIGINAL_IDENTITY')
  from PIL import Image
  with Image.open(p) as im:alpha=im.convert('RGBA').getextrema()[3][0]<255
  target={'collection':col,'stem':key,'number':n,'revision':revision,'sha256':row['sha256'],'ext':meta['ext'],
          'source':rel,'publicPath':rel,'alpha':alpha,'slideshow':row['slideshow'],'txt':f'txt/{key}.txt'}
  used.add(rel)
  if row.get('og'):
   ogrel=row['og'].removeprefix('/');op=safe(artifact,ogrel);om=probe(op.read_bytes(),op.suffix,og=True)
   if row['og']!=f'/{col}/og/{key}-{om["sha256"][:12]}{om["ext"]}':raise Reject('BRIDGE_OG_PATH')
   target.update(og=ogrel,ogPublicPath=ogrel);used.add(ogrel)
  rows.append(target)
 if used!=set(actual):raise Reject('BRIDGE_UNREFERENCED_FILE')
 return {'schema':1,'source_catalog_sha256':digest(raw_catalog),'items':sorted(rows,key=lambda x:(x['collection'],x['number']))},states,cat

def materialize(artifact,expected,destination):
 manifest,states,cat=derive(artifact,expected)
 dest=Path(destination).resolve()
 if dest.exists():raise Reject('BRIDGE_DESTINATION')
 dest.mkdir(parents=True)
 for rel in expected:
  if rel!='catalog.json':
   p=safe(dest,rel);p.parent.mkdir(parents=True,exist_ok=True)
   raw=(Path(artifact)/rel).read_bytes()
   if digest(raw)!=expected[rel]:raise Reject('BRIDGE_COPY_HASH_CHANGED')
   atomic(p,raw)
 for row in cat['items']:
  if row['state']=='published':atomic(safe(dest,f'txt/{row["id"]}.txt'),row['description'].encode('utf8'))
 atomic(dest/'catalog.json',data(manifest))
 return manifest,states
