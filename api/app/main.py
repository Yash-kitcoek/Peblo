import json
import os
import shutil
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

APP_ROOT = Path(__file__).resolve().parents[1]
# In Docker the API lives at /app; locally the supplied seed files live beside /api.
BASE = APP_ROOT.parent if (APP_ROOT.parent / "seed_shows.json").exists() else APP_ROOT
STORAGE = Path(os.getenv("STORAGE_DIR", str(BASE / "data" / "storage")))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE / 'data' / 'peblo.db'}")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase): pass

class Show(Base):
    __tablename__ = "shows"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), unique=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True)
    section: Mapped[str | None] = mapped_column(String(50), nullable=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    synopsis: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    episodes: Mapped[list["Episode"]] = relationship(back_populates="show", cascade="all, delete-orphan")
    artwork: Mapped[list["Artwork"]] = relationship(back_populates="show", cascade="all, delete-orphan")

class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("content_group", "language", name="uq_content_group_language"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(80), unique=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"))
    season_number: Mapped[int] = mapped_column(Integer)
    episode_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str] = mapped_column(String(8))
    content_group: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    show: Mapped[Show] = relationship(back_populates="episodes")

class Artwork(Base):
    __tablename__ = "artwork"
    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id"))
    kind: Mapped[str] = mapped_column(String(20))
    path: Mapped[str] = mapped_column(String(500))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(Integer)
    show: Mapped[Show] = relationship(back_populates="artwork")

class PublishRun(Base):
    __tablename__ = "publish_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    actor: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(20))
    show_count: Mapped[int] = mapped_column(Integer, default=0)
    episode_count: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

class ImportIssue(Base):
    __tablename__ = "import_issues"
    id: Mapped[int] = mapped_column(primary_key=True)
    message: Mapped[str] = mapped_column(Text)

def db():
    with SessionLocal() as session:
        yield session

def role(authorization: str | None = Header(None)):
    token = (authorization or "").removeprefix("Bearer ")
    if token == os.getenv("ADMIN_TOKEN", "admin-demo"): return "admin"
    if token == os.getenv("EDITOR_TOKEN", "editor-demo"): return "editor"
    raise HTTPException(401, "Sign in with an editor or admin token.")

def require_admin(user: str = Depends(role)):
    if user != "admin": raise HTTPException(403, "Publishing is available to admins only.")
    return user

SPECS = {"poster": (2 / 3, (600, 900)), "banner": (16 / 9, (1280, 720)), "thumbnail": (16 / 9, (640, 360))}
def art_map(show: Show): return {a.kind: f"/storage/{a.path}" for a in show.artwork}

def report(session: Session):
    issues = []
    issues.extend({"type":"duplicate_variant", "message":issue.message} for issue in session.scalars(select(ImportIssue)).all())
    published = session.scalars(select(Episode).where(Episode.status == "published")).all()
    by_group = defaultdict(list)
    for ep in published:
        by_group[(ep.content_group, ep.language)].append(ep)
        if not ep.duration_seconds: issues.append({"type":"duration", "episode":ep.external_id, "message":f"{ep.title} needs a duration before it can be published."})
        kinds = {a.kind for a in ep.show.artwork}
        missing = {"poster","banner","thumbnail"} - kinds
        if missing: issues.append({"type":"artwork", "show":ep.show.title, "message":f"{ep.show.title} is missing {', '.join(sorted(missing))} artwork."})
        if not ep.show.section: issues.append({"type":"section", "show":ep.show.title, "message":f"{ep.show.title} needs a section before it can be published."})
    for (_, _), eps in by_group.items():
        if len(eps) > 1: issues.append({"type":"duplicate_variant", "episode":eps[0].external_id, "message":f"{eps[0].content_group} has more than one {eps[0].language} variant. Keep one variant or change its language."})
    dedup = {i["message"]: i for i in issues}
    return {"blocked": bool(dedup), "issue_count":len(dedup), "issues":list(dedup.values())}

def catalogue(session: Session):
    episodes = session.scalars(select(Episode).where(Episode.status == "published")).all()
    groups = defaultdict(list)
    for ep in episodes: groups[(ep.show_id, ep.content_group)].append(ep)
    shows = {}
    for (show_id, _), variants in groups.items():
        show = variants[0].show
        if not show.section: continue
        item = {"content_group":variants[0].content_group,"title":variants[0].title,"season_number":variants[0].season_number,"episode_number":variants[0].episode_number,"duration_seconds":variants[0].duration_seconds,"languages":sorted({v.language for v in variants})}
        record = shows.setdefault(show.id, {"id":show.id,"title":show.title,"slug":show.slug,"section":show.section,"categories":show.categories,"synopsis":show.synopsis,"artwork":art_map(show),"episodes":[]})
        record["episodes"].append(item)
    rows = defaultdict(list)
    for show in shows.values():
        show["episodes"].sort(key=lambda e:(e["season_number"], e["episode_number"],e["title"].lower()))
        rows[show["section"]].append(show)
    for values in rows.values(): values.sort(key=lambda s:s["title"].lower())
    return {"published_at":datetime.now(timezone.utc).isoformat(),"sections":[{"name":k,"shows":v} for k,v in sorted(rows.items())]}

def write_catalog(data: dict):
    STORAGE.mkdir(parents=True, exist_ok=True)
    tmp = STORAGE / f"catalogue.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STORAGE / "catalogue.json")

app = FastAPI(title="Peblo TV Mini")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","), allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    STORAGE.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as s:
        if not s.scalar(select(Show.id)):
            seed = json.loads((BASE / "seed_shows.json").read_text())
            grouped = defaultdict(list)
            for row in seed: grouped[row["slug"]].append(row)
            for slug, rows in grouped.items():
                r = rows[0]; show = Show(title=r["show_title"],slug=slug,section=r["section"],categories=r["categories"],synopsis=r["synopsis"],status="published" if any(x["status"]=="published" for x in rows) else "draft")
                s.add(show); s.flush()
                for kind in {k for row in rows for k in row["artwork_available"]}:
                    # Seed metadata represents content already approved; uploads enforce real dimensions.
                    s.add(Artwork(show_id=show.id,kind=kind,path=f"placeholders/{kind}.svg",width=SPECS.get(kind,(1,(1,1)))[1][0],height=SPECS.get(kind,(1,(1,1)))[1][1],size_bytes=1000))
                seen = set()
                for e in rows:
                    # preserve the bad duplicate in the report without violating DB uniqueness
                    key=(e["content_group"],e["language"])
                    if key in seen:
                        s.add(ImportIssue(message=f"{e['content_group']} has more than one {e['language']} variant. Keep one variant or change its language."))
                        continue
                    seen.add(key)
                    s.add(Episode(external_id=e["episode_id"],show_id=show.id,season_number=e["season_number"],episode_number=e["episode_number"],title=e["episode_title"],duration_seconds=e["duration_seconds"],language=e["language"],content_group=e["content_group"],status=e["status"]))
            s.commit(); write_catalog(catalogue(s))

