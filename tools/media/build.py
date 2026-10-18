"""Local production build from a trusted frozen A2 release artifact; never deploys."""
from pathlib import Path
import argparse,json,subprocess,sys
from bridge import materialize
from media_support import fhash,data,digest,atomic
def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--artifact',required=True);p.add_argument('--expected-files',required=True)
 p.add_argument('--output-root',required=True);p.add_argument('--site',default=str(Path(__file__).resolve().parents[2]));p.add_argument('--hugo',default='hugo')
 a=p.parse_args();site=Path(a.site).resolve();out=Path(a.output_root).resolve();artifact=Path(a.artifact).resolve();expectedfile=Path(a.expected_files).resolve()
 if out.exists() or out.is_relative_to(site) or site.is_relative_to(out):raise SystemExit('Output must be a NEW external directory')
 if any((x/'.git').exists() for x in [out,*out.parents]):raise SystemExit('Output must be outside Git repositories')
 if any(x.is_symlink() for x in [out,*out.parents]):raise SystemExit('Symlink output refused')
 expected=json.loads(expectedfile.read_bytes().decode("utf-8-sig", "strict"))
 out.mkdir(parents=True)
 manifest,states=materialize(artifact,expected,out/'media')
 atomic(out/'validated-manifest.json',data(manifest))
 config='ignoreFiles = []\nresourceDir = '+json.dumps((out/'hugo-resources').as_posix())+'\n[params.media]\nenabled = true\n'
 config+='[[module.mounts]]\nsource = '+json.dumps((site/'assets').as_posix())+'\ntarget = "assets"\n'
 config+='[[module.mounts]]\nsource = '+json.dumps((out/'media').as_posix())+'\ntarget = "assets/bgz-media"\n'
 atomic(out/'media-config.toml',config.encode('utf8'))
 cmd=[a.hugo,'--source',str(site),'--config',str(site/'hugo.toml')+','+str(out/'media-config.toml'),'--destination',str(out/'public'),'--cacheDir',str(out/'cache'),'--baseURL','https://bgzona.win/','--environment','production','--noBuildLock','--printPathWarnings']
 q=subprocess.run(cmd,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=120)
 atomic(out/'hugo.log',(json.dumps(cmd,ensure_ascii=False)+'\n'+q.stdout+'\n'+q.stderr).encode('utf8'))
 receipt={'status':'BUILD_PASS_UNDEPLOYED' if q.returncode==0 else 'BUILD_FAIL','exit':q.returncode,'source_catalog_sha256':manifest['source_catalog_sha256'],'expected_files_sha256':fhash(expectedfile),'validated_manifest_sha256':fhash(out/'media/catalog.json'),'published':len(manifest['items']),'states':states,'command':cmd,'deployment':False}
 atomic(out/'build-receipt.json',data(receipt));print(json.dumps(receipt,ensure_ascii=False))
 if q.returncode:raise SystemExit(q.returncode)
if __name__=='__main__':main()
