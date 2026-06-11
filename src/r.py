import os,sys,io,json,math,time,hmac,hashlib,random,csv,urllib.parse,urllib.request
from PIL import Image
import fitz,requests,boto3

I=int(os.environ["I"]);N=int(os.environ["N"]);L=os.environ["L"]
K=os.environ["S1"].encode();E=os.environ["S2"];A=os.environ["S3"];SK=os.environ["S4"]
B=os.environ.get("S5","b1");C=os.environ["S6"];T=os.environ["S7"];D=os.environ["S8"]
H=os.environ["S9"];W=os.environ.get("SA","");X=os.environ.get("SB","")
TD=([X] if str(X).startswith("http") else [f"{i}{X}" for i in range(1,31)])
Q=85;CL=3000;UA="Mozilla/5.0";it_cat="医书"

s3=boto3.client("s3",endpoint_url=E,aws_access_key_id=A,aws_secret_access_key=SK,region_name="auto")

def lg(m):print(f"[{I}][{time.strftime('%H:%M:%S')}]{m}",flush=True)

def gt(u,tr=8,to=120):
    for i in range(tr):
        t=(f"{W}/fetch?url={urllib.parse.quote(u,safe='')}" if(W and i%2==0)else u)
        try:
            r=urllib.request.Request(t,headers={"User-Agent":UA,"Referer":f"https://{H}/"})
            with urllib.request.urlopen(r,timeout=to)as rs:
                b=rs.read()
                if b and len(b)>300:return b
        except urllib.error.HTTPError as e:
            if e.code in(403,429):time.sleep(min(90,12*(i+1)));continue
            time.sleep(2)
        except:time.sleep(2)
    return None

def gj(u):
    b=gt(u,tr=4);return json.loads(b)if b else None

def pg(s,W,H_):
    co=math.ceil(W/CL);ro=math.ceil(H_/CL)
    cv=Image.new("RGB",(W,H_),(255,255,255));ok=True
    for cy in range(ro):
        for cx in range(co):
            x,y=cx*CL,cy*CL;w,h=min(CL,W-x),min(CL,H_-y)
            b=gt(f"{s}/{x},{y},{w},{h}/full/0/default.jpg")
            if not b:ok=False;continue
            try:cv.paste(Image.open(io.BytesIO(b)).convert("RGB"),(x,y))
            except:ok=False
    return cv,ok

def wp(im):
    bf=io.BytesIO();im.save(bf,format="WEBP",quality=Q,method=4);return bf.getvalue()

def rp(k,d,ct):
    for a in range(3):
        try:s3.put_object(Bucket=B,Key=k,Body=d,ContentType=ct);return True
        except Exception as e:lg(f"x{a}:{e}");time.sleep(0.5*(a+1))
    return False

def tp(p,bk,ti,v,sb,pc,by):
    ts=str(int(time.time()));sh=hashlib.sha256(by).hexdigest()
    pa="/upload";ms=f"{ts}\n{pa}\n{sh}".encode()
    sg=hmac.new(K,ms,hashlib.sha256).hexdigest()
    dm=random.choice(TD);u=(dm+pa) if str(dm).startswith("http") else f"https://{dm}{pa}"
    pr={"part":p,"cat":it_cat,"book":bk,"title":ti,"vol":v,"sub":sb or"","pages":pc,"ts":ts,"sig":sg}
    for a in range(5):
        try:
            r=requests.post(u,params=pr,data=by,timeout=600)
            if r.status_code==200:return True
            lg(f"t{r.status_code}")
        except Exception as e:lg(f"te:{e}")
        time.sleep(2*(a+1))
    return False

def dq(s,p):
    u=f"https://api.cloudflare.com/client/v4/accounts/{C}/d1/database/{D}/query"
    for a in range(5):
        try:
            r=requests.post(u,headers={"Authorization":f"Bearer {T}","Content-Type":"application/json"},json={"sql":s,"params":p},timeout=60)
            j=r.json()
            if j.get("success"):return True
            lg(f"d:{j}");return False
        except Exception as e:lg(f"de:{e}");time.sleep(1+a)
    return False

def wd(bk,ti,pc,p,rn,cc):
    s="""INSERT OR REPLACE INTO books_assets_v2(book_id,book_title,webp_prefix,page_count,source_root,source_relative_path,upload_status,webp_status,ocr_status,sueai_status,evidence_status,entity_status,graph_status,rights_status,frontend_visible,collection,req_no,category_code,part,created_at,updated_at)VALUES(?,?,?,?,?,?,'done','done','pending','pending','pending','pending','pending','public_domain',1,'overseas',?,?,?,strftime('%s','now'),strftime('%s','now'))"""
    return dq(s,[bk,ti,f"book/{bk}/",pc,"x","",rn,cc,p])

def do(it):
    global it_cat
    iid=it["id"];bk=it["book_id"];ti=it["title"];pa=it["part"];it_cat=it.get("cat","医书");vo=int(it["vol"]);rn=it["req_no"];cc=it.get("category_code","");mu=it["manifest"]
    lg(f"s {bk}-{vo:02d}")
    mn=gj(mu)
    if not mn:lg("mx");return None
    sb="";lb=mn.get("label","")
    if isinstance(lb,str):sb=lb
    elif isinstance(lb,dict):
        for v in lb.values():
            if isinstance(v,list)and v:sb=str(v[0]);break
    cvs=mn["sequences"][0]["canvases"];tt=len(cvs)
    if tt==0:return None
    doc=fitz.open();k=0;fl=0
    for i,c in enumerate(cvs,1):
        W_,H_=int(c.get("width",0)),int(c.get("height",0))
        rs=c["images"][0]["resource"];sv=rs.get("service")or{}
        sd=(sv.get("@id")or sv.get("id")or"").rstrip("/")
        if not(sd and W_ and H_):fl+=1;continue
        im,ok=pg(sd,W_,H_)
        if not ok:fl+=1;continue
        wb=wp(im)
        ky=f"book/{bk}/page_{i:04d}.webp"
        if not rp(ky,wb,"image/webp"):fl+=1;continue
        jb=io.BytesIO();im.save(jb,"JPEG",quality=95)
        try:
            fi=fitz.open(stream=jb.getvalue(),filetype="jpg");pb=fi.convert_to_pdf();fi.close()
            doc.insert_pdf(fitz.open("pdf",pb));k+=1
        except:fl+=1
    if fl>0 or k==0:
        lg(f"!{k}/{fl}");doc.close()
        return{"book_id":bk,"vol":vo,"ok":False,"kept":k,"failed":fl}
    by=doc.tobytes();doc.close()
    po=tp(pa,bk,ti,vo,sb,k,by)
    di=wd(f"{bk}-{vo:02d}",ti,k,pa,rn,cc)
    lg(f"ok {bk}-{vo:02d}:{k}p t={po} d={di}")
    return{"book_id":bk,"vol":vo,"ok":True,"kept":k,"pdf":po,"d1":di}

def main():
    rs=[]
    with open(L,encoding="utf-8-sig")as f:
        for r in csv.DictReader(f):rs.append(r)
    mn=[r for i,r in enumerate(rs)if i%N==I]
    lg(f"{I}/{N} {len(mn)}")
    out=[]
    for it in mn:
        try:
            r=do(it)
            if r:out.append(r)
        except Exception as e:lg(f"e:{e}")
    on=sum(1 for r in out if r.get("ok"))
    lg(f"= {on}/{len(mn)}")
    open("r.json","w",encoding="utf-8").write(json.dumps(out,ensure_ascii=False))

if __name__=="__main__":main()
