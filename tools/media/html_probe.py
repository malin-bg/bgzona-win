"""Mechanical artifact checks. No production writes, no network."""
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit,parse_qs
import hashlib,json,sys,xml.etree.ElementTree as ET
from PIL import Image
ROOT=Path(__file__).resolve().parent
FIX=ROOT/"fixtures"
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
class Doc(HTMLParser):
    def __init__(self,s):
        super().__init__(convert_charrefs=True);self.tags=[];self.feed(s)
    def handle_starttag(self,tag,attrs):self.tags.append((tag,dict(attrs)))
    def select(self,tag=None,**attrs):
        return [a for t,a in self.tags if (not tag or t==tag) and all(a.get(k)==v for k,v in attrs.items())]
    def metadata(self):
        return {"meta":[a for t,a in self.tags if t=="meta" and (a.get("property","").startswith("og:") or a.get("name","").startswith("twitter:") or a.get("name")=="robots")],
                "canonical":[a.get("href") for t,a in self.tags if t=="link" and a.get("rel")=="canonical"]}
def local(out,url):return out/urlsplit(url).path.lstrip("/")
def inspect_image(path):
    with Image.open(path) as im:
        frames=[]; timing=[]
        for i in range(getattr(im,"n_frames",1)):
            im.seek(i);timing.append(im.info.get("duration"));frames.append(hashlib.sha256(im.convert("RGBA").tobytes()).hexdigest())
        im.seek(0)
        return {"format":im.format,"size":list(im.size),"mode":im.mode,"frames":getattr(im,"n_frames",1),
                "durations_ms":timing,"loop":im.info.get("loop"),"alpha_corner":im.convert("RGBA").getpixel((0,0))[3],"alpha_range":list(im.convert("RGBA").getextrema()[3]),
                "frame_pixel_hashes":frames,"sha256":sha(path),"bytes":path.stat().st_size}
