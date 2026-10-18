"""Image validation/file helpers extracted unchanged from the proven A2 importer."""
from pathlib import Path
from io import BytesIO
import base64,hashlib,json,os,uuid
from PIL import Image
EXTS={".jpg":"JPEG",".jpeg":"JPEG",".png":"PNG",".webp":"WEBP",".gif":"GIF"}
class Reject(RuntimeError):pass

class Crash(RuntimeError):pass

def data(o):return json.dumps(o,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf8")

def digest(b):return hashlib.sha256(b).hexdigest()

def fhash(p):return digest(p.read_bytes()) if p.is_file() else None

def probe(b,ext,og=False):
    try:
        with Image.open(BytesIO(b)) as im:fmt=im.format;im.verify()
        with Image.open(BytesIO(b)) as im:
            w,h=im.size;frames=getattr(im,"n_frames",1);loop=im.info.get("loop");dur=[]
            if w*h>40_000_000 or frames>1000 or w*h*frames>300_000_000:raise Reject("IMAGE_LIMIT")
            for n in range(frames):im.seek(n);im.load();dur.append(im.info.get("duration"))
    except Reject:raise
    except Exception as e:raise Reject("CORRUPT_IMAGE:"+type(e).__name__) from e
    if EXTS.get(ext.lower())!=fmt:raise Reject("WRONG_EXTENSION")
    if og and fmt not in ("JPEG","PNG"):raise Reject("INVALID_OG_FORMAT")
    return {"sha256":digest(b),"format":fmt,"ext":".jpg" if fmt=="JPEG" else "."+fmt.lower(),
            "width":w,"height":h,"frames":frames,"durations_ms":dur,"loop":loop,"bytes":len(b)}

def atomic(p,b,tear=False):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_name(p.name+"."+uuid.uuid4().hex+".tmp")
    with tmp.open("wb") as f:f.write(b[:max(1,len(b)//2)] if tear else b);f.flush();os.fsync(f.fileno())
    if tear:raise Crash("INTERRUPTED_TEMP_WRITE")
    os.replace(tmp,p)