@app.get("/health")
def health(): return {"status":"ok"}

@app.get("/catalog")
def get_catalog():
    path=STORAGE / "catalogue.json"
    if not path.exists(): raise HTTPException(404,"No catalogue has been published yet.")
    return json.loads(path.read_text())

@app.get("/catalog/search")
def search_catalog(q: str="", category: str | None=None, language: str | None=None, section: str | None=None):
    results=[]; needle=q.lower().strip()
    for row in get_catalog()["sections"]:
        if section and row["name"] != section: continue
        for show in row["shows"]:
            if category and category not in show["categories"]: continue
            matching=[e for e in show["episodes"] if (not language or language in e["languages"]) and (not needle or needle in show["title"].lower() or needle in e["title"].lower() or any(needle in c for c in show["categories"]))]
            if matching: results.append({**show,"episodes":matching})
    return {"results":results}

@app.get("/admin/shows")
def list_shows(q: str="", section: str | None=None, status: str | None=None, language: str | None=None, page:int=1, page_size:int=10, _:str=Depends(role), session:Session=Depends(db)):
    data=[]
    for show in session.scalars(select(Show).order_by(Show.title)).all():
        eps=show.episodes
        if q and q.lower() not in show.title.lower(): continue
        if section and show.section != section: continue
        if status and show.status != status: continue
        if language and not any(e.language==language for e in eps): continue
        data.append({"id":show.id,"title":show.title,"section":show.section,"status":show.status,"episodes":len(eps),"artwork":art_map(show)})
    return {"items":data[(page-1)*page_size:page*page_size],"total":len(data),"page":page}

@app.put("/admin/shows/{show_id}")
def update_show(show_id:int, payload:dict, _:str=Depends(role), session:Session=Depends(db)):
    show=session.get(Show,show_id)
    if not show: raise HTTPException(404,"Show not found")
    for key in ("title","section","categories","synopsis","status"):
        if key in payload: setattr(show,key,payload[key])
    session.commit(); return {"id":show.id,"title":show.title}

@app.put("/admin/episodes/{episode_id}")
def update_episode(episode_id:int, payload:dict, _:str=Depends(role), session:Session=Depends(db)):
    ep=session.get(Episode,episode_id)
    if not ep: raise HTTPException(404,"Episode not found")
    for key in ("title","duration_seconds","language","content_group","status","season_number","episode_number"):
        if key in payload: setattr(ep,key,payload[key])
    try: session.commit()
    except Exception: session.rollback(); raise HTTPException(422,"That content group already has this language.")
    return {"id":ep.id,"title":ep.title}

@app.post("/admin/shows/{show_id}/artwork/{kind}")
async def upload_artwork(show_id:int, kind:Literal["poster","banner","thumbnail"], file:UploadFile=File(...), _:str=Depends(role), session:Session=Depends(db)):
    if not session.get(Show,show_id): raise HTTPException(404,"Show not found")
    data=await file.read()
    if len(data)>200*1024: raise HTTPException(422,"This file is over 200 KB. Please export a smaller image and try again.")
    try:
        import io
        image=Image.open(io.BytesIO(data)); width,height=image.size
    except Exception: raise HTTPException(422,"We couldn't read that image. Please upload a PNG, JPEG, or WebP file.")
    ratio,target=SPECS[kind]
    if abs(width/height-ratio)>0.015 or abs(width-target[0])>4 or abs(height-target[1])>4:
        raise HTTPException(422,f"{kind.title()} artwork must be {target[0]}×{target[1]} pixels ({'2:3' if kind=='poster' else '16:9'}). This image is {width}×{height}.")
    ext=(file.filename or "image.png").split(".")[-1].lower(); name=f"{show_id}/{kind}-{uuid.uuid4().hex}.{ext}"; out=STORAGE/name; out.parent.mkdir(parents=True,exist_ok=True); out.write_bytes(data)
    current=session.scalars(select(Artwork).where(Artwork.show_id==show_id,Artwork.kind==kind)).first()
    if current: current.path,current.width,current.height,current.size_bytes=name,width,height,len(data)
    else: session.add(Artwork(show_id=show_id,kind=kind,path=name,width=width,height=height,size_bytes=len(data)))
    session.commit(); return {"path":f"/storage/{name}"}

@app.get("/admin/validation-report")
def validation(_:str=Depends(role), session:Session=Depends(db)): return report(session)

@app.get("/admin/publish-runs")
def runs(_:str=Depends(role), session:Session=Depends(db)):
    return [{"id":r.id,"created_at":r.created_at,"actor":r.actor,"outcome":r.outcome,"show_count":r.show_count,"episode_count":r.episode_count,"details":r.details} for r in session.scalars(select(PublishRun).order_by(PublishRun.id.desc()).limit(20))]

@app.post("/admin/catalog/publish")
def publish(actor:str=Depends(require_admin), session:Session=Depends(db)):
    check=report(session)
    if check["blocked"]:
        session.add(PublishRun(actor=actor,outcome="blocked",details=check)); session.commit(); raise HTTPException(422,check)
    data=catalogue(session); total=sum(len(s["episodes"]) for r in data["sections"] for s in r["shows"])
    write_catalog(data); session.add(PublishRun(actor=actor,outcome="success",show_count=sum(len(r["shows"]) for r in data["sections"]),episode_count=total,details={"version":data["published_at"]})); session.commit()
    return {"outcome":"success","show_count":sum(len(r["shows"]) for r in data["sections"]),"episode_count":total}

@app.get("/storage/{path:path}")
def storage(path:str):
    # Placeholder metadata keeps seed catalogue useful without downloading external images.
    if path.startswith("placeholders/"): return {"detail":"Upload artwork to replace this seed placeholder."}
    target=(STORAGE/path).resolve()
    if not str(target).startswith(str(STORAGE.resolve())) or not target.exists(): raise HTTPException(404,"Artwork not found")
    from fastapi.responses import FileResponse
    return FileResponse(target)
