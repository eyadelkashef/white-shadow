#!/usr/bin/env python3
"""
White Shadow Launcher
Offline-account Minecraft launcher with profiles, Fabric support,
Modrinth + CurseForge mod browser, and a local skin override.

Run:  python WhiteShadowLauncher.py        (Python 3.8+, no extra packages)
"""
import hashlib, io, json, math, os, platform, queue, re, shutil, subprocess, sys, time
import tarfile, textwrap, threading, traceback, zipfile
import urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

APP, VER = "White Shadow", "1.0.0"
SYS = {"Windows": "windows", "Darwin": "osx", "Linux": "linux"}.get(platform.system(), "linux")
ARCH = platform.machine().lower()
CUR_ARCH = "arm64" if ARCH in ("arm64", "aarch64") else ("x86" if ARCH in ("x86", "i386", "i686") else "x86_64")
NOWIN = 0x08000000 if SYS == "windows" else 0
JEXE = "java.exe" if SYS == "windows" else "java"

if SYS == "windows":
    DEFAULT_BASE = Path(os.environ.get("APPDATA", str(Path.home()))) / ".whiteshadow"
elif SYS == "osx":
    DEFAULT_BASE = Path.home() / "Library/Application Support/whiteshadow"
else:
    DEFAULT_BASE = Path.home() / ".whiteshadow"
POINTER = Path.home() / ".whiteshadow_location"


def _pick_base():
    try:
        p = Path(POINTER.read_text().strip())
        if str(p) not in ("", "."):
            p.mkdir(parents=True, exist_ok=True)
            return p
    except Exception:
        pass
    return DEFAULT_BASE


BASE = _pick_base()
VERSIONS, LIBS, ASSETS = BASE / "versions", BASE / "libraries", BASE / "assets"
NATIVES, RUNTIME, INSTANCES = BASE / "natives", BASE / "runtime", BASE / "instances"
CFG_FILE = BASE / "config.json"
for d in (VERSIONS, LIBS, ASSETS, NATIVES, RUNTIME, INSTANCES):
    d.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------- theme
UIFONT = "Segoe UI"  # replaced at start-up by the best font installed (Bahnschrift on Windows 10+)
BG, PANEL, PANEL2, LINE = "#07050d", "#0e0a1a", "#171028", "#2a1d4d"   # purple-black
FG, DIM, ACCENT = "#f1ecff", "#9c8fc4", "#c4a8ff"
ACC_BG = "#8b5cf6"

# ----------------------------------------------------------------- http
UA = {"User-Agent": f"WhiteShadowLauncher/{VER} (personal launcher)"}


try:  # bundled CA certificates so HTTPS works inside the packaged app on every OS
    import certifi, ssl
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL = None


def http(url, headers=None, timeout=40):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read()


def jget(url, headers=None):
    return json.loads(http(url, headers))


DL = {"workers": 16, "retries": 3}
LOGF = BASE / "launcher.log"
if LOGF.exists() and LOGF.stat().st_size > 2_000_000:
    LOGF.replace(LOGF.with_suffix(".old"))
META_CACHE = {}


def read_mod_meta(jar):
    key = (str(jar), jar.stat().st_mtime, jar.stat().st_size)
    if key in META_CACHE:
        return dict(META_CACHE[key])
    meta = {"file": jar.name, "id": None, "name": jar.name.replace(".jar.disabled", "").replace(".jar", ""),
            "version": "?", "authors": "", "desc": "", "icon": None, "depends": [], "provides": []}
    try:
        with zipfile.ZipFile(jar) as z:
            names = z.namelist()
            if "fabric.mod.json" in names:
                m = json.loads(z.read("fabric.mod.json").decode("utf-8-sig"), strict=False)
                meta.update(id=m.get("id"), name=m.get("name") or meta["name"], version=str(m.get("version", "?")),
                            desc=m.get("description", ""), provides=list(m.get("provides") or []),
                            depends=[d for d in (m.get("depends") or {}) if d not in ("minecraft", "java", "fabricloader", "fabric-loader")])
                meta["authors"] = ", ".join(a if isinstance(a, str) else a.get("name", "") for a in m.get("authors", []))
                ic = m.get("icon")
                if isinstance(ic, dict) and ic:
                    ic = ic[min(ic, key=lambda k: int(k))]
                if isinstance(ic, str) and ic in names:
                    meta["icon"] = z.read(ic)
    except Exception:
        pass
    META_CACHE[key] = meta
    return dict(meta)


def analyze_mods(d):
    metas = []
    for f in sorted(Path(d).iterdir()):
        if f.name.endswith(".jar") or f.name.endswith(".jar.disabled"):
            m = read_mod_meta(f)
            m["enabled"] = f.name.endswith(".jar")
            metas.append(m)
    ids, warns = {}, []
    for m in metas:
        if m["enabled"] and m["id"]:
            for i in [m["id"]] + m["provides"]:
                ids.setdefault(i, []).append(m["name"])
    for i, names in ids.items():
        if len(names) > 1:
            warns.append(f"Duplicate mod id '{i}': {', '.join(names)} (remove one)")
    for m in metas:
        if m["enabled"]:
            for dep in m["depends"]:
                if dep not in ids:
                    warns.append(f"{m['name']} needs '{dep}', which is not installed")
    return metas, warns


def free_mb():
    try:
        return shutil.disk_usage(BASE).free // (1 << 20)
    except Exception:
        return 10 ** 9


def no_space_msg(need=None):
    return (f"Not enough disk space on {BASE.drive or BASE.anchor} ({free_mb()} MB free"
            + (f", ~{need} MB needed" if need else "") + "). Free up space, or move the launcher's "
            "data to another drive: Settings > Data folder > Change.")


def need_space(mb, what=""):
    if free_mb() < mb:
        raise RuntimeError(f"Cannot install {what}. " + no_space_msg(mb))


def dir_size(p):
    return sum(f.stat().st_size for f in Path(p).rglob("*") if f.is_file())


def clean_space(used_ids):
    freed = 0
    for p in BASE.rglob("*.part"):
        try:
            freed += p.stat().st_size
            p.unlink()
        except OSError:
            pass
    for root in (VERSIONS, NATIVES):
        for d in list(root.iterdir()):
            if d.is_dir() and d.name not in used_ids:
                freed += dir_size(d)
                shutil.rmtree(d, ignore_errors=True)
    return freed


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest, sha1=None):
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size > 0 and (not sha1 or sha1_of(dest) == sha1):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(DL["retries"]):
        try:
            tmp.write_bytes(http(url))
            if sha1 and sha1_of(tmp) != sha1:
                raise IOError("checksum mismatch for " + dest.name)
            tmp.replace(dest)
            return
        except OSError as e:
            tmp.unlink(missing_ok=True)
            if e.errno == 28:
                raise RuntimeError(no_space_msg())
            if attempt == DL["retries"] - 1:
                raise
        except Exception:
            tmp.unlink(missing_ok=True)
            if attempt == DL["retries"] - 1:
                raise


def run_jobs(jobs, prog, workers=None):
    total, done = len(jobs), 0
    if not total:
        return
    with ThreadPoolExecutor(workers or DL["workers"]) as ex:
        futs = [ex.submit(download, u, d, s) for u, d, s in jobs]
        for f in futs:
            f.result()
            done += 1
            prog(done, total)


# ----------------------------------------------------------------- config
DEFAULT_CFG = {
    "username": "Player", "java": "", "jvm_args": "", "cf_key": "",
    "selected": "Default", "skin": "", "width": 0, "height": 0, "fullscreen": False,
    "workers": 16, "retries": 3, "playtime": 0, "launches": 0, "play_days": {},
    "profiles": {"Default": {"version": "", "loader": "vanilla", "ram": 4096}},
}


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        cfg.update(json.loads(CFG_FILE.read_text()))
    except Exception:
        pass
    if not cfg["profiles"]:
        cfg["profiles"] = DEFAULT_CFG["profiles"]
    if cfg["selected"] not in cfg["profiles"]:
        cfg["selected"] = next(iter(cfg["profiles"]))
    return cfg


def save_cfg(cfg):
    CFG_FILE.write_text(json.dumps(cfg, indent=2))


def instance_dir(name):
    return INSTANCES / re.sub(r'[^\w\- .]', "_", name)


def offline_uuid(name):
    h = bytearray(hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest())
    h[6] = (h[6] & 0x0F) | 0x30
    h[8] = (h[8] & 0x3F) | 0x80
    return h.hex()[:8] + "-" + h.hex()[8:12] + "-" + h.hex()[12:16] + "-" + h.hex()[16:20] + "-" + h.hex()[20:]


# ----------------------------------------------------------------- minecraft install
MANIFEST = None


def get_manifest():
    global MANIFEST
    cache = BASE / "version_manifest.json"
    try:
        MANIFEST = jget("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        cache.write_text(json.dumps(MANIFEST))
    except Exception:
        if cache.exists():
            MANIFEST = json.loads(cache.read_text())
        else:
            raise
    return MANIFEST


def rule_ok(rules):
    if not rules:
        return True
    allowed = False
    for r in rules:
        match = True
        o = r.get("os")
        if o:
            if "name" in o and o["name"] != SYS:
                match = False
            if "arch" in o and o["arch"] != CUR_ARCH:
                match = False
            if "version" in o and not re.search(o["version"], platform.release()):
                match = False
        if "features" in r:
            match = False  # we enable no optional launcher features
        if match:
            allowed = r.get("action") == "allow"
    return allowed


def install_vanilla(mc):
    vdir = VERSIONS / mc
    jf = vdir / f"{mc}.json"
    if not jf.exists():
        man = MANIFEST or get_manifest()
        ent = next((v for v in man["versions"] if v["id"] == mc), None)
        if not ent:
            raise RuntimeError(f"Unknown Minecraft version: {mc}")
        vdir.mkdir(parents=True, exist_ok=True)
        jf.write_bytes(http(ent["url"]))
    data = json.loads(jf.read_text())
    c = data["downloads"]["client"]
    download(c["url"], vdir / f"{mc}.jar", c["sha1"])


def install_fabric(mc):
    loaders = jget(f"https://meta.fabricmc.net/v2/versions/loader/{urllib.parse.quote(mc)}")
    if not loaders:
        raise RuntimeError(f"Fabric does not support Minecraft {mc} (yet).")
    stable = [l for l in loaders if l["loader"].get("stable")] or loaders
    lv = stable[0]["loader"]["version"]
    vid = f"fabric-loader-{lv}-{mc}"
    jf = VERSIONS / vid / f"{vid}.json"
    if not jf.exists():
        jf.parent.mkdir(parents=True, exist_ok=True)
        jf.write_bytes(http(f"https://meta.fabricmc.net/v2/versions/loader/{mc}/{lv}/profile/json"))
    return vid


def load_version(vid):
    data = json.loads((VERSIONS / vid / f"{vid}.json").read_text())
    if "inheritsFrom" not in data:
        data["_jar"] = vid
        return data
    parent = load_version(data["inheritsFrom"])
    merged = dict(parent)
    for k, v in data.items():
        if k == "libraries":
            merged[k] = v + parent.get(k, [])
        elif k == "arguments":
            pa = parent.get("arguments", {})
            merged[k] = {kk: v.get(kk, []) + pa.get(kk, []) for kk in ("game", "jvm")}
        else:
            merged[k] = v
    merged["_jar"] = parent["_jar"]
    return merged


def collect_libs(data):
    jobs, cp, natives, seen = [], [], [], set()
    for lib in data["libraries"]:
        if not rule_ok(lib.get("rules")):
            continue
        parts = lib["name"].split(":")
        key = ":".join(parts[:2] + parts[3:4])
        dls = lib.get("downloads")
        arts = []
        if dls is None:  # Fabric-style maven entry
            g, a, v = parts[:3]
            rel = f"{g.replace('.', '/')}/{a}/{v}/{a}-{v}.jar"
            arts.append((rel, lib.get("url", "https://libraries.minecraft.net/") + rel, lib.get("sha1"), None))
        else:
            nat = lib.get("natives")
            if nat and SYS in nat:
                cls = nat[SYS].replace("${arch}", "64" if CUR_ARCH != "x86" else "32")
                a = dls.get("classifiers", {}).get(cls)
                if a:
                    arts.append((a["path"], a["url"], a["sha1"], lib.get("extract", {}).get("exclude", ["META-INF/"])))
            art = dls.get("artifact")
            if art and art.get("url"):
                if key in seen:
                    continue
                seen.add(key)
                arts.append((art["path"], art["url"], art["sha1"], None))
        if dls is None:
            if key in seen:
                continue
            seen.add(key)
        for rel, url, sha, excl in arts:
            p = LIBS / rel
            jobs.append((url, p, sha))
            if excl is None:
                cp.append(str(p))
            else:
                natives.append((p, excl))
    jar = VERSIONS / data["_jar"] / f"{data['_jar']}.jar"
    cp.append(str(jar))
    return cp, jobs, natives


def extract_natives(natives, ndir):
    ndir.mkdir(parents=True, exist_ok=True)
    for jar, excl in natives:
        with zipfile.ZipFile(jar) as z:
            for n in z.namelist():
                if n.endswith("/") or any(n.startswith(e) for e in excl):
                    continue
                (ndir / Path(n).name).write_bytes(z.read(n))


def ensure_assets(data, prog):
    ai = data["assetIndex"]
    idx = ASSETS / "indexes" / f"{ai['id']}.json"
    download(ai["url"], idx, ai.get("sha1"))
    objs = json.loads(idx.read_text())["objects"]
    hashes = {o["hash"] for o in objs.values()}
    jobs = [(f"https://resources.download.minecraft.net/{h[:2]}/{h}", ASSETS / "objects" / h[:2] / h, None)
            for h in hashes]
    jobs = [j for j in jobs if not j[1].exists()]
    run_jobs(jobs, prog)


# ----------------------------------------------------------------- java
def java_major(path):
    try:
        out = subprocess.run([str(path), "-version"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                             timeout=20, creationflags=NOWIN).stderr
        m = re.search(r'version "(\d+)(?:\.(\d+))?', out)
        if not m:
            return None
        a = int(m.group(1))
        return int(m.group(2) or 0) if a == 1 else a
    except Exception:
        return None


def java_candidates():
    c = []
    c += list(RUNTIME.rglob("bin/" + JEXE))
    if os.environ.get("JAVA_HOME"):
        c.append(Path(os.environ["JAVA_HOME"]) / "bin" / JEXE)
    w = shutil.which("java")
    if w:
        c.append(Path(w))
    roots = []
    if SYS == "windows":
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            pf = os.environ.get(env)
            if pf:
                roots += [Path(pf) / x for x in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "BellSoft")]
        roots.append(Path(os.environ.get("APPDATA", "")) / ".minecraft/runtime")
    elif SYS == "osx":
        roots += [Path("/Library/Java/JavaVirtualMachines"), Path.home() / "Library/Application Support/minecraft/runtime"]
    else:
        roots += [Path("/usr/lib/jvm"), Path.home() / ".minecraft/runtime"]
    for r in roots:
        if r.exists():
            c += list(r.glob("*/bin/" + JEXE)) + list(r.glob("*/*/bin/" + JEXE)) + \
                 list(r.glob("*/Contents/Home/bin/java")) + list(r.glob("*/*/*/bin/" + JEXE))
    return c


def find_java(major, cfg):
    if cfg.get("java"):
        return cfg["java"]
    cf = BASE / "java_cache.json"
    try:
        cache = json.loads(cf.read_text())
    except Exception:
        cache = {}
    p = cache.get(str(major))
    if p and Path(p).exists():
        return p
    p = _scan_java(major)
    if p:
        cache[str(major)] = str(p)
        cf.write_text(json.dumps(cache))
    return p


def _scan_java(major):
    found = {}
    for p in java_candidates():
        if str(p) not in found and p.exists():
            v = java_major(p)
            if v:
                found[str(p)] = v
    exact = [p for p, v in found.items() if v == major]
    if exact:
        return exact[0]
    higher = sorted([(v, p) for p, v in found.items() if v > major])
    return higher[0][1] if higher else None


def download_java(major, log):
    osn = {"windows": "windows", "osx": "mac", "linux": "linux"}[SYS]
    arch = "aarch64" if CUR_ARCH == "arm64" else "x64"
    log(f"Downloading Java {major} (Eclipse Temurin) - one-time, ~50 MB...")
    url = f"https://api.adoptium.net/v3/binary/latest/{major}/ga/{osn}/{arch}/jre/hotspot/normal/eclipse"
    data = http(url, timeout=120)
    dest = RUNTIME / str(major)
    dest.mkdir(parents=True, exist_ok=True)
    if SYS == "windows":
        zipfile.ZipFile(io.BytesIO(data)).extractall(dest)
    else:
        tarfile.open(fileobj=io.BytesIO(data), mode="r:gz").extractall(dest)
    for p in dest.rglob("bin/" + JEXE):
        if SYS != "windows":
            p.chmod(0o755)
        return str(p)
    raise RuntimeError("Java download succeeded but java binary was not found.")


# ----------------------------------------------------------------- launch command
def expand_args(items, vars_):
    out = []
    for a in items:
        vals = []
        if isinstance(a, str):
            vals = [a]
        elif rule_ok(a.get("rules")):
            v = a["value"]
            vals = v if isinstance(v, list) else [v]
        for s in vals:
            for k, v in vars_.items():
                s = s.replace("${" + k + "}", str(v))
            out.append(s)
    return out


def build_cmd(java, data, vid, cp, gamedir, ndir, username, ram, extra_jvm):
    vars_ = {
        "auth_player_name": username, "auth_uuid": offline_uuid(username),
        "auth_access_token": "0", "auth_session": "0", "auth_xuid": "0", "clientid": "0",
        "user_type": "legacy", "user_properties": "{}", "version_name": vid,
        "version_type": data.get("type", "release"), "game_directory": gamedir,
        "assets_root": ASSETS, "game_assets": ASSETS, "assets_index_name": data["assetIndex"]["id"],
        "natives_directory": ndir, "launcher_name": "WhiteShadow", "launcher_version": VER,
        "classpath": os.pathsep.join(cp), "classpath_separator": os.pathsep, "library_directory": LIBS,
        "resolution_width": 854, "resolution_height": 480,
    }
    if "arguments" in data:
        jvm = expand_args(data["arguments"].get("jvm", []), vars_)
        game = expand_args(data["arguments"].get("game", []), vars_)
    else:
        jvm = expand_args(["-Djava.library.path=${natives_directory}", "-cp", "${classpath}"], vars_)
        game = expand_args(data["minecraftArguments"].split(), vars_)
    if "-cp" not in jvm and "-classpath" not in jvm:
        jvm += ["-cp", os.pathsep.join(cp)]
    mem = [f"-Xms{min(ram, 1024)}M", f"-Xmx{ram}M"]
    return [str(java)] + mem + extra_jvm.split() + jvm + [data["mainClass"]] + game


# ----------------------------------------------------------------- skin (local resource pack)
def png_ok(path):
    try:
        b = Path(path).read_bytes()[:24]
        if b[:8] != b"\x89PNG\r\n\x1a\n":
            return False
        w, h = int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
        return w == 64 and h in (32, 64)
    except Exception:
        return False


def pack_format_for(jar_id):
    try:
        with zipfile.ZipFile(VERSIONS / jar_id / f"{jar_id}.jar") as z:
            pv = json.loads(z.read("version.json")).get("pack_version", {})
        if isinstance(pv, int):
            return pv
        r = pv.get("resource", pv.get("resource_major"))
        if r:
            return int(r)
    except Exception:
        pass
    try:
        minor = int(jar_id.split(".")[1])
        return {8: 1, 9: 2, 10: 2, 11: 3, 12: 3, 13: 4, 14: 4, 15: 5, 16: 6}.get(minor, 15)
    except Exception:
        return 15


PACK_NAME = "WhiteShadowSkin.zip"


def edit_options(gamedir, add):
    opt = gamedir / "options.txt"
    lines = opt.read_text().splitlines() if opt.exists() else []
    entry, out, found = "file/" + PACK_NAME, [], False
    for l in lines:
        if l.startswith("resourcePacks:"):
            found = True
            try:
                arr = json.loads(l.split(":", 1)[1])
            except Exception:
                arr = ["vanilla"]
            arr = [a for a in arr if a != entry]
            if add:
                arr.append(entry)
            l = "resourcePacks:" + json.dumps(arr, separators=(",", ":"))
        out.append(l)
    if not found and add:
        out.append('resourcePacks:["vanilla","' + entry + '"]')
    opt.write_text("\n".join(out) + "\n")


def sync_skin(gamedir, skin, jar_id, hide=False):
    packs = gamedir / "resourcepacks"
    pack = packs / PACK_NAME
    if not skin or not Path(skin).exists():
        if pack.exists():
            pack.unlink()
            edit_options(gamedir, False)
        return
    packs.mkdir(parents=True, exist_ok=True)
    png = Path(skin).read_bytes()
    if hide:
        png = strip_overlay(png)
    base = "assets/minecraft/textures/entity/"
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": pack_format_for(jar_id),
                                                          "description": "White Shadow skin"}}))
        for n in ("steve", "alex"):
            z.writestr(f"{base}{n}.png", png)  # < 1.19.3
        for kind in ("wide", "slim"):
            for n in ("steve", "alex", "ari", "efe", "kai", "makena", "noor", "sunny", "zuri"):
                z.writestr(f"{base}player/{kind}/{n}.png", png)  # >= 1.19.3
    edit_options(gamedir, True)


# ----------------------------------------------------------------- mod sources
MR = "https://api.modrinth.com/v2"
CF = "https://api.curseforge.com/v1"
CF_LOADERS = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}


def mr_search(q, mc, loader, sort="relevance", cat="", ptype="mod"):
    facets = [[f"project_type:{ptype}"], [f"versions:{mc}"]]
    if loader != "vanilla" and ptype == "mod":
        facets.append([f"categories:{loader}"])
    if cat:
        facets.append([f"categories:{cat}"])
    url = MR + "/search?" + urllib.parse.urlencode({"query": q, "limit": 50, "index": sort,
                                                      "facets": json.dumps(facets)})
    return [{"id": h["project_id"], "name": h["title"], "downloads": h["downloads"],
             "desc": h["description"], "icon": h.get("icon_url"), "author": h.get("author", "")} for h in jget(url)["hits"]]


def mr_install(pid, mc, loader, dest, log, done=None, ptype="mod"):
    done = done if done is not None else set()
    if pid in done:
        return
    done.add(pid)
    params = {"game_versions": json.dumps([mc])}
    if loader != "vanilla" and ptype == "mod":
        params["loaders"] = json.dumps([loader])
    vers = jget(f"{MR}/project/{pid}/version?" + urllib.parse.urlencode(params))
    if not vers:
        raise RuntimeError("No compatible version for this Minecraft version/loader.")
    v = vers[0]
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    log(f"  + {f['filename']}")
    download(f["url"], dest / f["filename"], f["hashes"].get("sha1"))
    for d in v.get("dependencies", []):
        if d.get("dependency_type") != "required":
            continue
        dep = d.get("project_id")
        if not dep and d.get("version_id"):
            try:
                dep = jget(f"{MR}/version/{d['version_id']}")["project_id"]
            except Exception:
                dep = None
        if dep:
            log(f"    needs a required dependency -> installing")
            mr_install(dep, mc, loader, dest, log, done, ptype)


def cf_get(path, key):
    return jget(CF + path, {"x-api-key": key, "Accept": "application/json"})


def cf_search(q, mc, loader, key, class_id=6):
    p = {"gameId": 432, "classId": class_id, "searchFilter": q, "gameVersion": mc, "pageSize": 40,
         "sortField": 2, "sortOrder": "desc"}
    if loader in CF_LOADERS and class_id == 6:
        p["modLoaderType"] = CF_LOADERS[loader]
    r = cf_get("/mods/search?" + urllib.parse.urlencode(p), key)["data"]
    return [{"id": m["id"], "name": m["name"], "downloads": int(m["downloadCount"]),
             "desc": m["summary"], "icon": (m.get("logo") or {}).get("thumbnailUrl"),
             "author": ", ".join(a["name"] for a in m.get("authors", []))} for m in r]


def cf_install(mid, mc, loader, dest, key, log, done=None, class_id=6):
    done = done if done is not None else set()
    if mid in done:
        return
    done.add(mid)
    p = {"gameVersion": mc, "pageSize": 1}
    if loader in CF_LOADERS and class_id == 6:
        p["modLoaderType"] = CF_LOADERS[loader]
    files = cf_get(f"/mods/{mid}/files?" + urllib.parse.urlencode(p), key)["data"]
    if not files:
        raise RuntimeError("No compatible file for this Minecraft version/loader.")
    f = files[0]
    fid = f["id"]
    url = f.get("downloadUrl") or f"https://edge.forgecdn.net/files/{fid // 1000}/{fid % 1000}/{f['fileName']}"
    log(f"  + {f['fileName']}")
    try:
        download(url, dest / f["fileName"])
    except Exception:
        raise RuntimeError(f"{f['fileName']}: the author disabled third-party downloads. "
                           f"Download it manually from curseforge.com and use 'Add .jar'.")
    for d in f.get("dependencies", []):
        if d.get("relationType") == 3:
            cf_install(d["modId"], mc, loader, dest, key, log, done)


def jpost(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(), headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40, context=_SSL) as r:
        return json.loads(r.read())


def export_profile(name, prof, path):
    """Plain zip (.wsprof): profile settings, options/keybinds, packs, configs; mods by Modrinth version id."""
    gd = instance_dir(name)
    jars = sorted((gd / "mods").glob("*.jar")) if (gd / "mods").exists() else []
    hashes = {sha1_of(j): j for j in jars}
    try:
        found = jpost(MR + "/version_files", {"hashes": list(hashes), "algorithm": "sha1"}) if hashes else {}
    except Exception:
        found = {}
    man = {"format": 1, "name": name, "profile": {k: prof.get(k) for k in ("version", "loader", "ram", "icon")}, "mods": []}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for h, j in hashes.items():
            v = found.get(h)
            if v:
                man["mods"].append({"file": j.name, "version_id": v["id"], "project_id": v["project_id"]})
            else:
                z.write(j, "local_mods/" + j.name)
        if (gd / "options.txt").exists():
            z.write(gd / "options.txt", "overrides/options.txt")
        for sub in ("resourcepacks", "shaderpacks", "config"):
            for f in ((gd / sub).rglob("*") if (gd / sub).exists() else []):
                if f.is_file() and f.name != PACK_NAME and f.stat().st_size < 50 * 2 ** 20:
                    z.write(f, "overrides/" + f.relative_to(gd).as_posix())
        z.writestr("wsprofile.json", json.dumps(man, indent=2))
    return len(man["mods"]), len(jars) - len(man["mods"])


def import_profile(path, dest, log):
    dest = Path(dest).resolve()
    with zipfile.ZipFile(path) as z:
        man = json.loads(z.read("wsprofile.json"))
        for n in z.namelist():
            if n.endswith("/") or not (n.startswith("overrides/") or n.startswith("local_mods/")):
                continue
            rel = n.split("/", 1)[1] if n.startswith("overrides/") else "mods/" + n.split("/", 1)[1]
            target = (dest / rel).resolve()
            if dest not in target.parents:  # zip-slip guard
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(n))
    for m in man.get("mods", []):
        v = jget(f"{MR}/version/{m['version_id']}")
        f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
        log(f"  + {f['filename']}")
        download(f["url"], dest / "mods" / f["filename"], f["hashes"].get("sha1"))
    return man


def analyze_crash(lines):
    t = "\n".join(lines[-700:])
    out = []
    m = re.search(r"class file version (\d+)", t)
    if "UnsupportedClassVersionError" in t or m:
        need = int(m.group(1)) - 44 if m else "a newer"
        out.append(dict(title="Wrong Java version", text=f"Something needs Java {need}, but an older Java was used.",
                        fix=("java", None), fixlabel="Use automatic Java"))
    if re.search(r"OutOfMemoryError|Java heap space", t):
        out.append(dict(title="Out of memory", text="The game ran out of RAM. Give this profile more memory.",
                        fix=("ram", 1024), fixlabel="Add 1 GB RAM"))
    if re.search(r"Could not reserve enough space|Invalid maximum heap size", t):
        out.append(dict(title="Too much RAM requested", text="Java could not reserve that much memory on this PC.",
                        fix=("ram", -1024), fixlabel="Remove 1 GB RAM"))
    dm = re.search(r".*(requires|depends on).*(missing|not installed|any version of).*", t) or \
        re.search(r".*(Incompatible mod set|ModResolutionException).*", t) or re.search(r"NoClassDefFoundError: [\w/.$]+", t)
    if dm:
        out.append(dict(title="A mod is missing something it needs", text=dm.group(0).strip()[:220],
                        fix=("deps", None), fixlabel="Install missing dependencies"))
    if re.search(r"Mixin apply failed|MixinApplyError|InvalidInjectionException|MixinTransformerError", t):
        mm = re.search(r"([A-Za-z0-9_\-]+)\.mixins?\.json", t)
        out.append(dict(title="Two mods conflict", text="A mod could not apply its patches" + (f" ('{mm.group(1)}')." if mm else "."),
                        fix=("disable", mm.group(1)) if mm else None, fixlabel="Disable that mod"))
    if re.search(r"[Dd]uplicate mods?", t):
        out.append(dict(title="Duplicate mods", text="The same mod is installed twice. Check Mods -> Installed.", fix=None, fixlabel=""))
    if re.search(r"GLFW error|WGL:|Pixel format not accepted|OpenGL", t) and "Exception" in t:
        out.append(dict(title="Graphics problem", text="The graphics driver rejected the game. Update your GPU driver.", fix=None, fixlabel=""))
    return out or [dict(title="Unknown crash", text="No known cause found. Open the log for details.", fix=None, fixlabel="")]


LEGACY_COPY = [(4, 16, 16, 32, 4, 4), (8, 16, 16, 32, 4, 4), (0, 20, 24, 32, 4, 12), (4, 20, 16, 32, 4, 12),
               (8, 20, 8, 32, 4, 12), (12, 20, 16, 32, 4, 12), (44, 16, -8, 32, 4, 4), (48, 16, -8, 32, 4, 4),
               (40, 20, 0, 32, 4, 12), (44, 20, -8, 32, 4, 12), (48, 20, -16, 32, 4, 12), (52, 20, -8, 32, 4, 12)]


def normalize_skin(src, dest):
    b = Path(src).read_bytes()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Please choose a PNG file.")
    w, h = int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
    if (w, h) == (64, 64):
        shutil.copy(src, dest)
        return ""
    try:
        from PIL import Image
    except ImportError:
        raise ValueError(f"This skin is {w}x{h}. Without Pillow only 64x64 works. Run: pip install pillow "
                         "to auto-convert legacy 64x32 skins.")
    im = Image.open(src).convert("RGBA")
    if (w, h) == (64, 32):
        new = Image.new("RGBA", (64, 64))
        new.paste(im, (0, 0))
        for x, y, ox, oy, rw, rh in LEGACY_COPY:
            new.paste(im.crop((x, y, x + rw, y + rh)).transpose(Image.FLIP_LEFT_RIGHT), (x + ox, y + oy))
        new.save(dest)
        return "Converted the legacy 64x32 skin to the modern 64x64 layout."
    if w == h and w % 64 == 0:
        im.resize((64, 64), Image.NEAREST).save(dest)
        return f"Resized the {w}x{w} skin to 64x64."
    raise ValueError(f"Unsupported skin size {w}x{h}. Use 64x64 (or legacy 64x32).")


def strip_overlay(png):
    try:
        from PIL import Image
    except ImportError:
        return png
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    for box in ((32, 0, 64, 16), (16, 32, 40, 48), (40, 32, 56, 48), (0, 32, 16, 48), (48, 48, 64, 64), (0, 48, 16, 64)):
        im.paste((0, 0, 0, 0), box)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def fetch_icon(url):
    data = http(url, timeout=15)
    try:
        from PIL import Image  # optional: lets every icon format (webp/jpg/...) show
        im = Image.open(io.BytesIO(data)).convert("RGBA").resize((40, 40))
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except ImportError:
        return data if data[:4] == b"\x89PNG" or data[:3] == b"GIF" else None
    except Exception:
        return None


def fix_deps(dest, mc, loader, log):
    """Install every dependency the installed mods declare but are missing. Returns unresolved messages."""
    left = {}
    for _ in range(3):
        metas, _w = analyze_mods(dest)
        have = {i for m in metas if m["enabled"] for i in [m["id"]] + m["provides"] if i}
        missing = {d for m in metas if m["enabled"] for d in m["depends"] if d not in have} - set(left)
        if not missing:
            break
        for d in sorted(missing):
            try:
                log(f"  + dependency '{d}'")
                mr_install({"fabric": "fabric-api"}.get(d, d), mc, loader, dest, log)
            except Exception as e:
                left[d] = f"{d}: {e}"
    return list(left.values())



# ----------------------------------------------------------------- TEXT / DATA
CUR_LANG = "en"
LANG_CODES = ["en", "ar", "es", "fr", "de", "pt", "ru", "tr", "zh", "ja", "ko", "hi"]
LANG_NAMES = {"en": "English", "ar": "العربية", "es": "Español", "fr": "Français", "de": "Deutsch",
              "pt": "Português", "ru": "Русский", "tr": "Türkçe", "zh": "中文", "ja": "日本語",
              "ko": "한국어", "hi": "हिन्दी"}
RAW = """
home|Home|الرئيسية|Inicio|Accueil|Start|Início|Главная|Ana Sayfa|主页|ホーム|홈|होम
mods|Mods|المودات|Mods|Mods|Mods|Mods|Моды|Modlar|模组|Mod|모드|मॉड
suggested|Suggested|مقترحة|Sugeridos|Suggestions|Empfohlen|Sugeridos|Рекомендуемые|Önerilen|推荐|おすすめ|추천|सुझाव
profiles|Profiles|الملفات الشخصية|Perfiles|Profils|Profile|Perfis|Профили|Profiller|配置|プロファイル|프로필|प्रोफ़ाइल
skin|Skin|السكن|Skin|Skin|Skin|Skin|Скин|Skin|皮肤|スキン|스킨|स्किन
settings|Settings|الإعدادات|Ajustes|Paramètres|Einstellungen|Configurações|Настройки|Ayarlar|设置|設定|설정|सेटिंग्स
play|PLAY|لعب|JUGAR|JOUER|SPIELEN|JOGAR|ИГРАТЬ|OYNA|开始游戏|プレイ|플레이|खेलें
running|RUNNING...|قيد التشغيل...|EJECUTANDO...|EN COURS...|LÄUFT...|EXECUTANDO...|ЗАПУЩЕНО...|ÇALIŞIYOR...|运行中...|実行中...|실행 중...|चल रहा है...
username|Username|اسم المستخدم|Usuario|Pseudo|Benutzername|Usuário|Ник|Kullanıcı adı|用户名|ユーザー名|사용자 이름|उपयोगकर्ता नाम
profile|Profile|الملف|Perfil|Profil|Profil|Perfil|Профиль|Profil|配置|プロファイル|프로필|प्रोफ़ाइल
version|Version|الإصدار|Versión|Version|Version|Versão|Версия|Sürüm|版本|バージョン|버전|संस्करण
loader|Loader|المحمّل|Cargador|Chargeur|Loader|Loader|Загрузчик|Yükleyici|加载器|ローダー|로더|लोडर
search|Search|بحث|Buscar|Rechercher|Suchen|Buscar|Поиск|Ara|搜索|検索|검색|खोजें
install|Install selected|تثبيت المحدد|Instalar selección|Installer la sélection|Auswahl installieren|Instalar selecionados|Установить выбранное|Seçileni yükle|安装所选|選択をインストール|선택 설치|चयनित इंस्टॉल करें
installed|Installed mods|المودات المثبتة|Mods instalados|Mods installés|Installierte Mods|Mods instalados|Установленные моды|Yüklü modlar|已安装模组|インストール済みMod|설치된 모드|इंस्टॉल किए गए मॉड
browse|Browse|تصفح|Explorar|Parcourir|Durchsuchen|Explorar|Обзор|Gözat|浏览|閲覧|둘러보기|ब्राउज़
add_jar|Add .jar|إضافة .jar|Añadir .jar|Ajouter .jar|.jar hinzufügen|Adicionar .jar|Добавить .jar|.jar ekle|添加 .jar|.jar を追加|.jar 추가|.jar जोड़ें
toggle|Enable / Disable|تفعيل / تعطيل|Activar / Desactivar|Activer / Désactiver|Ein / Aus|Ativar / Desativar|Вкл / Выкл|Aç / Kapat|启用 / 禁用|有効 / 無効|활성 / 비활성|चालू / बंद
remove|Remove|حذف|Eliminar|Supprimer|Entfernen|Remover|Удалить|Kaldır|移除|削除|제거|हटाएं
open_folder|Open folder|فتح المجلد|Abrir carpeta|Ouvrir le dossier|Ordner öffnen|Abrir pasta|Открыть папку|Klasörü aç|打开文件夹|フォルダを開く|폴더 열기|फ़ोल्डर खोलें
source|Source|المصدر|Fuente|Source|Quelle|Fonte|Источник|Kaynak|来源|ソース|출처|स्रोत
sort|Sort|ترتيب|Orden|Tri|Sortierung|Ordem|Сортировка|Sırala|排序|並べ替え|정렬|क्रमबद्ध
category|Category|الفئة|Categoría|Catégorie|Kategorie|Categoria|Категория|Kategori|分类|カテゴリ|카테고리|श्रेणी
save|Save|حفظ|Guardar|Enregistrer|Speichern|Salvar|Сохранить|Kaydet|保存|保存|저장|सहेजें
language|Language|اللغة|Idioma|Langue|Sprache|Idioma|Язык|Dil|语言|言語|언어|भाषा
java|Java path (blank = auto)|مسار Java (فارغ = تلقائي)|Ruta de Java (vacío = auto)|Chemin Java (vide = auto)|Java-Pfad (leer = auto)|Caminho do Java (vazio = auto)|Путь к Java (пусто = авто)|Java yolu (boş = otomatik)|Java 路径(留空=自动)|Javaパス(空=自動)|Java 경로(비우면 자동)|Java पथ (खाली = ऑटो)
jvm|Extra JVM arguments|وسائط JVM إضافية|Argumentos JVM extra|Arguments JVM supplémentaires|Zusätzliche JVM-Argumente|Argumentos JVM extras|Доп. аргументы JVM|Ek JVM argümanları|额外 JVM 参数|追加JVM引数|추가 JVM 인수|अतिरिक्त JVM तर्क
cf_key|CurseForge API key|مفتاح CurseForge API|Clave API de CurseForge|Clé API CurseForge|CurseForge-API-Schlüssel|Chave de API do CurseForge|API-ключ CurseForge|CurseForge API anahtarı|CurseForge API 密钥|CurseForge APIキー|CurseForge API 키|CurseForge API कुंजी
data_folder|Data folder|مجلد البيانات|Carpeta de datos|Dossier de données|Datenordner|Pasta de dados|Папка данных|Veri klasörü|数据文件夹|データフォルダ|데이터 폴더|डेटा फ़ोल्डर
change|Change...|تغيير...|Cambiar...|Changer...|Ändern...|Alterar...|Изменить...|Değiştir...|更改...|変更...|변경...|बदलें...
free_space|Free space|المساحة الفارغة|Espacio libre|Espace libre|Freier Speicher|Espaço livre|Свободно|Boş alan|可用空间|空き容量|여유 공간|खाली जगह
clean|Free up space|تفريغ مساحة|Liberar espacio|Libérer de l'espace|Speicher freigeben|Liberar espaço|Освободить место|Yer aç|释放空间|容量を空ける|공간 확보|जगह खाली करें
ready|Ready|جاهز|Listo|Prêt|Bereit|Pronto|Готово|Hazır|就绪|準備完了|준비됨|तैयार
new|New|جديد|Nuevo|Nouveau|Neu|Novo|Создать|Yeni|新建|新規|새로 만들기|नया
edit|Edit|تعديل|Editar|Modifier|Bearbeiten|Editar|Изменить|Düzenle|编辑|編集|편집|संपादित करें
duplicate|Duplicate|نسخ|Duplicar|Dupliquer|Duplizieren|Duplicar|Копировать|Çoğalt|复制|複製|복제|डुप्लिकेट
delete|Delete|حذف|Eliminar|Supprimer|Löschen|Excluir|Удалить|Sil|删除|削除|삭제|हटाएं
snapshots|Show snapshots|عرض اللقطات|Mostrar snapshots|Afficher les snapshots|Snapshots anzeigen|Mostrar snapshots|Показать снапшоты|Snapshot'ları göster|显示快照|スナップショットを表示|스냅샷 표시|स्नैपशॉट दिखाएं
choose_skin|Choose skin...|اختر سكن...|Elegir skin...|Choisir un skin...|Skin wählen...|Escolher skin...|Выбрать скин...|Skin seç...|选择皮肤...|スキンを選択...|스킨 선택...|स्किन चुनें...
clear_skin|Remove skin|إزالة السكن|Quitar skin|Retirer le skin|Skin entfernen|Remover skin|Убрать скин|Skin'i kaldır|移除皮肤|スキンを削除|스킨 제거|स्किन हटाएं
skin_note|Offline accounts can't use Mojang skin servers, so your skin is applied as a local resource pack. Only you can see it.|لا تدعم الحسابات غير المتصلة خوادم سكنات موجانج، لذلك يُطبَّق السكن كحزمة موارد محلية. أنت فقط من يراه.|Las cuentas offline no pueden usar los servidores de skins de Mojang, así que tu skin se aplica como paquete de recursos local. Solo tú lo ves.|Les comptes hors ligne n'ont pas accès aux serveurs de skins de Mojang : votre skin est appliqué comme pack de ressources local. Vous seul le voyez.|Offline-Konten können Mojangs Skin-Server nicht nutzen, daher wird dein Skin als lokales Ressourcenpaket angewendet. Nur du siehst ihn.|Contas offline não usam os servidores de skins da Mojang, então sua skin é aplicada como pacote de recursos local. Só você a vê.|Офлайн-аккаунты не используют серверы скинов Mojang, поэтому скин применяется как локальный набор ресурсов. Видите его только вы.|Çevrimdışı hesaplar Mojang skin sunucularını kullanamaz; skin'iniz yerel kaynak paketi olarak uygulanır. Sadece siz görürsünüz.|离线账号无法使用 Mojang 皮肤服务器,因此皮肤以本地资源包形式应用,仅你自己可见。|オフラインアカウントはMojangのスキンサーバーを使えないため、スキンはローカルのリソースパックとして適用されます。自分にだけ表示されます。|오프라인 계정은 Mojang 스킨 서버를 사용할 수 없어 스킨이 로컬 리소스 팩으로 적용되며 본인에게만 보입니다.|ऑफ़लाइन खाते Mojang स्किन सर्वर का उपयोग नहीं कर सकते, इसलिए स्किन लोकल रिसोर्स पैक के रूप में लगती है। इसे केवल आप देख सकते हैं।
perf_pack|Install performance pack|تثبيت حزمة الأداء|Instalar pack de rendimiento|Installer le pack performance|Performance-Paket installieren|Instalar pacote de desempenho|Установить пакет оптимизации|Performans paketini yükle|安装性能优化包|軽量化パックをインストール|성능 팩 설치|परफ़ॉर्मेंस पैक इंस्टॉल करें
suggest_hint|Pick mods, then install. Only versions compatible with your profile are used.|اختر المودات ثم ثبّتها. تُستخدم فقط الإصدارات المتوافقة مع ملفك.|Elige mods e instala. Solo se usan versiones compatibles con tu perfil.|Choisissez des mods puis installez. Seules les versions compatibles avec votre profil sont utilisées.|Mods auswählen und installieren. Es werden nur zum Profil passende Versionen genutzt.|Escolha mods e instale. Só versões compatíveis com seu perfil são usadas.|Выберите моды и установите. Используются только версии, совместимые с профилем.|Modları seçip yükleyin. Yalnızca profilinizle uyumlu sürümler kullanılır.|选择模组后安装,仅使用与当前配置兼容的版本。|Modを選んでインストール。プロファイルに対応するバージョンのみ使われます。|모드를 선택해 설치하세요. 프로필과 호환되는 버전만 사용됩니다.|मॉड चुनें और इंस्टॉल करें। केवल आपकी प्रोफ़ाइल के अनुकूल संस्करण उपयोग होते हैं।
name|Name|الاسم|Nombre|Nom|Name|Nome|Название|Ad|名称|名前|이름|नाम
state|State|الحالة|Estado|État|Status|Estado|Состояние|Durum|状态|状態|상태|स्थिति
"""
TR = {}
for _line in RAW.strip().splitlines():
    _p = _line.split("|")
    for _l, _v in zip(LANG_CODES, _p[1:]):
        TR.setdefault(_l, {})[_p[0]] = _v


def T(key):
    return TR.get(CUR_LANG, {}).get(key) or TR["en"].get(key, key)


# (slug, name, category, why) - curated, legit performance / quality-of-life mods
SUGGESTED = [
    ("sodium", "Sodium", "Performance", "Modern rendering engine - the biggest FPS boost"),
    ("lithium", "Lithium", "Performance", "Optimizes game logic, physics and AI"),
    ("ferrite-core", "FerriteCore", "Performance", "Cuts RAM usage"),
    ("entityculling", "Entity Culling", "Performance", "Skips rendering things you can't see"),
    ("immediatelyfast", "ImmediatelyFast", "Performance", "Faster immediate-mode rendering and HUD"),
    ("modernfix", "ModernFix", "Performance", "Faster startup, less memory, bug fixes"),
    ("dynamic-fps", "Dynamic FPS", "Performance", "Lowers FPS when the window is unfocused"),
    ("iris", "Iris Shaders", "Visuals", "Shader support (needs Sodium - auto-installed)"),
    ("sodium-extra", "Sodium Extra", "Visuals", "More video options for Sodium"),
    ("continuity", "Continuity", "Visuals", "Connected textures for resource packs"),
    ("modmenu", "Mod Menu", "Quality of life", "In-game list and settings for your mods"),
    ("appleskin", "AppleSkin", "Quality of life", "Shows food and saturation values"),
    ("jade", "Jade", "Quality of life", "Tooltip showing the block/entity you look at"),
    ("xaeros-minimap", "Xaero's Minimap", "Quality of life", "Minimap with waypoints"),
    ("mouse-tweaks", "Mouse Tweaks", "Quality of life", "Faster inventory management with the mouse"),
    ("inventory-profiles-next", "Inventory Profiles Next", "Quality of life", "Inventory sorting and auto-refill"),
    ("zoomify", "Zoomify", "Quality of life", "Smooth, configurable zoom key"),
    ("no-chat-reports", "No Chat Reports", "Quality of life", "Disables chat signing/reporting"),
    ("fabric-api", "Fabric API", "Library", "Required by most Fabric mods"),
]
MR_CATS = ["all", "optimization", "utility", "adventure", "decoration", "equipment", "food", "library", "magic",
           "mobs", "storage", "technology", "transportation", "worldgen", "social", "game-mechanics", "management"]
CF_CLASS = {"Mods": 6, "Resource Packs": 12, "Shaders": 6552}
PACK_CATS = {"Mods": MR_CATS,
             "Resource Packs": ["all", "combat", "decoration", "realistic", "simplistic", "themed", "tweaks", "utility",
                                "vanilla-like", "16x", "32x", "64x", "128x"],
             "Shaders": ["all", "cartoon", "fantasy", "realistic", "semi-realistic", "vanilla-like", "atmosphere",
                         "bloom", "colored-lighting", "foliage", "path-tracing", "pbr", "reflections", "shadows"]}
PT = {"Mods": "mod", "Resource Packs": "resourcepack", "Shaders": "shader"}
MR_SORT = {"Relevance": "relevance", "Downloads": "downloads", "Updated": "updated", "Newest": "newest"}


# ----------------------------------------------------------------- MODERN WEB UI (local app window)
HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>White Shadow</title><link rel="icon" type="image/png" href="data:image/png;base64,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"><meta name="theme-color" content="#0a0614"><meta name="color-scheme" content="dark">
<style>
:root{--bg:#07050d;--p:#0e0a1a;--p2:#171028;--p3:#20163a;--line:#2a1d4d;--fg:#f1ecff;--dim:#9c8fc4;--ac:var(--ac);--ac2:var(--ac2);--ok:#22d3a0;--bad:#ff5d73;color-scheme:dark}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;color:var(--fg);font:14px/1.5 "Bahnschrift","Segoe UI Variable Display","Segoe UI",Inter,system-ui,sans-serif;overflow:hidden;display:grid;grid-template-columns:244px 1fr;grid-template-rows:auto minmax(0,1fr) auto;
background:radial-gradient(900px 520px at 88% -10%,#5b21b655,transparent 60%),radial-gradient(700px 500px at -5% 110%,color-mix(in srgb,var(--ac2) 13%,transparent),transparent 60%),var(--bg)}
button,input,select{font:inherit;color:inherit}:focus-visible{outline:2px solid var(--ac2);outline-offset:2px}
.ic{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none}
#side{grid-row:2;background:linear-gradient(180deg,#120c24,#0a0714);border-right:1px solid var(--line);padding:22px 14px;display:flex;flex-direction:column;gap:6px}
.logo{display:flex;gap:12px;align-items:center;padding:0 8px 22px}.logo i{width:42px;height:42px;display:grid;place-items:center;font-family:inherit;font-size:15px;font-weight:800;line-height:1;font-style:normal;background:linear-gradient(135deg,var(--ac),var(--ac2));clip-path:polygon(10px 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%,0 10px);box-shadow:0 0 24px color-mix(in srgb,var(--ac) 67%,transparent)}
.logo b{font-size:16px;letter-spacing:.14em;line-height:1.1}.logo b em{display:block;font-style:normal;color:var(--ac2)}
#nav button{display:flex;gap:14px;align-items:center;width:100%;padding:12px 14px;border:0;border-radius:12px;background:none;color:var(--dim);cursor:pointer;font-weight:700;font-size:15px;text-align:left;position:relative;transition:.18s}
#nav button:hover{background:#1a1230;color:var(--fg);transform:translateX(3px)}
#nav button.on{background:linear-gradient(90deg,color-mix(in srgb,var(--ac) 20%,transparent),transparent);color:#fff}#nav button.on::before{content:"";position:absolute;left:-14px;top:8px;bottom:8px;width:4px;border-radius:0 4px 4px 0;background:linear-gradient(var(--ac),var(--ac2));box-shadow:0 0 14px var(--ac2)}
.ver{margin-top:6px;color:var(--dim);font-size:11px;padding:8px}
main{overflow:auto;padding:0 30px 24px;scroll-behavior:smooth}
.top{display:flex;justify-content:space-between;align-items:center;padding:22px 0 14px;position:sticky;top:0;z-index:5;background:linear-gradient(var(--bg) 70%,transparent)}
h1{margin:0;font-size:24px;letter-spacing:.06em;text-transform:uppercase}h3{margin:22px 0 10px;font-size:12px;letter-spacing:.18em;color:#b794ff;text-transform:uppercase}
.acct{position:relative}.chip{display:flex;gap:10px;align-items:center;padding:6px 12px 6px 6px;border-radius:14px;background:var(--p2);border:1px solid var(--line);cursor:pointer;transition:.18s}.chip:hover{border-color:var(--ac);box-shadow:0 0 18px color-mix(in srgb,var(--ac) 33%,transparent)}
.chip img,.av{width:36px;height:36px;border-radius:9px;image-rendering:pixelated;background:var(--p3)}.chip small{display:block;color:#e0a030;font-size:11px;line-height:1}
.menu{position:absolute;right:0;top:calc(100% + 8px);min-width:250px;background:var(--p);border:1px solid var(--line);border-radius:14px;padding:8px;box-shadow:0 20px 60px #000c;display:none}.menu.open{display:block;animation:pop .15s}@keyframes pop{from{opacity:0;transform:translateY(-6px)}}
.menu button{display:flex;gap:10px;align-items:center;width:100%;border:0;background:none;padding:9px;border-radius:9px;cursor:pointer;text-align:left}.menu button:hover{background:var(--p3)}.menu .cur{outline:1px solid var(--ac)}
.card{background:linear-gradient(180deg,#1a1230cc,#120c24cc);border:1px solid var(--line);border-radius:16px;padding:16px;backdrop-filter:blur(8px);transition:.2s}
.hover:hover{transform:translateY(-3px);border-color:var(--ac);box-shadow:0 10px 36px color-mix(in srgb,var(--ac) 20%,transparent)}
.hero{position:relative;overflow:hidden;border-radius:20px;padding:30px;min-height:190px;border:1px solid #5b21b6;background:linear-gradient(110deg,#4c1d95,#1a0f3a 55%,#0e0a1a);display:flex;justify-content:space-between;align-items:flex-end;gap:20px;flex-wrap:wrap}
.hero::before,.hero::after{content:"";position:absolute;top:-20%;bottom:-20%;width:70px;transform:skewX(-24deg);background:linear-gradient(#a855f7aa,#7c3aed44);right:34%}.hero::after{right:26%;width:30px;background:color-mix(in srgb,var(--ac2) 33%,transparent)}
.hero>*{position:relative;z-index:1}.eyebrow{color:#d8c8ff;letter-spacing:.3em;font-size:11px;font-weight:700}.hname{font-size:44px;font-weight:800;letter-spacing:.04em;margin:2px 0;text-shadow:3px 3px 0 color-mix(in srgb,var(--ac2) 53%,transparent);word-break:break-all}
.tiles{display:flex;gap:10px;flex-wrap:wrap}.tile{min-width:112px;padding:10px 14px;background:#0e0a1acc;border:1px solid var(--ac);clip-path:polygon(10px 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%,0 10px)}.tile small{display:block;color:#b794ff;font-size:10px;letter-spacing:.16em;font-weight:700}.tile b{font-size:22px}
.cols{display:grid;grid-template-columns:1fr 290px;gap:18px}@media(max-width:1100px){.cols{grid-template-columns:1fr}}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(240px,1fr))}
.btn{border:1px solid var(--line);background:var(--p2);padding:9px 16px;border-radius:11px;cursor:pointer;font-weight:700;transition:.16s}.btn:hover{border-color:var(--ac);background:var(--p3)}.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.pri{border:0;background:linear-gradient(135deg,var(--ac),var(--ac2));color:#fff;box-shadow:0 6px 22px color-mix(in srgb,var(--ac) 33%,transparent)}.btn.pri:hover{filter:brightness(1.12);transform:translateY(-1px)}.btn.red:hover{border-color:var(--bad);color:var(--bad)}
input[type=text],input[type=number],input[type=search],select{background:var(--p2);border:1px solid var(--line);border-radius:11px;padding:9px 12px;min-width:0;transition:.16s}input:focus,select:focus{border-color:var(--ac);box-shadow:0 0 0 3px color-mix(in srgb,var(--ac) 20%,transparent);outline:0}option{background:var(--p2)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.dim{color:var(--dim)}.sp{flex:1}
.seg{display:inline-flex;background:var(--p2);border:1px solid var(--line);border-radius:12px;padding:4px;gap:2px;margin-right:10px}.seg button{border:0;background:none;padding:7px 16px;border-radius:9px;cursor:pointer;font-weight:700;color:var(--dim)}.seg button.on{background:linear-gradient(135deg,var(--ac),var(--ac2));color:#fff}
.mod{display:flex;gap:12px;cursor:pointer;position:relative;min-height:124px;padding-bottom:34px}.mod.sel{border-color:var(--ac2);box-shadow:0 0 0 1px var(--ac2) inset,0 8px 30px color-mix(in srgb,var(--ac2) 20%,transparent)}.mod.sel::after{content:"✓";position:absolute;right:10px;top:8px;background:var(--ac2);width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:13px;font-weight:800}
.mi{width:52px;height:52px;border-radius:12px;background:var(--p3);object-fit:cover;flex:none;display:grid;place-items:center;color:#4a3a7a;font-size:22px;overflow:hidden}
.mod b{display:block}.mod p{margin:3px 0 0;color:var(--dim);font-size:12px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.mod .by{color:#b794ff;font-size:12px}.mod .dl{position:absolute;right:14px;bottom:10px;color:var(--dim);font-size:11px}
.sw{width:44px;height:24px;border-radius:99px;background:#2a2147;position:relative;cursor:pointer;flex:none;transition:.2s;border:0}.sw.on{background:linear-gradient(135deg,var(--ac),var(--ac2));box-shadow:0 0 14px color-mix(in srgb,var(--ac) 53%,transparent)}.sw::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:.2s}.sw.on::after{left:23px}
.li{display:flex;gap:14px;align-items:center;padding:12px 14px;margin-bottom:10px}.li .mi{width:44px;height:44px}
.warn{padding:12px 16px;border-radius:12px;margin:12px 0;border:1px solid #e0a03066;background:#e0a03014;color:#ffd78a}.okb{border-color:#22d3a055;background:#22d3a010;color:#7ff0d2}
.pf .ico{font-size:30px}.badge{font-size:10px;letter-spacing:.14em;font-weight:800;padding:3px 9px;border-radius:99px;background:color-mix(in srgb,var(--ac) 20%,transparent);color:#d8c8ff}.badge.f{background:#d9c9a322;color:#e8dcc0}
.dock{grid-column:2;display:flex;gap:16px;align-items:center;padding:14px 30px;background:linear-gradient(180deg,#120c24ee,#0a0714);border-top:2px solid var(--ac);box-shadow:0 -10px 40px color-mix(in srgb,var(--ac) 13%,transparent);flex-wrap:wrap}
.dock .st{flex:1;min-width:200px}.dock .st b{display:block;font-size:13px}.bar{height:7px;border-radius:9px;background:var(--p3);margin-top:7px;overflow:hidden}.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--ac),var(--ac2));box-shadow:0 0 12px var(--ac2);transition:width .4s}
.play{border:0;cursor:pointer;color:#fff;font-family:inherit;font-size:21px;font-weight:800;line-height:1;letter-spacing:.2em;padding:18px 54px;background:linear-gradient(135deg,var(--ac),var(--ac2));clip-path:polygon(16px 0,100% 0,100% calc(100% - 16px),calc(100% - 16px) 100%,0 100%,0 16px);animation:glow 2.2s ease-in-out infinite;transition:.15s;filter:drop-shadow(0 0 14px var(--ac))}
.play:hover{filter:drop-shadow(0 0 22px var(--ac2)) brightness(1.1);transform:translateY(-2px)}.play:disabled{animation:none;background:#2a2147;color:var(--dim);filter:none;cursor:progress}@keyframes glow{50%{filter:drop-shadow(0 0 26px var(--ac2))}}
pre.log{background:#06040b;border:1px solid var(--line);border-radius:12px;padding:12px;max-height:210px;overflow:auto;color:#cdbff5;font:12px/1.5 Consolas,monospace;white-space:pre-wrap;margin:0}
.chart{display:flex;gap:10px;align-items:flex-end;height:96px}.chart div{flex:1;text-align:center;font-size:10px;color:var(--dim);display:flex;flex-direction:column;justify-content:flex-end;height:100%}.chart i{display:block;border-radius:5px 5px 0 0;background:#241b3d;min-height:4px;transition:height .5s}.chart i.v{background:linear-gradient(var(--ac2),var(--ac))}
.news p{margin:0 0 10px;color:var(--dim);font-size:12px;padding-left:12px;border-left:2px solid var(--ac)}
.modal{position:fixed;inset:0;background:#000b;display:grid;place-items:center;z-index:20;padding:20px;animation:pop .15s}.modal.hidden{display:none}.box{background:var(--p);border:1px solid var(--ac);border-radius:18px;padding:24px;max-width:560px;width:100%;max-height:90vh;overflow:auto;box-shadow:0 0 60px color-mix(in srgb,var(--ac) 33%,transparent)}
.box h2{margin:0 0 14px}.fld{display:grid;gap:5px;margin-bottom:12px}.fld>span{color:var(--dim);font-size:12px;letter-spacing:.06em}
#toasts{position:fixed;right:22px;top:80px;display:grid;gap:10px;z-index:30}.toast{background:var(--p);border:1px solid var(--ac);border-left-width:5px;border-radius:12px;padding:12px 16px;max-width:380px;box-shadow:0 12px 40px #000a;animation:pop .2s}.toast.bad{border-color:var(--bad)}
.fab{position:sticky;bottom:10px;display:flex;justify-content:center;margin-top:18px;pointer-events:none}.fab .btn{pointer-events:auto;padding:13px 30px;font-size:15px}
input[type=range]{accent-color:var(--ac2);padding:0}input[type=checkbox]{accent-color:var(--ac)}.g3{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(190px,1fr))}
.tabs{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}.err{color:var(--bad);margin:8px 0}
@media(max-width:900px){body{grid-template-columns:78px 1fr}#nav span,.logo b,.ver{display:none}.hname{font-size:30px}.dock{padding:12px 16px}.play{padding:16px 30px}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

html.rm *{animation:none!important;transition:none!important}
#side{overflow:auto}.sdiv{height:1px;background:var(--line);margin:10px 4px}.ib{display:flex;gap:12px;align-items:center;width:100%;padding:7px 10px;border:0;border-radius:12px;background:none;color:var(--dim);cursor:pointer;text-align:left;transition:.18s}
.ib i{font-style:normal;width:32px;height:32px;border-radius:9px;display:grid;place-items:center;background:var(--p3);font-size:17px;flex:none}.ib span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}.ib:hover{background:#1a1230;color:var(--fg)}.ib.on{background:color-mix(in srgb,var(--ac) 25%,transparent);color:#fff;box-shadow:0 0 0 1px var(--ac) inset}
.gear{display:flex;gap:12px;align-items:center;padding:11px 14px;border:0;border-radius:12px;background:var(--p2);color:var(--dim);cursor:pointer;font-weight:700;letter-spacing:.08em;font-size:12px;margin-top:10px}.gear:hover{color:#fff;box-shadow:0 0 0 1px var(--ac) inset}
.big{width:100%;padding:14px 16px;border-radius:14px;font-size:15px}.sm{font-size:12px}.btn.sm{padding:6px 12px}.btn.rnd{border-radius:50%;width:38px;height:38px;padding:0}
.seg.tight{margin:0;padding:3px}.seg.tight button{padding:5px 11px}
.icard{position:relative;overflow:hidden;cursor:pointer;min-height:150px;display:flex;flex-direction:column;gap:6px}.icard .ico{font-size:34px}.icard .ov{position:absolute;inset:0;background:#07050dcc;backdrop-filter:blur(3px);display:flex;gap:10px;align-items:center;justify-content:center;opacity:0;transition:.2s}.icard:hover .ov{opacity:1}
.chipv{background:var(--p3);padding:2px 9px;border-radius:7px;font:11px Consolas,monospace;color:var(--dim)}.lg{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(260px,1fr))}.lg.list{grid-template-columns:1fr}.lg.list .icard{flex-direction:row;align-items:center;min-height:0}
.ihead{padding:0;overflow:hidden}.ihead .top2{display:flex;gap:18px;align-items:center;padding:20px;flex-wrap:wrap}.ihead .big2{width:78px;height:78px;border-radius:16px;background:var(--p3);display:grid;place-items:center;font-size:40px;border:1px solid var(--line)}
.ihead h2{margin:4px 0 0;font-size:34px;letter-spacing:.02em}.stats{display:grid;grid-template-columns:repeat(3,1fr) auto auto;border-top:1px solid var(--line)}.stats>div{padding:12px 20px;border-right:1px solid var(--line)}.stats small{display:block;color:var(--dim);font-size:10px;letter-spacing:.16em;font-weight:700}.stats button{border:0;background:none;color:var(--dim);cursor:pointer;padding:10px 18px;font-size:11px}.stats button:hover{color:#fff;background:var(--p3)}
.tabsx{display:flex;gap:6px;border-bottom:1px solid var(--line);margin:18px 0 14px}.tabsx button{border:0;background:none;color:var(--dim);padding:11px 18px;cursor:pointer;font-weight:700;border-bottom:2px solid transparent;margin-bottom:-1px}.tabsx button.on{color:#fff;border-color:var(--ac)}
.pill{border:1px solid var(--line);background:var(--p2);color:var(--dim);padding:6px 14px;border-radius:99px;cursor:pointer;font-weight:600;font-size:12px;transition:.15s}.pill:hover{border-color:var(--ac);color:#fff}.pill.on{background:color-mix(in srgb,var(--ac) 28%,transparent);border-color:var(--ac);color:#fff}.pills{display:flex;gap:7px;flex-wrap:wrap}
.cnt{background:var(--p3);border-radius:99px;padding:0 7px;font-size:10px;margin-left:5px}
.rowi{display:flex;gap:12px;align-items:center;padding:9px 12px;border-radius:12px;background:var(--p2);border:1px solid transparent;margin-bottom:7px;transition:.15s}.rowi:hover{border-color:var(--line);background:var(--p3)}.rowi .mi{width:36px;height:36px;border-radius:9px}
.disc{display:grid;grid-template-columns:1fr 270px;gap:20px;align-items:start}@media(max-width:1100px){.disc{grid-template-columns:1fr}}
.filters{position:sticky;top:76px}.filters h4{margin:16px 0 8px;font-size:11px;letter-spacing:.16em;color:var(--dim)}.filters h4:first-child{margin-top:0}.lock{padding:8px 12px;border-radius:10px;background:var(--p3);font-weight:700}.lock small{color:var(--dim);font-weight:400}
.cgrid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(250px,1fr))}.clist{display:grid;gap:10px}
.ccard{position:relative;overflow:hidden;border-radius:16px;border:1px solid var(--line);background:var(--p);transition:.2s}.ccard.hover:hover{transform:translateY(-3px);border-color:var(--ac);box-shadow:0 10px 30px color-mix(in srgb,var(--ac) 30%,transparent)}
.banner{height:112px;background:linear-gradient(135deg,color-mix(in srgb,var(--ac) 35%,#0e0a1a),#0e0a1a);background-size:cover;background-position:center}.ci{position:absolute;left:14px;top:80px;width:50px;height:50px;border-radius:12px;border:2px solid var(--p);background:var(--p3);object-fit:cover}
.cb{padding:34px 14px 12px}.cb p{margin:4px 0 10px;color:var(--dim);font-size:12px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;min-height:54px}.cb .by{color:#b794ff;font-size:12px}.ok{color:var(--ok);font-weight:700;font-size:12px}
.clist .ccard{display:flex}.clist .banner{width:150px;height:auto;flex:none}.clist .ci{top:14px;left:96px}.clist .cb{padding:14px 14px 12px 60px;flex:1}
.srv{display:flex;gap:14px;align-items:center;padding:14px;margin-bottom:10px}.srv img,.srv .si{width:56px;height:56px;border-radius:12px;image-rendering:pixelated;background:var(--p3);display:grid;place-items:center;font-size:24px;flex:none}.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px;background:var(--bad)}.dot.on{background:var(--ok);box-shadow:0 0 10px var(--ok)}
.shots{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}.shots img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:12px;border:1px solid var(--line);cursor:zoom-in;transition:.2s}.shots img:hover{border-color:var(--ac);transform:scale(1.02)}
.box.wide{max-width:1000px;height:min(740px,92vh);display:grid;grid-template-columns:230px 1fr;padding:0;overflow:hidden}.sn{padding:24px 14px;border-right:1px solid var(--line);background:#0a0714;overflow:auto}.sn h2{padding:0 10px}.sn button{display:block;width:100%;text-align:left;border:0;background:none;color:var(--dim);padding:10px 12px;border-radius:10px;cursor:pointer;font-weight:600;margin-bottom:3px}.sn button:hover{background:var(--p3);color:#fff}.sn button.on{background:color-mix(in srgb,var(--ac) 22%,transparent);color:#fff;box-shadow:0 0 0 1px var(--ac) inset}
.sp2{display:flex;flex-direction:column;min-height:0}.sb{flex:1;overflow:auto;padding:24px 28px}.sf{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:14px 28px;border-top:1px solid var(--line);color:var(--dim);font-size:12px}
.srow{display:flex;gap:16px;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid var(--line)}.srow:last-child{border:0}.srow>div:first-child{min-width:0}.sgrp{border:1px solid var(--line);border-radius:14px;background:var(--p2);margin-bottom:18px}
.sw2{display:flex;gap:10px}.swatch{width:38px;height:38px;border-radius:50%;border:3px solid transparent;cursor:pointer}.swatch.on{border-color:#fff;box-shadow:0 0 16px currentColor}

#tb{grid-column:1/3;display:flex;align-items:center;gap:18px;padding:0 18px;height:58px;background:linear-gradient(180deg,#0f0a1e,#0a0614);border-bottom:1px solid var(--line)}
.tl{display:flex;gap:12px;align-items:center;font-size:18px;font-weight:700;letter-spacing:.02em}.tl img{width:38px;height:38px;border-radius:10px}
.gs{margin-left:auto;display:none;align-items:center;gap:12px;min-width:260px}.gs.on{display:flex}.gs span{font-size:12px;color:#d8c8ff;white-space:nowrap;max-width:300px;overflow:hidden;text-overflow:ellipsis}.gs .bar{flex:1;margin:0;min-width:110px}
.tgear{margin-left:auto;border:0;background:none;color:var(--ac);cursor:pointer;padding:8px;border-radius:10px;transition:.18s}.gs.on+.tgear,#upd:not([hidden])+.tgear{margin-left:12px}#upd:not([hidden]){margin-left:auto}.gs.on~#upd{margin-left:12px}.tgear:hover{background:var(--p3);color:#fff}.tgear .ic{width:24px;height:24px}
#side{padding:14px 12px 14px;gap:6px}.grow{flex:1}#nav button{padding:13px 16px}#nav button.on{background:linear-gradient(90deg,color-mix(in srgb,var(--ac) 30%,transparent),color-mix(in srgb,var(--ac) 10%,transparent));color:#fff}
.acct{position:relative;margin-top:8px}.acct .chip{background:none;border-color:transparent;padding:8px}.acct .chip:hover{background:var(--p2)}.acct .chip img{width:46px;height:46px;border-radius:50%}.acct .chip b{font-size:16px}.acct .chip small{color:var(--ok)}
.menu.up{top:auto;bottom:calc(100% + 8px);left:0;right:auto}
.brandbox{display:flex;gap:12px;align-items:center;padding:12px 14px;margin-top:6px;border:1px solid var(--ac);border-radius:14px;background:color-mix(in srgb,var(--ac) 10%,transparent)}.brandbox img{width:38px;height:38px;border-radius:9px}.brandbox b{display:block}.brandbox small{color:#b794ff}
#foot{grid-column:1/3;display:flex;gap:18px;align-items:center;justify-content:center;height:40px;border-top:1px solid var(--line);font-size:11px;letter-spacing:.34em;color:#9b7ce0;position:relative;background:#09060f}#foot i{width:60px;height:1px;background:linear-gradient(90deg,transparent,var(--ac))}#foot i+span+i{background:linear-gradient(90deg,var(--ac),transparent)}#foot em{position:absolute;right:16px;font-style:normal;letter-spacing:.08em;color:var(--dim)}
.hh{display:grid;grid-template-columns:minmax(0,1fr) 310px;gap:22px;padding-top:6px}@media(max-width:1180px){.hh{grid-template-columns:1fr}}
.hero2{height:clamp(220px,31vw,340px);border-radius:20px;background:#07050d center/cover no-repeat;-webkit-mask-image:linear-gradient(to bottom,#000 93%,transparent);mask-image:linear-gradient(to bottom,#000 93%,transparent);margin:-6px -30px 0;border-radius:0}
.playrow{display:flex;gap:12px;justify-content:center;margin-top:2px;position:relative}.play{border-radius:12px;clip-path:none;padding:0 96px;height:66px;font-size:22px;letter-spacing:.02em;filter:none;box-shadow:0 0 0 1px color-mix(in srgb,var(--ac) 70%,#fff),0 8px 34px color-mix(in srgb,var(--ac) 55%,transparent);animation:glow2 2.6s ease-in-out infinite}
.play:hover{filter:brightness(1.12);box-shadow:0 0 0 1px #fff3,0 10px 44px color-mix(in srgb,var(--ac) 80%,transparent)}.play:disabled{box-shadow:none}@keyframes glow2{50%{box-shadow:0 0 0 1px color-mix(in srgb,var(--ac) 70%,#fff),0 8px 52px color-mix(in srgb,var(--ac2) 70%,transparent)}}
.chev{width:66px;height:66px;border-radius:12px;border:1px solid var(--ac);background:var(--p2);color:#fff;font-size:22px;cursor:pointer;transition:.18s}.chev:hover{background:var(--p3);box-shadow:0 0 18px color-mix(in srgb,var(--ac) 50%,transparent)}
.vsel{display:flex;gap:12px;align-items:center;justify-content:center;margin:14px auto 0;max-width:380px;background:var(--p);border:1px solid var(--line);border-radius:12px;padding:6px 14px}.vsel select{flex:1;background:none;border:0;font-size:15px;padding:8px 4px;box-shadow:none}.vsel:focus-within{border-color:var(--ac)}.gi{width:30px;height:30px;flex:none}
.status2{max-width:380px;margin:12px auto 0;text-align:center}.status2 b{font-size:12px;color:#d8c8ff}.status2 .bar{margin-top:6px}
.quick4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:20px 0}@media(max-width:700px){.quick4{grid-template-columns:repeat(2,1fr)}}
.qt{background:var(--p);border:1px solid var(--line);border-radius:16px;padding:20px 12px;text-align:center;cursor:pointer;transition:.2s;color:var(--fg)}.qt:hover{transform:translateY(-4px);border-color:var(--ac);box-shadow:0 10px 30px color-mix(in srgb,var(--ac) 30%,transparent)}.qt .ic{width:34px;height:34px;color:var(--ac);margin-bottom:8px}.qt b{display:block;font-size:15px}.qt small{color:var(--dim)}
.news2{display:flex;gap:18px;align-items:flex-start}.news2 img{width:210px;height:112px;object-fit:cover;border-radius:10px;border:1px solid var(--line);flex:none}.news2 h4{margin:0 0 2px;font-size:15px}.nl{display:flex;gap:10px;justify-content:space-between;font-size:12px;color:var(--dim);padding:4px 0;border-top:1px solid var(--line)}
.sc{padding:14px 16px;margin-bottom:16px}.sc h4{margin:0 0 12px;font-size:16px;display:flex;gap:10px;align-items:center}.sc h4 .ic{color:var(--ac)}.bc{display:flex;gap:14px;align-items:center}.bc img{width:62px;height:62px;border-radius:14px;border:1px solid var(--ac)}
.ql{display:flex;gap:12px;align-items:center;padding:8px 0}.ql .qi{width:46px;height:46px;border-radius:11px;background:var(--p3);display:grid;place-items:center;font-size:24px;flex:none}.ql .qi svg{width:32px;height:32px}.ql b{display:block}.ql small{color:var(--dim)}.ql .go{margin-left:auto;width:42px;height:42px;border-radius:10px;border:1px solid var(--line);background:var(--p3);color:var(--ac);cursor:pointer;font-size:15px;transition:.15s}.ql .go:hover{background:var(--ac);color:#fff}
.srvl{display:flex;gap:10px;align-items:center;padding:7px 0}.srvl img,.srvl .si2{width:38px;height:38px;border-radius:9px;image-rendering:pixelated;background:var(--p3);display:grid;place-items:center;flex:none}.srvl small{display:block;color:var(--ok);font-size:11px}.srvl small.off{color:var(--dim)}
details.lg2{margin-top:14px}details.lg2 summary{cursor:pointer;color:var(--dim);font-size:12px;letter-spacing:.14em}
.box.modw{max-width:600px}.mdh{display:flex;gap:14px;align-items:center}.mdh .mi{width:64px;height:64px;border-radius:14px}.mdh h2{margin:0}
.vrow{display:flex;gap:12px;align-items:center;justify-content:space-between;padding:12px 14px;border-radius:12px;border:1px solid transparent;background:var(--p2);margin-bottom:8px;cursor:pointer;transition:.15s}.vrow:hover{border-color:var(--line)}.vrow.on{border-color:var(--ac);background:color-mix(in srgb,var(--ac) 14%,transparent)}
.vt{font-size:11px;font-weight:700;padding:2px 9px;border-radius:99px;margin-left:8px}.vt.release{background:#16a34a33;color:#4ade80}.vt.beta{background:#d9770633;color:#fbbf24}.vt.alpha{background:#dc262633;color:#f87171}
.dep{display:flex;gap:12px;align-items:center;padding:10px 12px;border-radius:12px;background:var(--p2);margin-bottom:7px}.dep img,.dep .di{width:30px;height:30px;border-radius:7px;background:var(--p3);flex:none;display:grid;place-items:center}.dep small{color:var(--dim);margin-left:8px}
.btn.grn{background:linear-gradient(135deg,#16a34a,#22c55e);color:#fff;border:0;box-shadow:0 6px 20px #16a34a55}.btn.grn:hover{filter:brightness(1.1)}
.skgrid{display:grid;grid-template-columns:280px 1fr;gap:20px;margin-top:16px}@media(max-width:900px){.skgrid{grid-template-columns:1fr}}.skl{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));margin-top:12px}
.skc{background:var(--p2);border:1px solid var(--line);border-radius:14px;padding:12px;text-align:center;cursor:pointer;transition:.18s;position:relative}.skc:hover{border-color:var(--ac);transform:translateY(-3px)}.skc.on{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok) inset}.skc canvas{width:64px;height:64px;image-rendering:pixelated;border-radius:8px;background:var(--p3)}.skc b{display:block;font-size:12px;margin-top:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.skc .act{position:absolute;top:6px;right:6px;background:var(--ok);color:#04130d;font-size:10px;font-weight:800;border-radius:99px;padding:1px 7px}.skc .x{position:absolute;top:4px;left:6px;border:0;background:none;color:var(--dim);cursor:pointer}.skc .x:hover{color:var(--bad)}
</style></head><body>
<header id="tb"><div class="tl"><img data-logo alt=""><span>White Shadow</span></div><div class="gs" id="gs"><span id="gst"></span><div class="bar"><i id="gpb"></i></div></div><button class="btn pri sm" id="upd" hidden style="margin-left:auto"></button><button class="tgear" id="gear" title="Settings"><svg class="ic" viewBox="0 0 24 24"><path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/></svg></button></header>
<aside id="side"><nav id="nav"></nav><div class="grow"></div><div class="acct"><div class="chip" id="chip" tabindex="0"></div><div class="menu up" id="menu"></div></div><div class="brandbox"><img data-logo alt=""><div><b>White Shadow</b><small>Play Beyond Limits</small></div></div></aside>
<main id="main"><div id="view"></div></main>
<footer id="foot"><i></i><span>WHITE SHADOW &mdash; BUILT FOR LEGENDS</span><i></i><em id="ver"></em></footer>
<div class="modal hidden" id="dlg" style="z-index:60"><div class="box" id="dbox" style="max-width:480px"></div></div><div id="toasts"></div><div class="modal hidden" id="modal"><div class="box" id="box"></div></div>
<script>
const TOKEN="__TOKEN__",LOGO="data:image/png;base64,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";const FACE="data:image/svg+xml,"+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><rect width="8" height="8" fill="#b98960"/><rect width="8" height="2" fill="#3b2a1a"/><rect x="1" y="3" width="2" height="1" fill="#fff"/><rect x="5" y="3" width="2" height="1" fill="#fff"/><rect x="2" y="3" width="1" height="1" fill="#4b3a8f"/><rect x="5" y="3" width="1" height="1" fill="#4b3a8f"/><rect x="3" y="5" width="2" height="1" fill="#7a4a3a"/></svg>');let S=null,T={},page="home",since=0,job={},lastJob=0,crashShown=false,logBuf=[];

const $=(q,r=document)=>r.querySelector(q),$$=(q,r=document)=>[...r.querySelectorAll(q)];
const E=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const t=k=>T[k]||k;
const IC={play:'<polygon points="6 3 20 12 6 21 6 3"/>',packs:'<path d="m9.060 11.900 8.070-8.060a2.850 2.850 0 1 1 4.030 4.030l-8.060 8.080"/><path d="M7.070 14.940c-1.660 0-3 1.350-3 3.020 0 1.330-2.500 1.520-2 2.020 1.080 1.100 2.490 2.020 4 2.020 2.200 0 4-1.800 4-4.040a3.010 3.010 0 0 0-3-3.020z"/>',skins:'<path d="M20.380 3.460 16 2a4 4 0 0 1-8 0L3.620 3.460a2 2 0 0 0-1.340 2.230l.58 3.470a1 1 0 0 0 .99.840H6v10c0 1.100.9 2 2 2h8a2 2 0 0 0 2-2V10h2.150a1 1 0 0 0 .99-.840l.58-3.470a2 2 0 0 0-1.340-2.230z"/>',rack:'<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01M6 18h.01"/>',discover:'<circle cx="12" cy="12" r="10"/><path d="m16.200 7.800-2.100 6.300-6.300 2.100 2.100-6.300z"/>',servers:'<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.300 15.300 0 0 1 4 10 15.300 15.300 0 0 1-4 10 15.300 15.300 0 0 1-4-10 15.300 15.300 0 0 1 4-10z"/>',library:'<rect x="2" y="3" width="20" height="5" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',home:'<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V12h6v10"/>',mods:'<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="m3 8 9 5 9-5"/><path d="M12 13v9"/>',suggested:'<path d="m12 2 3 7 7 .6-5.300 4.700 1.600 7L12 17.600 5.700 21.300l1.600-7L2 9.600 9 9z"/>',profiles:'<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',skin:'<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',settings:'<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>'};
const ic=n=>`<svg class="ic" viewBox="0 0 24 24">${IC[n]}</svg>`;
const NAV=[["home","home"],["library","play"],["mods","mods"],["packs","packs"],["servers","rack"],["skin","skins"],["settings","settings"]];
const lab=k=>({home:t("home"),library:t("play").replace(/.*/,x=>x[0]+x.slice(1).toLowerCase()),mods:t("mods"),packs:"Resource Packs",servers:"Servers",skin:t("skin")+"s",settings:t("settings"),discover:"Discover"}[k]||k);
function nav(k){if(k=="settings")return openSettings();if(k=="mods"||k=="packs"){M.type=k=="mods"?"Mods":"Resource Packs";M.cats.clear();M.res=[];return go("discover")}go(k)}
const ACC={violet:["#8b5cf6","#d946ef"],blue:["#6366f1","#38bdf8"],rose:["#e11d48","#f472b6"],teal:["#14b8a6","#a3e635"]};
function theme(a){const c=ACC[a||S.cfg.accent]||ACC.violet,r=document.documentElement;r.style.setProperty("--ac",c[0]);r.style.setProperty("--ac2",c[1]);r.classList.toggle("rm",!!S.cfg.reduce_motion)}
const fmt=n=>n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"K":String(n);
const ago=ts=>{if(!ts)return"Never";const m=(Date.now()/1000-ts)/60;return m<2?"Just now":m<90?Math.round(m)+" minutes ago":m<2160?Math.round(m/60)+" hours ago":Math.round(m/1440)+" days ago"};
const dur=s=>{const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h}h ${m}m`:`${m}m`};
const sz=n=>n>=2**30?(n/2**30).toFixed(1)+" GB":n>=2**20?(n/2**20).toFixed(1)+" MB":Math.max(1,Math.round(n/1024))+" KB";
const ask=async m=>S.cfg.skip_confirm||await yesno(m,"Yes",true);
const lbadge=p=>`<span class="badge ${p.loader=="fabric"?"f":""}">${p.loader=="fabric"?"✂ ":""}${E(p.loader).toUpperCase()}</span>`;
async function api(p,body,raw){const o={headers:{"X-WS-Token":TOKEN}};if(body!==undefined){o.method="POST";if(raw){o.body=body}else{o.body=JSON.stringify(body);o.headers["Content-Type"]="application/json"}}
 const r=await fetch("/api/"+p,o);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
function toast(m,bad){const d=document.createElement("div");d.className="toast"+(bad?" bad":"");d.textContent=m;$("#toasts").append(d);setTimeout(()=>d.remove(),bad?7000:3800)}
function yesno(msg,ok="Yes",danger){return new Promise(r=>{$("#dbox").innerHTML=`<div style="font-size:16px;line-height:1.55">${E(msg).replace(/\n/g,"<br>")}</div><div class="row" style="justify-content:flex-end;margin-top:20px"><button class="btn" id="dn">Cancel</button><button class="btn ${danger?"red":"pri"}" id="dy">${E(ok)}</button></div>`;$("#dlg").classList.remove("hidden");
 const fin=v=>{$("#dlg").classList.add("hidden");r(v)};$("#dn").onclick=()=>fin(false);$("#dy").onclick=()=>fin(true);$("#dy").focus();$("#dlg").onclick=e=>{if(e.target.id==="dlg")fin(false)};$("#dlg").onkeydown=e=>{if(e.key==="Escape")fin(false)}})}
function promptBox(msg,ph=""){return new Promise(r=>{$("#dbox").innerHTML=`<div style="font-size:16px;margin-bottom:12px">${E(msg)}</div><input type="text" id="pv" placeholder="${E(ph)}" style="width:100%"><div class="row" style="justify-content:flex-end;margin-top:18px"><button class="btn" id="dn">Cancel</button><button class="btn pri" id="dy">OK</button></div>`;$("#dlg").classList.remove("hidden");
 const fin=v=>{$("#dlg").classList.add("hidden");r(v)};$("#dn").onclick=()=>fin(null);$("#dy").onclick=()=>fin($("#pv").value.trim()||null);$("#pv").focus();$("#pv").onkeydown=e=>{if(e.key==="Enter")fin($("#pv").value.trim()||null);if(e.key==="Escape")fin(null)}})}
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&$("#dlg").classList.contains("hidden")&&!$("#modal").classList.contains("hidden"))closeModal()});
function modal(h,c){$("#box").className="box "+(c||"");$("#box").innerHTML=h;$("#modal").classList.remove("hidden")}const closeModal=()=>{SOPEN=false;$("#modal").classList.add("hidden")};
$("#modal").onclick=e=>{if(e.target.id==="modal")closeModal()};
const prof=()=>S.cfg.profiles[S.cfg.selected]||{};
async function boot(){S=await api("boot");T=S.t;theme();$("#ver").textContent="v"+S.ver;shell();paintAcct();paintProfiles();go(page)}
function shell(){const hl=page=="inst"?"library":page=="discover"?(M.type=="Resource Packs"?"packs":"mods"):page;
 $("#nav").innerHTML=NAV.map(([k,i])=>`<button data-go="${k}" class="${k==hl?"on":""}">${ic(i)}<span>${E(lab(k))}</span></button>`).join("");$$("#nav button").forEach(b=>b.onclick=()=>nav(b.dataset.go))}
$("#gear").onclick=()=>openSettings();$$("[data-logo]").forEach(i=>i.src=LOGO);
function go(p){page=p;shell();$("#main").scrollTop=0;({home:rHome,discover:rDiscover,servers:rServers,skin:rSkin,library:rLibrary,inst:rInst})[p]()}
function head(n){return `https://minotar.net/helm/${encodeURIComponent(n)}/48.png`}
function paintAcct(){const u=S.cfg.username;$("#chip").innerHTML=`<img src="${head(u)}" alt="" onerror="this.onerror=null;this.src=FACE"><span><b>${E(u)}</b><small>● OFFLINE ACCOUNT</small></span>`;
 $("#menu").innerHTML=(S.cfg.accounts||[u]).map(a=>`<button data-use="${E(a)}" class="${a==u?"cur":""}"><img class="av" src="${head(a)}" alt="" onerror="this.onerror=null;this.src=FACE"><span><b>${E(a)}</b><br><small class="dim">offline</small></span></button>`).join("")+`<button data-add>＋ Add offline account</button><button data-rm>✕ Remove current account</button>`;
 $$("#menu [data-use]").forEach(b=>b.onclick=()=>acct("use",b.dataset.use));$("#menu [data-add]").onclick=async()=>{const n=await promptBox("Offline username (3-16 letters, numbers, _)","e.g. ShadowRex");if(n)acct("add",n)};$("#menu [data-rm]").onclick=()=>acct("remove",u)}
async function acct(a,name){try{S.cfg=(await api("account",{action:a,name})).cfg;paintAcct();$("#menu").classList.remove("open");if(page=="home"||page=="skin")go(page)}catch(e){toast(e.message,1)}}
$("#chip").onclick=e=>{e.stopPropagation();$("#menu").classList.toggle("open")};document.addEventListener("click",e=>{if(!$("#menu").contains(e.target))$("#menu").classList.remove("open")});
function paintProfiles(){const ps=$("#psel");if(!ps)return;ps.innerHTML=Object.entries(S.cfg.profiles).map(([n,p])=>`<option value="${E(n)}" ${n==S.cfg.selected?"selected":""}>${E(n)} · ${E(p.version||"latest")} ${E(p.loader)}</option>`).join("")}
function paintDock(){const b=job.busy||job.running,txt=job.status=="Ready"?t("ready"):(job.status||t("ready"));
 const pl=$("#play");if(pl){pl.disabled=!!b;pl.textContent=b?t("running"):"▶  "+lab("library")}const sp=$("#stop");if(sp)sp.hidden=!job.running;const ps=$("#psel");if(ps)ps.disabled=!!b;
 const s=$("#st");if(s)s.textContent=txt;const pb=$("#pb");if(pb)pb.style.width=(job.busy?job.pct:0)+"%";
 $("#gs").classList.toggle("on",!!job.busy);$("#gst").textContent=txt+(job.busy?` · ${job.pct}%`:"");$("#gpb").style.width=job.pct+"%"}
// ---------- HOME
const GRASS='<svg class="gi" viewBox="0 0 32 32"><path d="M16 3 28 9.500v13L16 29 4 22.500v-13z" fill="#6b4423"/><path d="M16 3 28 9.500 16 16 4 9.500z" fill="#4cae3b"/><path d="M4 9.500 16 16v13L4 22.500z" fill="#7a4f2a"/><path d="M4 9.500 16 16v4L4 13.500z" fill="#3c8f2e"/></svg>';
const FABRIC='<svg class="gi" viewBox="0 0 24 24" fill="none" stroke="#d9c9a3" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 21h11a2 2 0 0 0 2-2v-2H9v2a2 2 0 0 1-4 0V5a2 2 0 1 0-4 0v3h4"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/></svg>';
const lico=p=>p.loader=="fabric"?FABRIC:GRASS;
function rHome(){const c=S.cfg,p=prof(),news=S.news.length?S.news:["Loading news..."];
 const q=(k,i,a,b)=>`<div class="qt" data-n="${k}">${ic(i)}<b>${a}</b><small>${b}</small></div>`;
 $("#view").innerHTML=`<div class="hh"><div>
 <div class="hero2" style="background-image:url(data:image/jpeg;base64,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)"></div>
 <div class="playrow"><button class="play" id="play"></button><button class="chev" id="chev" title="More">⌄</button><div class="menu" id="cm" style="top:100%;right:0;left:auto"><button data-c="inst">Open instance page</button><button data-c="set">Instance settings</button><button data-c="dir">Open game folder</button><button data-c="log">Open launcher log</button></div></div>
 <div class="vsel">${lico(p)}<select id="psel" aria-label="Instance"></select></div>
 <div class="status2"><b id="st"></b><div class="bar"><i id="pb"></i></div><button class="btn red sm" id="stop" hidden style="margin-top:8px">STOP</button></div>
 <div class="quick4">${q("mods","mods","Mods","Enhance your world")}${q("packs","packs","Resource Packs","Change the look")}${q("servers","rack","Servers","Play with friends")}${q("skin","skins","Skins","Be who you want")}</div>
 <div class="card"><div class="row" style="margin-bottom:12px"><b style="font-size:16px">📰 Latest News</b><span class="sp"></span><a href="#" id="va" style="color:#b794ff;text-decoration:none">View All →</a></div>
 <div class="news2"><img src="data:image/jpeg;base64,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" alt=""><div style="flex:1;min-width:0"><h4>${E(news[0].split("  -  ")[0])}</h4><div class="dim sm" style="margin-bottom:8px">${E((news[0].split("  -  ")[1]||""))} · from Mojang</div>${news.slice(1,4).map(n=>`<div class="nl"><span>${E(n.split("  -  ")[0])}</span><span>${E(n.split("  -  ")[1]||"")}</span></div>`).join("")}</div></div></div>
 <details class="lg2"><summary>GAME LOG</summary><pre class="log" id="log" style="margin-top:8px"></pre></details></div>
 <div><div class="card sc"><div class="bc"><img src="${LOGO}" alt=""><div><b style="font-size:17px">White Shadow</b><div class="dim sm">Minecraft Launcher</div></div></div><p class="dim" style="margin:12px 0 0">Same game. A new shadow.</p></div>
 <div class="card sc"><h4>${ic("home").replace("<svg",'<svg style="display:none"')}⚡ Quick Launch</h4>${Object.entries(c.profiles).map(([n,x])=>`<div class="ql"><div class="qi">${x.loader=="fabric"?FABRIC:GRASS}</div><div style="min-width:0"><b>${E(n)}</b><small>${E(x.version||"latest")} · ${E(x.loader)}</small></div><button class="go" data-l="${E(n)}" title="Play">▶</button></div>`).join("")}</div>
 <div class="card sc"><h4>🖥 Servers</h4><div id="srvs"><p class="dim sm">Checking…</p></div></div></div></div>`;
 paintProfiles();paintDock();$("#psel").onchange=async e=>{S.cfg.selected=e.target.value;await api("select",{name:e.target.value});go("home")};
 $("#play").onclick=()=>launch($("#psel").value);$("#stop").onclick=()=>api("stop",{});$("#chev").onclick=e=>{e.stopPropagation();$("#cm").classList.toggle("open")};
 $$("#cm [data-c]").forEach(b=>b.onclick=()=>{$("#cm").classList.remove("open");const k=b.dataset.c;if(k=="inst")openInst(S.cfg.selected);else if(k=="set")pform(S.cfg.selected);else if(k=="dir")api("open",{what:"root"});else api("logfile",{})});
 $$(".qt").forEach(e=>e.onclick=()=>nav(e.dataset.n));$$("[data-l]").forEach(b=>b.onclick=()=>launch(b.dataset.l));
 $("#va").onclick=e=>{e.preventDefault();modal(`<h2>Minecraft news</h2>${S.news.map(n=>`<div class="nl" style="font-size:14px;color:var(--fg)"><span>${E(n.split("  -  ")[0])}</span><span class="dim">${E(n.split("  -  ")[1]||"")}</span></div>`).join("")}<div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn" onclick="closeModal()">Close</button></div>`)};
 $("#log").textContent=logBuf.join("\n");
 api("servers").then(l=>{const el=$("#srvs");if(!el)return;el.innerHTML=l.slice(0,4).map(s=>`<div class="srvl"><div class="si2">${s.icon?`<img src="${s.icon}" alt="">`:"🖥"}</div><div style="min-width:0"><b>${E(s.name)}</b><small class="${s.ok?"":"off"}">${s.ok?`● ${s.online}/${s.max} online`:"● Offline"}</small></div></div>`).join("")||'<p class="dim sm">Add servers to see their status here.</p>'}).catch(()=>{})}
// ---------- DISCOVER (content browser)
let M={type:"Mods",src:"Modrinth",q:"",sort:"Downloads",cats:new Set(),res:[],total:0,view:"grid",busy:false};
function rDiscover(){const p=prof(),cats=S.cats[M.type].filter(x=>x!="all");
 $("#view").innerHTML=`<div class="disc"><div><div class="row"><div class="sp"><h2 style="margin:0">Content Browser</h2><div class="dim">Browse and install content for your instance</div></div><button class="btn" id="ipick">${E(p.icon||"⛏")} <b>${E(S.cfg.selected)}</b> ▾</button><button class="btn pri" id="perf">⚡ ${E(t("perf_pack"))}</button></div>
 <input type="search" id="q" class="big" placeholder="Search ${M.type.toLowerCase()}..." value="${E(M.q)}" style="margin:16px 0">
 <div class="row" style="margin-bottom:14px"><b>${M.type.replace(/s$/,"")}</b><span class="dim" id="cnt"></span><span class="sp"></span><select id="sort">${["Relevance","Downloads","Updated","Newest"].map(x=>`<option ${x==M.sort?"selected":""}>${x}</option>`)}</select><div class="seg tight"><button data-v="grid" class="${M.view=="grid"?"on":""}">▦</button><button data-v="list" class="${M.view=="list"?"on":""}">☰</button></div></div>
 <div class="${M.view=="grid"?"cgrid":"clist"}" id="res"></div><div class="row" style="justify-content:center;margin:18px 0" id="more"></div></div>
 <aside class="filters card"><h4>SOURCE</h4><div class="pills">${["Modrinth","CurseForge"].map(x=>`<button data-src="${x}" class="pill ${M.src==x?"on":""}">${x}</button>`).join("")}</div>
 <h4>CONTENT TYPE</h4><div class="pills">${["Mods","Resource Packs","Shaders"].map(x=>`<button data-ty="${x}" class="pill ${M.type==x?"on":""}">${x}</button>`).join("")}</div>
 <h4>GAME VERSION</h4><div class="lock">${E(p.version||"latest")} <small>(your instance)</small></div>${M.type=="Mods"?`<h4>MOD LOADER</h4><div class="lock">${E(p.loader)} <small>(your instance)</small></div>`:""}
 <h4>CATEGORIES</h4><div class="pills">${cats.map(x=>`<button data-cat="${x}" class="pill ${M.cats.has(x)?"on":""}">${x}</button>`).join("")}</div></aside></div>`;
 $$("[data-src]").forEach(b=>b.onclick=()=>{M.src=b.dataset.src;rDiscover()});$$("[data-ty]").forEach(b=>b.onclick=()=>{M.type=b.dataset.ty;M.cats.clear();M.res=[];rDiscover()});
 $$("[data-cat]").forEach(b=>b.onclick=()=>{const c=b.dataset.cat;M.cats.has(c)?M.cats.delete(c):M.cats.add(c);b.classList.toggle("on");dsearch()});
 $$("[data-v]").forEach(b=>b.onclick=()=>{M.view=b.dataset.v;rDiscover()});$("#sort").onchange=e=>{M.sort=e.target.value;dsearch()};
 let tm;$("#q").oninput=e=>{M.q=e.target.value;clearTimeout(tm);tm=setTimeout(dsearch,450)};$("#q").onkeydown=e=>{if(e.key=="Enter")dsearch()};
 $("#perf").onclick=()=>doInstall(S.suggested.filter(s=>s[2]=="Performance"||s[0]=="fabric-api").map(s=>({src:"Modrinth",id:s[0],name:s[1]})),"Mods");
 $("#ipick").onclick=()=>modal(`<h2>Choose instance</h2>${Object.entries(S.cfg.profiles).map(([n,p])=>`<div class="rowi" data-p="${E(n)}" style="cursor:pointer"><span style="font-size:24px">${E(p.icon||"⛏")}</span><b class="sp">${E(n)}</b>${lbadge(p)}<span class="chipv">${E(p.version||"latest")}</span></div>`).join("")}`);
 $$("#box [data-p]").forEach(r=>r.onclick=async()=>{S.cfg.selected=r.dataset.p;await api("select",{name:r.dataset.p});paintProfiles();closeModal();M.res=[];rDiscover()});
 if(M.res.length)paintCards();else dsearch()}
async function dsearch(more){if(M.busy)return;M.busy=true;if(!more){M.res=[];$("#res").innerHTML='<p class="dim">Searching...</p>'}
 try{const r=await api(`search?src=${M.src}&type=${encodeURIComponent(M.type)}&q=${encodeURIComponent(M.q)}&sort=${M.sort}&cats=${[...M.cats].join(",")}&off=${more?M.res.length:0}`);M.res=more?M.res.concat(r.items):r.items;M.total=r.total;paintCards()}catch(e){$("#res").innerHTML=`<p class="err">${E(e.message)}</p>`}M.busy=false}
function paintCards(){const r=$("#res");if(!r)return;$("#cnt").textContent=`(${M.total.toLocaleString()} results)`;
 r.innerHTML=M.res.map((m,i)=>`<div class="ccard hover"><div class="banner" ${m.banner?`style="background-image:url('${E(m.banner)}')"`:""}></div><img class="ci" src="${E(m.icon||"")}" alt="" onerror="this.style.visibility='hidden'"><div class="cb"><div class="row" style="flex-wrap:nowrap"><b class="sp" style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${E(m.name)}</b><span class="dim sm">⬇ ${fmt(m.downloads)}</span></div><p>${E(m.desc)}</p><div class="row" style="flex-wrap:nowrap"><span class="by sp">${E(m.author||"")}</span>${m.installed?'<span class="ok">✓ Installed</span>':`<button class="btn pri sm" data-i="${i}">⬇ Install</button>`}</div></div></div>`).join("")||'<p class="dim">No results.</p>';
 $$("[data-i]",r).forEach(b=>b.onclick=e=>{e.stopPropagation();const m=M.res[+b.dataset.i];b.disabled=true;b.textContent="Installing…";doInstall([{src:M.src,id:m.id,name:m.name}],M.type)});
 if(M.src=="Modrinth")$$(".ccard",r).forEach((c,i)=>{c.style.cursor="pointer";c.onclick=()=>openMod(i)});
 $("#more").innerHTML=M.res.length<M.total?'<button class="btn" id="lm">Load more</button>':"";if($("#lm"))$("#lm").onclick=()=>dsearch(true)}
async function openMod(i){const m=M.res[i],p=prof();
 modal(`<div class="mdh"><img class="mi" src="${E(m.icon||"")}" alt="" onerror="this.style.visibility='hidden'"><div class="sp"><h2>${E(m.name)}</h2><div class="dim">by ${E(m.author||"?")} · ⬇ ${fmt(m.downloads)}</div></div><button class="btn" onclick="closeModal()">✕</button></div><p class="dim">${E(m.desc)}</p>
 <div class="dim sm" style="margin:10px 0 6px;letter-spacing:.12em">DOWNLOAD MANUALLY</div><div class="row"><select disabled style="flex:1"><option>${E(p.version||"latest")}</option></select>${M.type=="Mods"?`<select disabled style="flex:1"><option>${E(p.loader)}</option></select>`:""}</div><div id="mv" style="margin-top:12px"><p class="dim">Loading versions…</p></div>`,"modw");
 let d;try{d=await api(`modinfo?id=${m.id}&type=${encodeURIComponent(M.type)}`)}catch(e){return $("#mv").innerHTML=`<p class="err">${E(e.message)}</p>`}
 if(!d.versions.length)return $("#mv").innerHTML='<div class="warn">No version of this is compatible with your instance.</div>';
 let sel=0;const paint=()=>{const v=d.versions[sel],req=v.deps.filter(x=>x.kind=="required"),opt=v.deps.filter(x=>x.kind=="optional");
  $("#mv").innerHTML=d.versions.map((x,i)=>`<div class="vrow ${i==sel?"on":""}" data-v="${i}"><div><b>${E(x.number)}</b><span class="vt ${x.type}">${x.type=="release"?"Release":x.type=="beta"?"Beta":"Alpha"}</span><div class="dim sm">${ago(Date.parse(x.date)/1000)} · ${sz(x.size)}</div></div><span class="dim">⬇</span></div>`).join("")+
  `<h3 style="margin:16px 0 8px">Dependencies</h3>`+(req.length?req.map(x=>`<div class="dep"><div class="di">${x.icon?`<img src="${E(x.icon)}" alt="" style="width:30px;height:30px;border-radius:7px">`:"▣"}</div><b>${E(x.title)}</b><small>${x.vid?"Specific version":"Any compatible"}</small><span class="sp"></span>${x.installed?'<span class="ok">✓ Installed</span>':'<span class="dim sm">will be installed</span>'}</div>`).join(""):'<p class="dim sm">None — this works on its own.</p>')+(opt.length?`<div class="dim sm" style="margin-top:8px">Optional: ${opt.map(x=>E(x.title)).join(", ")}</div>`:"")+
  `<div class="row" style="justify-content:flex-end;margin-top:18px"><button class="btn" id="mo">⬇ Install only this</button><button class="btn grn" id="md">⬇ Install with dependencies</button></div>`;
  $$("#mv [data-v]").forEach(r=>r.onclick=()=>{sel=+r.dataset.v;paint()});
  const go2=nodeps=>{closeModal();doInstall([{src:"Modrinth",id:m.id,name:m.name,vid:v.id,nodeps}],M.type)};$("#md").onclick=()=>go2(false);$("#mo").onclick=async()=>{if(!req.length||await yesno("This mod needs other mods to run:\n"+req.map(x=>"• "+x.title).join("\n")+"\n\nInstall it WITHOUT them anyway?","Install anyway",true))go2(true)}};paint()}
async function doInstall(entries,type,sw){try{const r=await api("install",{entries,type,switch:sw});
 if(r.needs_loader){if(await yesno("Mods need a mod loader. Switch this instance to Fabric?","Switch to Fabric"))return doInstall(entries,type,true);return}
 if(r.cfg){S.cfg=r.cfg;paintProfiles()}if(S.cfg.notify!==false)toast("Installing "+entries.length+" item(s)...");logBuf=[]}catch(e){toast(e.message,1)}}
// ---------- LIBRARY
let LB={q:"",sort:"last",view:"grid"};
async function launch(n){try{S.cfg.selected=n;await api("select",{name:n});paintProfiles();await api("play",{name:n});logBuf=[]}catch(e){toast(e.message,1)}}
function rLibrary(){$("#view").innerHTML=`<div class="row"><div class="sp"><h2 style="margin:0">Library</h2><div class="dim">Manage your Minecraft instances</div></div><label class="btn">⬇ Import (.wsprof)<input type="file" id="imp" accept=".wsprof" hidden></label><button class="btn pri" id="np">＋ New Instance</button></div>
 <input type="search" id="q" class="big" placeholder="Search instances..." value="${E(LB.q)}" style="margin:16px 0"><div class="row" style="margin-bottom:14px"><b>Instances</b><span class="dim" id="lc"></span><span class="sp"></span><select id="sort">${[["last","Last Played"],["name","Name"],["play","Playtime"]].map(([k,l])=>`<option value="${k}" ${k==LB.sort?"selected":""}>${l}</option>`)}</select><div class="seg tight"><button data-v="grid" class="${LB.view=="grid"?"on":""}">▦</button><button data-v="list" class="${LB.view=="list"?"on":""}">☰</button></div></div><div id="lib"></div>`;
 $("#np").onclick=()=>pform();$("#q").oninput=e=>{LB.q=e.target.value;paintLib()};$("#sort").onchange=e=>{LB.sort=e.target.value;paintLib()};$$("[data-v]").forEach(b=>b.onclick=()=>{LB.view=b.dataset.v;rLibrary()});
 $("#imp").onchange=async e=>{try{const f=e.target.files[0],r=await api("import/check",await f.arrayBuffer(),true);
  if(await yesno(`Import "${r.name}"?\n\nMods are programs that run on your PC. Only import profiles from people you trust.\n${r.mods} mods will be downloaded from Modrinth; ${r.bundled} mod files are bundled in the file.`,"Import")){await api("import/run",{id:r.id});toast("Importing...")}}catch(x){toast(x.message,1)}};paintLib()}
function paintLib(){let l=Object.entries(S.cfg.profiles).filter(([n])=>n.toLowerCase().includes(LB.q.toLowerCase()));
 l.sort((a,b)=>LB.sort=="name"?a[0].localeCompare(b[0]):LB.sort=="play"?(b[1].playtime||0)-(a[1].playtime||0):(b[1].last_played||0)-(a[1].last_played||0));$("#lc").textContent=`(${l.length} found)`;
 $("#lib").innerHTML=`<div class="lg ${LB.view}">${l.map(([n,p])=>`<div class="card hover icard" data-n="${E(n)}"><div class="ov"><button class="btn pri" data-pl="${E(n)}">▶ Play Now</button><button class="btn rnd" data-ed="${E(n)}" title="Settings">⚙</button></div><div class="row" style="flex-wrap:nowrap"><span class="ico">${E(p.icon||"⛏")}</span><span class="sp"></span><span class="dim sm">${ago(p.last_played)}</span></div><b style="font-size:16px">${E(n)}</b><div class="dim sm">⏱ ${dur(p.playtime||0)}</div><div class="row" style="flex-wrap:nowrap">${lbadge(p)}<span class="sp"></span><span class="chipv">${E(p.version||"latest")}</span></div></div>`).join("")||'<p class="dim">No instances found.</p>'}</div>`;
 $$("#lib .icard").forEach(c=>{c.onclick=e=>{if(!e.target.closest("button"))openInst(c.dataset.n)};c.ondblclick=e=>{if(S.cfg.dbl_launch&&!e.target.closest("button"))launch(c.dataset.n)}});
 $$("[data-pl]").forEach(b=>b.onclick=()=>launch(b.dataset.pl));$$("[data-ed]").forEach(b=>b.onclick=async()=>{S.cfg.selected=b.dataset.ed;await api("select",{name:b.dataset.ed});pform(b.dataset.ed)})}
// ---------- INSTANCE PAGE
let IN={tab:"content",type:"Mods",q:""};
async function openInst(n){S.cfg.selected=n;await api("select",{name:n});paintProfiles();IN.tab="content";go("inst")}
async function rInst(){const n=S.cfg.selected,p=prof();let info;try{info=await api("inst?name="+encodeURIComponent(n))}catch(e){return go("library")}const busy=job.busy||job.running;
 $("#view").innerHTML=`<button class="btn" id="bk" style="margin-bottom:14px">← Back</button><div class="card ihead"><div class="top2"><div class="big2">${E(p.icon||"⛏")}</div><div class="sp"><div class="row">${lbadge(p)}<span class="chipv">MC ${E(p.version||"latest")}</span></div><h2>${E(n)}</h2></div><button class="play" id="ip">▶ PLAY</button></div>
 <div class="stats"><div><small>STATUS</small>${job.running?"Running":job.busy?"Working…":"Ready to play"}</div><div><small>LAST PLAYED</small>${ago(p.last_played)}</div><div><small>PLAYTIME</small>⏱ ${dur(p.playtime||0)}</div><button id="is">⚙<br>Instance Settings</button><div style="padding:0;position:relative"><button id="im" style="height:100%">⋮<br>More</button><div class="menu" id="mm" style="top:100%;right:0"><button data-m="dup">Duplicate</button><button data-m="share">Share (.wsprof)</button><button data-m="open">Open folder</button><button data-m="del" style="color:var(--bad)">Delete</button></div></div></div></div>
 <div class="tabsx">${[["content","Content"],["worlds","Worlds"],["gallery","Gallery"],["files","Files"]].map(([k,l])=>`<button data-tb="${k}" class="${IN.tab==k?"on":""}">${l}</button>`).join("")}</div><div id="ib"></div>`;
 $("#bk").onclick=()=>go("library");$("#ip").onclick=()=>launch(n);$("#ip").disabled=!!busy;$("#is").onclick=()=>pform(n);$("#im").onclick=e=>{e.stopPropagation();$("#mm").classList.toggle("open")};
 $$("[data-m]").forEach(b=>b.onclick=async()=>{const m=b.dataset.m;$("#mm").classList.remove("open");
  if(m=="dup"){S.cfg=(await api("profile/dup",{name:n})).cfg;paintProfiles();toast("Duplicated");go("library")}
  else if(m=="share"){toast("Building share file...");try{const r=await api("export",{name:n});toast("Saved to "+r.path)}catch(e){toast(e.message,1)}}
  else if(m=="open")api("open",{what:"root"});
  else if(await ask("Delete this instance and ALL its worlds/mods?"))try{S.cfg=(await api("profile/delete",{name:n})).cfg;paintProfiles();go("library")}catch(e){toast(e.message,1)}});
 $$("[data-tb]").forEach(b=>b.onclick=()=>{IN.tab=b.dataset.tb;$$("[data-tb]").forEach(x=>x.classList.toggle("on",x==b));itab(info)});itab(info)}
async function itab(info){const box=$("#ib");if(!box)return;box.innerHTML='<p class="dim">Loading…</p>';
 if(IN.tab=="content")return loadContent(info);
 try{if(IN.tab=="worlds"){const w=await api("worlds");box.innerHTML=`<div class="row" style="margin-bottom:10px"><span class="dim sp">${w.length} world(s)</span><button class="btn" id="ow">Open folder</button></div>`+(w.map(x=>`<div class="rowi"><span style="font-size:24px">🌍</span><div class="sp"><b>${E(x.name)}</b><div class="dim sm">${sz(x.size)} · ${ago(x.mtime)}</div></div><button class="btn red sm" data-wd="${E(x.name)}">${E(t("delete"))}</button></div>`).join("")||'<p class="dim">No worlds yet - start the game and create one.</p>');
   $("#ow").onclick=()=>api("open",{what:"worlds"});$$("[data-wd]").forEach(b=>b.onclick=async()=>{if(await ask("Delete world "+b.dataset.wd+" permanently?")){await api("world/delete",{name:b.dataset.wd});itab(info)}})}
  else if(IN.tab=="gallery"){const s=await api("shots");box.innerHTML=`<div class="row" style="margin-bottom:10px"><span class="dim sp">${s.length} screenshot(s)</span><button class="btn" id="os">Open folder</button></div><div class="shots">${s.map(x=>`<img loading="lazy" src="/api/shot?file=${encodeURIComponent(x.file)}&t=${TOKEN}" alt="${E(x.file)}" data-f="${E(x.file)}">`).join("")}</div>${s.length?"":'<p class="dim">No screenshots yet - press F2 in game.</p>'}`;
   $("#os").onclick=()=>api("open",{what:"shots"});$$(".shots img").forEach(i=>i.onclick=()=>modal(`<img src="${i.src}" style="width:100%;border-radius:12px"><div class="row" style="margin-top:12px;justify-content:space-between"><span class="dim">${E(i.dataset.f)}</span><button class="btn" onclick="closeModal()">Close</button></div>`))}
  else{const f=await api("files");box.innerHTML=`<div class="row" style="margin-bottom:10px"><span class="dim sp">Instance folder</span><button class="btn" id="of2">Open folder</button></div>`+f.map(x=>`<div class="rowi"><span style="font-size:20px">${x.dir?"📁":"📄"}</span><b class="sp">${E(x.name)}</b><span class="dim sm">${x.dir?"folder":sz(x.size)}</span></div>`).join("");$("#of2").onclick=()=>api("open",{what:"root"})}}
 catch(e){box.innerHTML=`<p class="err">${E(e.message)}</p>`}}
async function loadContent(info){const box=$("#ib");if(!box)return;let d;
 try{info=info||await api("inst?name="+encodeURIComponent(S.cfg.selected));d=await api("installed?type="+encodeURIComponent(IN.type))}catch(e){return box.innerHTML=`<p class="err">${E(e.message)}</p>`}
 const q=IN.q.toLowerCase(),items=d.items.filter(m=>m.name.toLowerCase().includes(q));
 box.innerHTML=`<div class="row" style="margin-bottom:12px"><div class="pills">${["Mods","Resource Packs","Shaders"].map(x=>`<button class="pill ${IN.type==x?"on":""}" data-it="${x}">${x}<span class="cnt">${info.counts[x]}</span></button>`).join("")}</div><input type="search" id="iq" placeholder="Search installed ${IN.type.toLowerCase()}..." value="${E(IN.q)}" style="flex:1;min-width:160px"><button class="btn" id="rf" title="Refresh">⟳</button><label class="btn" title="Add a file">＋ File<input type="file" id="af" accept=".jar,.zip" hidden multiple></label><button class="btn pri" id="ad">＋ Add</button></div>
 ${IN.type=="Mods"?`<div class="warn ${d.warns.length?"":"okb"}">${d.warns.length?d.warns.map(w=>"⚠ "+E(w)).join("<br>")+' <button class="btn sm" id="fx" style="margin-left:10px">Fix dependencies</button>':"✓ No duplicates or missing dependencies detected"}</div>`:""}
 ${items.map(m=>`<div class="rowi"><div class="mi">${m.icon?`<img class="mi" src="${m.icon}" alt="">`:"▣"}</div><div class="sp" style="min-width:0"><div class="row" style="gap:8px;flex-wrap:nowrap"><b>${E(m.name)}</b>${m.ver?`<span class="chipv">${E(m.ver)}</span>`:""}</div><div class="dim sm" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${E(m.desc||m.author||m.sub)}</div></div><button class="sw ${m.on?"on":""}" data-tg="${E(m.file)}" aria-label="toggle"></button><button class="btn red sm" data-rm="${E(m.file)}" title="Remove">🗑</button></div>`).join("")||'<p class="dim">Nothing installed yet. Press Add to browse.</p>'}`;
 $$("[data-it]").forEach(b=>b.onclick=()=>{IN.type=b.dataset.it;IN.q="";loadContent(info)});let tm;$("#iq").oninput=e=>{IN.q=e.target.value;clearTimeout(tm);tm=setTimeout(()=>loadContent(info),250)};
 $("#rf").onclick=()=>rInst();$("#ad").onclick=()=>{M.type=IN.type;M.res=[];M.cats.clear();go("discover")};
 $$("[data-tg]").forEach(b=>b.onclick=async()=>{await api("toggle",{type:IN.type,file:b.dataset.tg});loadContent(info)});
 $$("[data-rm]").forEach(b=>b.onclick=async()=>{if(await ask("Remove "+b.dataset.rm+"?")){await api("remove",{type:IN.type,files:[b.dataset.rm]});rInst()}});
 if($("#fx"))$("#fx").onclick=()=>api("fixdeps",{}).then(()=>toast("Resolving dependencies...")).catch(e=>toast(e.message,1));
 $("#af").onchange=async e=>{for(const f of e.target.files)await api(`addfile?type=${encodeURIComponent(IN.type)}&name=${encodeURIComponent(f.name)}`,await f.arrayBuffer(),true);rInst()}}
document.addEventListener("click",()=>{["#mm","#cm"].forEach(s=>{const m=$(s);if(m)m.classList.remove("open")})});
// ---------- PROFILE FORM
async function pform(old){const p=old?S.cfg.profiles[old]:{version:"",loader:S.cfg.default_loader||"fabric",ram:S.cfg.default_ram||4096,icon:"⛏",java:"",jvm:""};let vs=["latest"];try{vs=await api("versions")}catch(e){}
 modal(`<h2>${E(old?"Instance settings":"New instance")}</h2><div class="fld"><span>${E(t("name"))}</span><input type="text" id="f-n" value="${E(old||"New instance")}"></div>
 <div class="row"><div class="fld sp"><span>${E(t("version"))}</span><select id="f-v">${vs.map(v=>`<option ${v==(p.version||"latest")?"selected":""}>${v}</option>`)}</select></div><div class="fld"><span>${E(t("loader"))}</span><select id="f-l">${["vanilla","fabric"].map(v=>`<option ${v==p.loader?"selected":""}>${v}</option>`)}</select></div></div>
 <label class="dim"><input type="checkbox" id="f-s"> ${E(t("snapshots"))}</label><div class="fld" style="margin-top:12px"><span>RAM (MB): <b id="rv">${p.ram}</b></span><input type="range" id="f-r" min="1024" max="16384" step="512" value="${p.ram}" oninput="$('#rv').textContent=this.value"></div>
 <div class="fld"><span>Icon</span><select id="f-i">${[..."⛏⚔★♦☀❄♥⚡"].map(v=>`<option ${v==p.icon?"selected":""}>${v}</option>`)}</select></div><div class="fld"><span>Java (override)</span><input type="text" id="f-j" value="${E(p.java)}"></div><div class="fld"><span>JVM args (override)</span><input type="text" id="f-a" value="${E(p.jvm)}"></div><div class="err" id="f-e"></div>
 <div class="row" style="justify-content:flex-end"><button class="btn" onclick="closeModal()">Cancel</button><button class="btn pri" id="f-ok">${E(t("save"))}</button></div>`);
 $("#f-s").onchange=async e=>{const l=await api("versions?snap="+(e.target.checked?1:0));$("#f-v").innerHTML=l.map(v=>`<option>${v}</option>`).join("")};
 $("#f-ok").onclick=async()=>{try{S.cfg=(await api("profile",{old,name:$("#f-n").value,version:$("#f-v").value,loader:$("#f-l").value,ram:$("#f-r").value,icon:$("#f-i").value,java:$("#f-j").value,jvm:$("#f-a").value})).cfg;closeModal();paintProfiles();shell();go(old?"inst":"library")}catch(e){$("#f-e").textContent=e.message}}}
// ---------- SERVERS
async function rServers(){$("#view").innerHTML=`<div class="row"><div class="sp"><h2 style="margin:0">Servers</h2><div class="dim">Your multiplayer servers - live status, one-click join</div></div><button class="btn" id="rs">⟳ Refresh</button></div>
 <div class="card row" style="margin:16px 0"><input type="text" id="sn" placeholder="Name (optional)" style="width:180px"><input type="text" id="sa" placeholder="Address, e.g. play.example.com" style="flex:1;min-width:200px"><button class="btn pri" id="sa2">＋ Add server</button></div>
 <p class="dim sm">Offline accounts can only join servers that allow them (offline-mode / LAN). Online-mode servers need a purchased Minecraft account.</p><div id="sl"><p class="dim">Pinging servers…</p></div>`;
 const load=async()=>{let l;try{l=await api("servers")}catch(e){return $("#sl").innerHTML=`<p class="err">${E(e.message)}</p>`}
  $("#sl").innerHTML=l.map(s=>`<div class="card srv"><div class="si">${s.icon?`<img src="${s.icon}" alt="">`:"🖥"}</div><div class="sp" style="min-width:0"><b>${E(s.name)}</b> <span class="dim sm">${E(s.address)}</span><div class="sm"><span class="dot ${s.ok?"on":""}"></span>${s.ok?`${s.online}/${s.max} online · ${s.ms} ms · <span class="chipv">${E(s.version)}</span>`:`<span class="dim">Offline - ${E(s.error||"no response")}</span>`}</div>${s.ok&&s.motd?`<div class="dim sm" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${E(s.motd.split("\n")[0])}</div>`:""}</div><button class="btn pri" data-j="${E(s.address)}" ${s.ok?"":"disabled"}>▶ Join</button><button class="btn red sm" data-x="${E(s.address)}">✕</button></div>`).join("")||'<p class="dim">No servers yet - add one above.</p>';
  $$("[data-j]").forEach(b=>b.onclick=()=>api("server/join",{address:b.dataset.j}).then(()=>{logBuf=[];toast("Launching and joining…")}).catch(e=>toast(e.message,1)));
  $$("[data-x]").forEach(b=>b.onclick=async()=>{await api("server/remove",{address:b.dataset.x});rServers()})};
 $("#rs").onclick=()=>{$("#sl").innerHTML='<p class="dim">Pinging servers…</p>';load()};
 $("#sa2").onclick=async()=>{try{await api("server/add",{name:$("#sn").value,address:$("#sa").value});rServers()}catch(e){toast(e.message,1)}};load()}
// ---------- SKINS (saved library)
function drawBody(cv,src,hide,face){const x=cv.getContext("2d");x.imageSmoothingEnabled=false;x.clearRect(0,0,cv.width,cv.height);const im=new Image();im.onload=()=>{const L=im.height==32;
 if(face){x.drawImage(im,8,8,8,8,0,0,16,16);if(!hide)x.drawImage(im,40,8,8,8,0,0,16,16);return}
 const P=[[8,8,8,8,4,0],[20,20,8,12,4,8],[44,20,4,12,0,8],[L?44:36,L?20:52,4,12,12,8],[4,20,4,12,4,20],[L?4:20,L?20:52,4,12,8,20]],O=[[40,8,8,8,4,0],[20,36,8,12,4,8],[44,36,4,12,0,8],[52,52,4,12,12,8],[4,36,4,12,4,20],[4,52,4,12,8,20]];
 [...P,...(hide||L?[]:O)].forEach(a=>x.drawImage(im,a[0],a[1],a[2],a[3],a[4],a[5],a[2],a[3]))};im.src=src}
async function rSkin(){const c=S.cfg;let L=[];try{L=await api("skins")}catch(e){}const act=L.find(s=>s.active);
 $("#view").innerHTML=`<h2 style="margin:6px 0 0">Skins</h2><div class="dim">Be who you want — your active skin is saved and applied automatically every time you launch</div>
 <div class="skgrid"><div class="card" style="text-align:center;height:fit-content"><canvas id="cv" width="16" height="32" style="width:150px;height:300px;image-rendering:pixelated;background:var(--p3);border-radius:14px"></canvas><div style="margin-top:10px"><b>${act?E(act.name):"No skin selected"}</b></div>${act?'<div class="ok sm">✓ Saved · applied on every launch, in every instance</div>':'<div class="dim sm">You will see the default Steve/Alex skin</div>'}</div>
 <div><div class="card"><div class="row"><label class="btn pri">⬆ Upload skin (.png)<input type="file" id="sf" accept="image/png" hidden></label><span class="sp"></span><input type="text" id="un" placeholder="Import from a username" style="width:210px"><button class="btn" id="ui">Import</button></div>
 <div class="row" style="margin-top:14px"><button class="sw ${c.skin_hide?"on":""}" id="sh" aria-label="hide outer layer"></button><span>Hide outer layer (hat, jacket, sleeves, pants)</span></div><p class="dim sm" style="margin:10px 0 0">${E(t("skin_note"))} 64x32 skins are converted automatically.</p></div>
 <h3>Saved skins</h3><div class="skl">${L.map(s=>`<div class="skc ${s.active?"on":""}" data-s="${E(s.name)}">${s.active?'<span class="act">ACTIVE</span>':""}<button class="x" data-d="${E(s.name)}" title="Delete">✕</button><canvas width="16" height="16" data-f="${E(s.name)}"></canvas><b>${E(s.name)}</b></div>`).join("")||'<p class="dim">No saved skins yet — upload one or import by username.</p>'}</div></div></div>`;
 if(act)drawBody($("#cv"),`/api/skinfile?name=${encodeURIComponent(act.name)}&t=${TOKEN}`,c.skin_hide,false);
 $$("canvas[data-f]").forEach(cv=>drawBody(cv,`/api/skinfile?name=${encodeURIComponent(cv.dataset.f)}&t=${TOKEN}`,c.skin_hide,true));
 $$(".skc").forEach(e=>e.onclick=async()=>{await api("skin/use",{name:e.dataset.s});S.cfg=(await api("boot")).cfg;toast("Skin saved as active ✓");rSkin()});
 $$(".skc .x").forEach(b=>b.onclick=async e=>{e.stopPropagation();if(await ask("Delete this saved skin?")){await api("skin/delete",{name:b.dataset.d});S.cfg=(await api("boot")).cfg;rSkin()}});
 $("#sf").onchange=async e=>{try{const f=e.target.files[0],r=await api(`skin?name=${encodeURIComponent(f.name.replace(/\.png$/i,""))}`,await f.arrayBuffer(),true);S.cfg=r.cfg;toast(r.note||"Skin saved ✓");rSkin()}catch(x){toast(x.message,1)}};
 $("#ui").onclick=async()=>{try{const r=await api("skin/import",{name:$("#un").value});S.cfg=r.cfg;toast("Skin imported and saved ✓");rSkin()}catch(x){toast(x.message,1)}};
 $("#sh").onclick=async()=>{S.cfg.skin_hide=!S.cfg.skin_hide;await api("skin/hide",{on:S.cfg.skin_hide});rSkin()}}
// ---------- SETTINGS (modal with categories)
let SC="General",ST=null,SOPEN=false;
const SCATS=["General","Appearance","Java Manager","Storage","Default Options","Downloads","Privacy","Notifications","About"];
function openSettings(){ST={...S.cfg};SOPEN=true;sview();$("#modal").classList.remove("hidden")}
const srow=(ti,de,ct)=>`<div class="srow"><div><b>${ti}</b><div class="dim sm">${de}</div></div>${ct}</div>`;
const tgl=k=>`<button class="sw ${ST[k]?"on":""}" data-tk="${k}" aria-label="${k}"></button>`;
const inp=(k,ty="text",w=240)=>`<input type="${ty}" data-k="${k}" value="${E(ST[k]??"")}" style="width:${w}px">`;
function sview(){const body={
 General:`<h2>General</h2><div class="dim" style="margin-bottom:16px">General application settings and preferences</div><h3>Language</h3><div class="sgrp">${srow("Language","Choose your preferred language",`<select data-k="lang">${Object.entries(S.langs).map(([k,v])=>`<option value="${k}" ${k==(ST.lang||"en")?"selected":""}>${v}</option>`)}</select>`)}</div><h3>Application</h3><div class="sgrp">${srow("Double-click to launch","Start an instance by double-clicking it",tgl("dbl_launch"))}${srow("Skip delete confirmation","Don't ask before removing instances, worlds or content",tgl("skip_confirm"))}${srow("Disable playtime tracking","Don't record time spent playing",tgl("no_playtime"))}</div><button class="btn" id="rs">Restore defaults</button>`,
 Appearance:`<h2>Appearance</h2><div class="dim" style="margin-bottom:16px">Make the launcher yours</div><div class="sgrp">${srow("Accent color","Used for buttons, highlights and glows",`<div class="sw2">${Object.entries(ACC).map(([k,c])=>`<span class="swatch ${ST.accent==k?"on":""}" data-ac="${k}" style="background:linear-gradient(135deg,${c[0]},${c[1]});color:${c[0]}" title="${k}"></span>`).join("")}</div>`)}${srow("Reduce motion","Turn off animations and the pulsing glow",tgl("reduce_motion"))}</div>`,
 "Java Manager":`<h2>Java Manager</h2><div class="dim" style="margin-bottom:16px">Detected Java installations. Auto mode picks the right one for each Minecraft version and downloads it when missing.</div><div id="jm"><p class="dim">Scanning…</p></div><h3>Download Java</h3><div class="row">${[8,17,21,25].map(v=>`<button class="btn" data-jd="${v}">⬇ Java ${v}</button>`).join("")}</div>`,
 Storage:`<h2>Storage</h2><div class="dim" style="margin-bottom:16px">Where the launcher keeps its files</div><div id="sm"><p class="dim">Measuring…</p></div>`,
 "Default Options":`<h2>Default Options</h2><div class="dim" style="margin-bottom:16px">Used for new instances and game launches</div><div class="sgrp">${srow("Default memory (MB)","For new instances",inp("default_ram","number",120))}${srow("Default mod loader","For new instances",`<select data-k="default_loader">${["vanilla","fabric"].map(v=>`<option ${v==ST.default_loader?"selected":""}>${v}</option>`)}</select>`)}${srow("Game window size","Width x height, 0 = Minecraft default",`<div class="row">${inp("width","number",90)}<span>×</span>${inp("height","number",90)}</div>`)}${srow("Start in fullscreen","",tgl("fullscreen"))}${srow("Java path","Blank = automatic",inp("java","text",300))}${srow("Extra JVM arguments","Added to every launch, before an instance's own",inp("jvm_args","text",300))}</div>`,
 Downloads:`<h2>Downloads</h2><div class="dim" style="margin-bottom:16px">Speed, retries and content sources</div><div class="sgrp">${srow("Parallel downloads","1-64",inp("workers","number",90))}${srow("Retries per file","1-10",inp("retries","number",90))}${srow("CurseForge API key","Free at console.curseforge.com - Modrinth needs none",inp("cf_key","text",300))}</div>`,
 Privacy:`<h2>Privacy</h2><div class="dim" style="margin-bottom:16px">What leaves your computer</div><div class="card"><p>White Shadow sends <b>no analytics or telemetry</b>. Your settings, accounts and worlds stay on this PC.</p><p class="dim">Network requests only go to Mojang (game files), Fabric, Modrinth / CurseForge (when you browse content), Minotar (to draw your head) and the servers you add.</p></div><div class="row" style="margin-top:16px"><button class="btn" id="cl2">Clear launcher log</button></div>`,
 Notifications:`<h2>Notifications</h2><div class="dim" style="margin-bottom:16px">Pop-up messages inside the launcher</div><div class="sgrp">${srow("Install notifications","Show a message when downloads and installs finish",tgl("notify"))}</div>`,
 About:`<h2>About</h2><div class="card"><p><b>White Shadow Launcher</b> v${E(S.ver)}</p><p class="dim">${E(S.sys)} · data folder: ${E(S.base)}</p></div><div class="row" style="margin-top:14px"><button class="btn" id="lg">Open log</button></div>`}[SC];
 modal(`<div class="sn"><h2>Settings</h2>${SCATS.map(c=>`<button data-sc="${c}" class="${c==SC?"on":""}">${c}</button>`).join("")}</div><div class="sp2"><div class="sb">${body}<div class="err" id="se"></div></div><div class="sf"><span>White Shadow ${E(S.ver)} · ${E(S.sys)}</span><div class="row"><button class="btn" onclick="closeModal();theme()">Close</button><button class="btn pri" id="sv">✓ Save changes</button></div></div></div>`,"wide");
 $$("[data-sc]").forEach(b=>b.onclick=()=>{SC=b.dataset.sc;sview()});$$("[data-tk]").forEach(b=>b.onclick=()=>{ST[b.dataset.tk]=!ST[b.dataset.tk];b.classList.toggle("on")});
 $$("#box [data-k]").forEach(e=>e.onchange=()=>ST[e.dataset.k]=e.value);$$("[data-ac]").forEach(s=>s.onclick=()=>{ST.accent=s.dataset.ac;theme(ST.accent);sview()});
 $("#sv").onclick=async()=>{try{S=await api("settings",ST);T=S.t;SOPEN=false;closeModal();theme();shell();paintAcct();paintProfiles();toast("Settings saved ✓");go(page)}catch(e){$("#se").textContent=e.message}};
 if($("#rs"))$("#rs").onclick=async()=>{if(await yesno("Restore global settings to defaults? (Instances are not changed.)","Restore")){S=await api("settings/reset",{});T=S.t;ST={...S.cfg};theme();sview()}};
 if($("#cl2"))$("#cl2").onclick=async()=>{await api("logclear",{});toast("Log cleared")};if($("#lg"))$("#lg").onclick=()=>api("logfile",{});
 $$("[data-jd]").forEach(b=>b.onclick=()=>api("java/download",{major:b.dataset.jd}).then(()=>toast("Downloading Java "+b.dataset.jd+"…")).catch(e=>toast(e.message,1)));
 if(SC=="Java Manager")jpanel();if(SC=="Storage")spanel()}
async function jpanel(){const el=$("#jm");if(!el)return;try{const d=await api("javas");el.innerHTML=`<div class="sgrp">${srow("Automatic","Let the launcher choose per Minecraft version",`<button class="btn ${ST.java?"":"pri"}" data-uj="">${ST.java?"Use":"✓ In use"}</button>`)}${d.javas.map(j=>srow("Java "+j.major,E(j.path),`<button class="btn ${ST.java==j.path?"pri":""}" data-uj="${E(j.path)}">${ST.java==j.path?"✓ In use":"Use"}</button>`)).join("")}</div>`;
 $$("[data-uj]").forEach(b=>b.onclick=()=>{ST.java=b.dataset.uj;jpanel()})}catch(e){el.innerHTML=`<p class="err">${E(e.message)}</p>`}}
async function spanel(){const el=$("#sm");if(!el)return;try{const d=await api("storage"),tot=Object.values(d.parts).reduce((a,b)=>a+b,0);
 el.innerHTML=`<div class="sgrp">${srow("Data folder",E(d.base),`<button class="btn" id="dd">${E(t("change"))}</button>`)}${srow("Free space","On the drive holding the data folder",`<b>${(d.free/1024).toFixed(1)} GB</b>`)}</div><h3>Usage · ${sz(tot)}</h3><div class="sgrp">${Object.entries(d.parts).map(([k,v])=>srow(k,"",`<b>${sz(v)}</b>`)).join("")}</div><button class="btn" id="cl">${E(t("clean"))}</button>`;
 $("#dd").onclick=async()=>{const p=await promptBox("Folder for launcher data","e.g. E:\\Games");if(p)try{toast((await api("datadir",{path:p})).msg)}catch(e){toast(e.message,1)}};
 $("#cl").onclick=async()=>{if(await yesno("Delete downloaded game versions not used by any instance, plus temporary files?\n(They download again when needed.)","Free up space")){const r=await api("clean",{});toast("Freed "+(r.freed/1048576).toFixed(0)+" MB");spanel()}}}catch(e){el.innerHTML=`<p class="err">${E(e.message)}</p>`}}
// ---------- results, crash helper, polling
function onResult(r){if(r.error)return toast(r.error,1);if(r.ptype=="Profile"){boot();return S.cfg.notify!==false&&toast("Imported ✓")}
 if(r.ptype=="Java"){toast(r.ok[0]+" installed ✓");if(SOPEN&&SC=="Java Manager")jpanel();return}
 if(r.ok.length&&!r.bad.length&&S.cfg.notify!==false){const ex=(r.files||[]).length-r.ok.length;toast(`Installed ${r.ok.join(", ")}${ex>0?` + ${ex} required dependenc${ex>1?"ies":"y"}`:""} ✓`)}
 M.res.forEach(m=>{if(r.ok.includes(m.name))m.installed=true});if(page=="discover")paintCards();if(page=="inst"&&IN.tab=="content")rInst();
 if(r.bad.length){const retry=r.entries.filter(e=>r.bad.some(b=>b[0]==e[2]));modal(`<h2>Some items failed</h2>${r.bad.map(b=>`<div class="warn">⚠ <b>${E(b[0])}</b>: ${E(b[1])}</div>`).join("")}<div class="row" style="justify-content:flex-end"><button class="btn" onclick="closeModal()">Close</button>${retry.length?`<button class="btn pri" id="rt">Retry failed</button>`:""}</div>`);
  if(retry.length)$("#rt").onclick=()=>{closeModal();doInstall(retry.map(e=>({src:e[0],id:e[1],name:e[2]})),r.ptype)}}}
function crash(c){modal(`<h2>Why did Minecraft stop?</h2><p class="dim">The game exited with code ${c.code}</p>${c.findings.map((f,i)=>`<div class="card" style="margin-bottom:10px"><b>${E(f.title)}</b><p class="dim">${E(f.text)}</p>${f.fix?`<button class="btn pri" data-f="${i}">${E(f.fixlabel)}</button>`:""}</div>`).join("")}<div class="row" style="justify-content:flex-end"><button class="btn" id="ol">Open log</button><button class="btn" onclick="closeModal()">Close</button></div>`);
 $("#ol").onclick=()=>api("logfile",{});$$("[data-f]").forEach(b=>b.onclick=async()=>{const f=c.findings[b.dataset.f];try{const r=await api("fix",{kind:f.fix[0],arg:f.fix[1]});if(r.cfg){S.cfg=r.cfg;paintProfiles()}closeModal();toast(r.msg)}catch(e){toast(e.message,1)}})}
async function tick(){try{const s=await api("state?since="+since);since=s.seq;job=s;S&&(S.free=s.free);
  if(s.lines.length){logBuf=logBuf.concat(s.lines).slice(-400);const l=$("#log");if(l){l.textContent=logBuf.join("\n");l.scrollTop=1e9}}
  paintDock();if(!s.busy&&s.result&&s.id!==lastJob){lastJob=s.id;onResult(s.result)}
  if(s.crash&&!crashShown){crashShown=true;crash(s.crash)}if(!s.crash)crashShown=false}catch(e){}
 setTimeout(tick,job.busy||job.running?500:1500)}
boot().then(()=>api("update").then(u=>{if(u.new){const b=$("#upd");b.hidden=false;b.textContent="⬆ Update "+u.new+" available";b.onclick=()=>api("openurl",{url:u.url})}}).catch(()=>{})).then(tick).catch(e=>{document.body.innerHTML="<pre style='padding:30px;color:#ff5d73'>Could not start: "+E(e.message)+"</pre>"});
</script></body></html>
"""

import secrets, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NEWDEF = {"skip_confirm": False, "dbl_launch": True, "no_playtime": False, "reduce_motion": False, "accent": "violet",
          "default_ram": 4096, "default_loader": "fabric", "notify": True}
CFG = load_cfg()
for _k, _v in NEWDEF.items():
    CFG.setdefault(_k, _v)
CFG.setdefault("servers", [])
DL.update(workers=CFG["workers"], retries=CFG["retries"])
LOCK = threading.RLock()
LOG, NEWS, IMPORTS, PROC = [], [], {}, [None]
JOB = {"busy": False, "running": False, "kind": "", "status": "Ready", "pct": 0, "crash": None, "result": None,
       "id": 0, "seq": 0, "t0": 0.0, "last_ping": 0.0, "pinged": False}
TOKEN = secrets.token_urlsafe(24)
FOLDERS = {"Mods": "mods", "Resource Packs": "resourcepacks", "Shaders": "shaderpacks"}
USER_RE = re.compile(r"[A-Za-z0-9_]{3,16}")


def log(msg):
    msg = str(msg)
    with LOCK:
        LOG.append(msg)
        JOB["seq"] += 1
        del LOG[:-1500]
    try:
        with open(LOGF, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def status(msg):
    JOB["status"] = msg
    if JOB["t0"]:
        log(f"[{time.time() - JOB['t0']:6.1f}s] {msg}")


def prog(done, total):
    JOB["pct"] = int(100 * done / max(total, 1))


def resolve_version(prof):
    if prof["version"]:
        return prof["version"]
    if not MANIFEST:
        get_manifest()
    return MANIFEST["latest"]["release"]


def cur():
    return CFG["profiles"][CFG["selected"]]


SKINDIR = BASE / "skins"
SKINDIR.mkdir(parents=True, exist_ok=True)


def _skin_boot():
    old, act = BASE / "skin.png", CFG.get("skin_active", "")
    if old.exists() and not any(SKINDIR.glob("*.png")):
        shutil.copy(old, SKINDIR / "My skin.png")
        act = act or "My skin.png"
    if act and (SKINDIR / act).exists():
        CFG["skin_active"], CFG["skin"] = act, str(SKINDIR / act)
    elif CFG.get("skin") and not Path(CFG["skin"]).exists():
        CFG["skin"], CFG["skin_active"] = "", ""
    save_cfg(CFG)


_skin_boot()


def sync_skin(gamedir, skin, jar_id, hide=False):
    """Apply the saved skin to this instance on EVERY launch (a local resource pack that overrides the default skins)."""
    packs = gamedir / "resourcepacks"
    pack = packs / PACK_NAME
    if not skin or not Path(skin).exists():
        if pack.exists():
            pack.unlink()
            edit_options(gamedir, False)
        return
    packs.mkdir(parents=True, exist_ok=True)
    png = Path(skin).read_bytes()
    if hide:
        png = strip_overlay(png)
    pf = pack_format_for(jar_id)
    meta = {"pack": {"pack_format": pf, "min_format": 1, "max_format": pf + 60, "description": "White Shadow saved skin",
                     "supported_formats": {"min_inclusive": 1, "max_inclusive": pf + 60}}}
    base = "assets/minecraft/textures/entity/"
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pack.mcmeta", json.dumps(meta))
        for n in ("steve", "alex"):
            z.writestr(f"{base}{n}.png", png)
        for kind in ("wide", "slim"):
            for n in ("steve", "alex", "ari", "efe", "kai", "makena", "noor", "sunny", "zuri"):
                z.writestr(f"{base}player/{kind}/{n}.png", png)
    edit_options(gamedir, True)
    log(f"Saved skin applied ({Path(skin).stem}).")


def type_dir(ptype="Mods"):
    d = instance_dir(CFG["selected"]) / FOLDERS[ptype]
    d.mkdir(parents=True, exist_ok=True)
    return d


def mr_install(pid, mc, loader, dest, log, done=None, ptype="mod", vid=None, nodeps=False):
    done = done if done is not None else set()
    if pid in done:
        return
    done.add(pid)
    if vid:
        v = jget(f"{MR}/version/{vid}")
    else:
        params = {"game_versions": json.dumps([mc])}
        if loader != "vanilla" and ptype == "mod":
            params["loaders"] = json.dumps([loader])
        vers = jget(f"{MR}/project/{pid}/version?" + urllib.parse.urlencode(params))
        if not vers:
            raise RuntimeError("No compatible version for this Minecraft version/loader.")
        v = vers[0]
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    log(f"  + {f['filename']}")
    download(f["url"], dest / f["filename"], f["hashes"].get("sha1"))
    if nodeps:
        return
    for d in v.get("dependencies", []):
        if d.get("dependency_type") != "required":
            continue
        dep = d.get("project_id")
        if not dep and d.get("version_id"):
            try:
                dep = jget(f"{MR}/version/{d['version_id']}")["project_id"]
            except Exception:
                dep = None
        if dep:
            mr_install(dep, mc, loader, dest, log, done, ptype, d.get("version_id"))


def run_job(kind, fn, *args):
    with LOCK:
        if JOB["busy"] or JOB["running"]:
            raise RuntimeError("Another task is running. Wait for it to finish first.")
        JOB.update(busy=True, kind=kind, pct=0, crash=None, result=None, t0=time.time(), id=JOB["id"] + 1)

    def worker():
        try:
            fn(*args)
        except Exception as e:
            log(traceback.format_exc())
            msg = ("Network problem - check your internet connection." if isinstance(e, urllib.error.URLError)
                   else f"Unexpected data received: {e}" if isinstance(e, KeyError) else str(e))
            JOB["status"], JOB["result"] = "Error - see log", {"error": msg}
        finally:
            JOB["busy"] = False
    threading.Thread(target=worker, daemon=True).start()


def play_job(name, extra=None):
    prof = CFG["profiles"][name]
    mc = resolve_version(prof)
    if not (VERSIONS / mc / f"{mc}.jar").exists():
        need_space(1500, f"Minecraft {mc}")
    status(f"Installing Minecraft {mc}")
    install_vanilla(mc)
    vid = install_fabric(mc) if prof["loader"] == "fabric" else mc
    data = load_version(vid)
    cp, jobs, natives = collect_libs(data)
    status("Downloading libraries")
    run_jobs([j for j in jobs if not j[1].exists()], prog)
    ndir = NATIVES / vid
    if natives:
        extract_natives(natives, ndir)
    status("Downloading assets")
    ensure_assets(data, prog)
    status("Preparing Java")
    major = data.get("javaVersion", {}).get("majorVersion", 8)
    java = prof.get("java") or find_java(major, CFG)
    if java and not Path(java).is_file():
        raise RuntimeError(f"Java not found at: {java}. Fix the path in the profile or in Settings.")
    if not java:
        need_space(400, f"Java {major}")
        java = download_java(major, log)
    gamedir = instance_dir(name)
    (gamedir / "mods").mkdir(parents=True, exist_ok=True)
    sync_skin(gamedir, CFG["skin"], data["_jar"], CFG.get("skin_hide", False))
    cmd = build_cmd(java, data, vid, cp, gamedir, ndir, CFG["username"], prof["ram"],
                    (CFG["jvm_args"] + " " + prof.get("jvm", "")).strip())
    if CFG.get("fullscreen"):
        cmd.append("--fullscreen")
    elif CFG.get("width") and CFG.get("height"):
        cmd += ["--width", str(CFG["width"]), "--height", str(CFG["height"])]
    if extra:
        cmd += extra
    log(f"Launching {vid} as {CFG['username']} ...")
    status("Minecraft is running")
    JOB.update(pct=0, running=True, busy=False)
    tstart = time.time()
    prof["last_played"] = tstart
    try:
        proc = subprocess.Popen(cmd, cwd=gamedir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", creationflags=NOWIN)
        PROC[0] = proc
        for line in proc.stdout:
            log(line.rstrip())
        code = proc.wait()
    finally:
        JOB["running"], PROC[0] = False, None
    dur, day = time.time() - tstart, time.strftime("%Y-%m-%d")
    pd = CFG.setdefault("play_days", {})
    pd[day] = pd.get(day, 0) + dur
    if not CFG.get("no_playtime"):
        CFG["playtime"] = CFG.get("playtime", 0) + dur
        prof["playtime"] = prof.get("playtime", 0) + dur
    CFG["launches"] = CFG.get("launches", 0) + 1
    save_cfg(CFG)
    log(f"Game closed (exit code {code}).")
    if code != 0:
        JOB["crash"] = {"code": code, "findings": analyze_crash(list(LOG))}
        status(f"Game exited with code {code}")
    else:
        status("Ready")


def install_job(entries, ptype):
    prof = cur()
    mc, loader = resolve_version(prof), prof["loader"]
    need_space(80, "files")
    dest, key, ok, bad, files = type_dir(ptype), CFG["cf_key"], [], [], []

    def cap(m):
        log(m)
        if str(m).startswith("  + "):
            files.append(str(m)[4:])
    for i, (src, mid, name, vid, nod) in enumerate(entries):
        status(f"Installing {name}")
        try:
            if src == "Modrinth":
                mr_install(mid, mc, loader, dest, cap, ptype=PT[ptype], vid=vid, nodeps=nod)
            else:
                cf_install(mid, mc, loader, dest, key, cap, class_id=CF_CLASS[ptype])
            ok.append(name)
        except Exception as e:
            bad.append([name, str(e)])
            log(f"  x {name}: {e}")
        prog(i + 1, len(entries))
    if ptype == "Mods" and loader != "vanilla" and ok and not any(e[4] for e in entries):
        status("Resolving dependencies")
        for msg in fix_deps(dest, mc, loader, cap):
            bad.append([msg.split(":")[0], msg.split(": ", 1)[-1]])
    JOB["result"] = {"ok": ok, "bad": bad, "entries": [list(e[:3]) for e in entries], "ptype": ptype, "files": files}
    status(f"Installed {len(ok)}/{len(entries)}")


def deps_job():
    prof = cur()
    left = fix_deps(type_dir("Mods"), resolve_version(prof), prof["loader"], log)
    JOB["result"] = {"ok": [], "bad": [[x.split(":")[0], x.split(": ", 1)[-1]] for x in left], "entries": [], "ptype": "Mods"}
    status("Ready")


def import_job(path, name, prof, man):
    import_profile(path, instance_dir(name), log)
    CFG["profiles"][name] = prof
    CFG["selected"] = name
    save_cfg(CFG)
    JOB["result"] = {"ok": [name], "bad": [], "entries": [], "ptype": "Profile"}
    status("Ready")


def open_path(p):
    if SYS == "windows":
        os.startfile(str(p))
    elif SYS == "osx":
        subprocess.Popen(["open", str(p)], stdin=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", str(p)], stdin=subprocess.DEVNULL)


def img_uri(b):
    mime = "image/png" if b[:4] == b"\x89PNG" else "image/jpeg" if b[:2] == b"\xff\xd8" else "image/gif" if b[:3] == b"GIF" else "image/webp"
    return f"data:{mime};base64," + __import__("base64").b64encode(b).decode()


def J(b):
    return json.loads(b or b"{}")


# ---------------- API routes: (method, path) -> fn(query, body)
def r_boot(q, b):
    global CUR_LANG
    CUR_LANG = CFG.get("lang", "en") if CFG.get("lang") in TR else "en"
    if CFG["username"] not in CFG.setdefault("accounts", []):
        CFG["accounts"].insert(0, CFG["username"])
    tr = dict(TR["en"])
    tr.update(TR[CUR_LANG])
    return {"cfg": CFG, "t": tr, "langs": {c: LANG_NAMES[c] for c in LANG_CODES}, "ver": VER, "base": str(BASE), "free": free_mb(),
            "suggested": SUGGESTED, "cats": PACK_CATS, "news": NEWS, "sys": SYS}


def r_state(q, b):
    since = int(q.get("since", 0))
    with LOCK:
        n = JOB["seq"] - since
        lines = LOG[-n:] if 0 < n <= len(LOG) else (LOG[:] if n > 0 else [])
        JOB["last_ping"], JOB["pinged"] = time.time(), True
        out = {k: JOB[k] for k in ("busy", "running", "kind", "status", "pct", "crash", "result", "id", "seq")}
    out.update(lines=lines, free=free_mb())
    return out


def r_versions(q, b):
    if not MANIFEST:
        get_manifest()
    return ["latest"] + [v["id"] for v in MANIFEST["versions"] if q.get("snap") == "1" or v["type"] == "release"]


def r_play(q, b):
    name = J(b)["name"]
    if not USER_RE.fullmatch(CFG["username"]):
        raise ValueError("Set a valid username first (3-16 letters, numbers or _).")
    if name not in CFG["profiles"]:
        raise ValueError("Unknown profile.")
    CFG["selected"] = name
    save_cfg(CFG)
    run_job("play", play_job, name)


def r_stop(q, b):
    if PROC[0]:
        PROC[0].terminate()


def r_select(q, b):
    n = J(b)["name"]
    if n in CFG["profiles"]:
        CFG["selected"] = n
        save_cfg(CFG)


def r_profile(q, b):
    d = J(b)
    old, n = d.get("old"), (d.get("name") or "").strip()
    if not n:
        raise ValueError("Give the profile a name.")
    if n != old and n in CFG["profiles"]:
        raise ValueError("A profile with that name already exists.")
    ram = int(d.get("ram") or 0)
    if not 512 <= ram <= 65536:
        raise ValueError("RAM must be between 512 and 65536 MB.")
    jp = (d.get("java") or "").strip()
    if jp and not Path(jp).is_file():
        raise ValueError("That Java path does not exist.")
    prof = {"version": "" if d.get("version") in ("", "latest", None) else d["version"],
            "loader": d.get("loader") if d.get("loader") in ("vanilla", "fabric") else "vanilla", "ram": ram,
            "icon": d.get("icon") or "⛏", "java": jp, "jvm": (d.get("jvm") or "").strip()}
    if old and old != n and old in CFG["profiles"]:
        CFG["profiles"].pop(old)
        od, nd = instance_dir(old), instance_dir(n)
        if od.exists() and not nd.exists():
            od.rename(nd)
    CFG["profiles"][n] = prof
    CFG["selected"] = n
    save_cfg(CFG)
    return {"cfg": CFG}


def r_profile_del(q, b):
    n = J(b)["name"]
    if len(CFG["profiles"]) < 2:
        raise ValueError("You need at least one profile.")
    CFG["profiles"].pop(n, None)
    shutil.rmtree(instance_dir(n), ignore_errors=True)
    CFG["selected"] = next(iter(CFG["profiles"]))
    save_cfg(CFG)
    return {"cfg": CFG}


def r_profile_dup(q, b):
    n = J(b)["name"]
    c = n + " copy"
    CFG["profiles"][c] = dict(CFG["profiles"][n])
    if instance_dir(n).exists():
        shutil.copytree(instance_dir(n), instance_dir(c), dirs_exist_ok=True)
    save_cfg(CFG)
    return {"cfg": CFG}


def r_account(q, b):
    d = J(b)
    acc, name = CFG.setdefault("accounts", [CFG["username"]]), (d.get("name") or "").strip()
    if d["action"] in ("add", "use") and not USER_RE.fullmatch(name):
        raise ValueError("Username must be 3-16 characters: letters, numbers, underscore.")
    if d["action"] == "add" and name not in acc:
        acc.append(name)
    if d["action"] in ("add", "use"):
        CFG["username"] = name
    if d["action"] == "remove" and len(acc) > 1:
        acc.remove(name)
        if CFG["username"] == name:
            CFG["username"] = acc[0]
    save_cfg(CFG)
    return {"cfg": CFG}


def r_settings(q, b):
    d = J(b)
    w, h = int(d.get("width") or 0), int(d.get("height") or 0)
    if (w or h) and not (320 <= w <= 7680 and 240 <= h <= 4320):
        raise ValueError("Window size must be 320-7680 x 240-4320, or 0 x 0 for the Minecraft default.")
    wk, rt = int(d["workers"]), int(d["retries"])
    if not 1 <= wk <= 64:
        raise ValueError("Download threads must be 1-64.")
    if not 1 <= rt <= 10:
        raise ValueError("Retries must be 1-10.")
    jp = (d.get("java") or "").strip()
    if jp and not Path(jp).is_file():
        raise ValueError("The Java path does not exist.")
    for k in ("skip_confirm", "dbl_launch", "no_playtime", "reduce_motion", "notify"):
        CFG[k] = bool(d.get(k, NEWDEF[k]))
    CFG["accent"] = d.get("accent") if d.get("accent") in ("violet", "blue", "rose", "teal") else "violet"
    dr = int(d.get("default_ram") or 4096)
    if not 1024 <= dr <= 65536:
        raise ValueError("Default RAM must be between 1024 and 65536 MB.")
    CFG["default_ram"] = dr
    CFG["default_loader"] = d.get("default_loader") if d.get("default_loader") in ("vanilla", "fabric") else "fabric"
    CFG.update(java=jp, jvm_args=(d.get("jvm_args") or "").strip(), cf_key=(d.get("cf_key") or "").strip(), width=w, height=h,
               fullscreen=bool(d.get("fullscreen")), workers=wk, retries=rt, lang=d.get("lang") if d.get("lang") in TR else "en")
    DL.update(workers=wk, retries=rt)
    save_cfg(CFG)
    return r_boot(q, b)


def r_reset(q, b):
    for k in ("java", "jvm_args", "width", "height", "fullscreen", "workers", "retries"):
        CFG[k] = DEFAULT_CFG[k]
    CFG.update(NEWDEF)
    DL.update(workers=CFG["workers"], retries=CFG["retries"])
    save_cfg(CFG)
    return r_boot(q, b)


SHA_CACHE, HASH_PID = {}, {}


def installed_pids(ptype):
    """Modrinth project ids of everything already installed (matched by file hash, cached)."""
    d = type_dir(ptype)
    hashes = {}
    for f in d.iterdir():
        if f.is_file():
            key = (str(f), f.stat().st_mtime, f.stat().st_size)
            if key not in SHA_CACHE:
                SHA_CACHE[key] = sha1_of(f)
            hashes[SHA_CACHE[key]] = f
    new = [h for h in hashes if h not in HASH_PID]
    if new:
        try:
            for h, v in jpost(MR + "/version_files", {"hashes": new, "algorithm": "sha1"}).items():
                HASH_PID[h] = v["project_id"]
        except Exception:
            return set()
        for h in new:
            HASH_PID.setdefault(h, None)
    return {HASH_PID[h] for h in hashes if HASH_PID.get(h)}


def r_search(q, b):
    prof, ptype, src = cur(), q.get("type", "Mods"), q.get("src", "Modrinth")
    mc, loader = resolve_version(prof), prof["loader"]
    cats = [c for c in q.get("cats", "").split(",") if c]
    if src == "Modrinth":
        facets = [[f"project_type:{PT[ptype]}"], [f"versions:{mc}"]]
        if loader != "vanilla" and ptype == "Mods":
            facets.append([f"categories:{loader}"])
        facets += [[f"categories:{c}"] for c in cats]
        url = MR + "/search?" + urllib.parse.urlencode({"query": q.get("q", ""), "limit": 24, "offset": int(q.get("off", 0)),
                                                         "index": MR_SORT.get(q.get("sort"), "relevance"), "facets": json.dumps(facets)})
        d, have = jget(url), installed_pids(ptype)
        return {"total": d["total_hits"], "items": [
            {"id": h["project_id"], "name": h["title"], "desc": h["description"], "downloads": h["downloads"], "icon": h.get("icon_url"),
             "author": h.get("author", ""), "banner": h.get("featured_gallery") or next(iter(h.get("gallery") or []), None),
             "installed": h["project_id"] in have} for h in d["hits"]]}
    if not CFG["cf_key"]:
        raise ValueError("Add your free CurseForge API key in Settings > Downloads first.")
    items = cf_search(q.get("q", ""), mc, loader, CFG["cf_key"], CF_CLASS[ptype])
    for it in items:
        it.update(banner=None, installed=False)
    return {"items": items, "total": len(items)}


def r_install(q, b):
    d = J(b)
    ptype = d.get("type", "Mods")
    entries = [(e["src"], e["id"], e["name"], e.get("vid"), bool(e.get("nodeps"))) for e in d["entries"]]
    prof = cur()
    if ptype == "Mods" and prof["loader"] == "vanilla":
        if not d.get("switch"):
            return {"needs_loader": True}
        prof["loader"] = "fabric"
        save_cfg(CFG)
    run_job("install", install_job, entries, ptype)
    return {"started": True, "cfg": CFG}


def r_installed(q, b):
    ptype, d = q.get("type", "Mods"), type_dir(q.get("type", "Mods"))
    if ptype == "Mods":
        metas, warns = analyze_mods(d)
        return {"items": [{"file": m["file"], "name": m["name"], "sub": f"{m['version']} • {m['authors'] or 'unknown author'}",
                           "desc": m["desc"], "on": m["enabled"], "icon": img_uri(m["icon"]) if m["icon"] else "",
                           "ver": m["version"], "author": m["authors"]} for m in metas],
                "warns": warns}
    items = [{"file": f.name, "name": f.name.replace(".disabled", ""), "sub": f"{f.stat().st_size / 2**20:.1f} MB", "desc": "",
              "on": f.suffix == ".zip", "icon": "", "ver": "", "author": ""} for f in sorted(d.iterdir()) if f.is_file() and (f.suffix == ".zip" or f.name.endswith(".zip.disabled"))]
    return {"items": items, "warns": []}


def r_toggle(q, b):
    d = J(b)
    p = type_dir(d["type"]) / Path(d["file"]).name
    if p.exists():
        p.rename(p.with_name(p.name[:-9] if p.name.endswith(".disabled") else p.name + ".disabled"))


def r_remove(q, b):
    d = J(b)
    for f in d["files"]:
        (type_dir(d["type"]) / Path(f).name).unlink(missing_ok=True)


def r_addfile(q, b):
    name = Path(q["name"]).name
    if not name.lower().endswith((".jar", ".zip")):
        raise ValueError("Only .jar and .zip files can be added.")
    (type_dir(q.get("type", "Mods")) / name).write_bytes(b)


def r_fixdeps(q, b):
    run_job("fix", deps_job)


def r_open(q, b):
    d = J(b)
    sub = {"root": "", "worlds": "saves", "shots": "screenshots"}.get(d.get("what"))
    if sub is None:
        return open_path(type_dir(d.get("type", "Mods")))
    p = instance_dir(CFG["selected"]) / sub
    p.mkdir(parents=True, exist_ok=True)
    open_path(p)


def r_fix(q, b):
    d = J(b)
    kind, arg, prof = d["kind"], d.get("arg"), cur()
    if kind == "java":
        prof["java"] = CFG["java"] = ""
        (BASE / "java_cache.json").unlink(missing_ok=True)
    elif kind == "ram":
        prof["ram"] = min(65536, max(1024, prof["ram"] + int(arg)))
    elif kind == "deps":
        run_job("fix", deps_job)
        return {"msg": "Installing missing dependencies..."}
    elif kind == "disable":
        for m in analyze_mods(type_dir("Mods"))[0]:
            if m["enabled"] and (m["id"] == arg or str(arg).lower() in (m["name"] or "").lower()):
                (type_dir("Mods") / m["file"]).rename(type_dir("Mods") / (m["file"] + ".disabled"))
    save_cfg(CFG)
    JOB["crash"] = None
    return {"msg": "Done - press PLAY to try again.", "cfg": CFG}


def _skin_path(name):
    p = SKINDIR / (Path(str(name)).name + ".png")
    if not p.is_file() or p.parent != SKINDIR:
        raise ValueError("That skin is not in your library.")
    return p


def _safe_skin(name):
    return (re.sub(r"[^A-Za-z0-9_ .\-]", "_", name).strip()[:40] or "My skin")


def _activate(p):
    CFG["skin_active"], CFG["skin"] = p.name, str(p)
    save_cfg(CFG)


def r_skins(q, b):
    return [{"name": f.stem, "active": f.name == CFG.get("skin_active")} for f in sorted(SKINDIR.glob("*.png"))]


def r_skin_file(q, b):
    return _skin_path(q["name"]).read_bytes(), "image/png", {"Cache-Control": "no-store"}


def r_skin_png(q, b):
    p = Path(CFG.get("skin") or "")
    if not p.is_file():
        raise ValueError("no skin")
    return p.read_bytes(), "image/png", {"Cache-Control": "no-store"}


def r_skin_up(q, b):
    tmp = BASE / "skin_upload.png"
    tmp.write_bytes(b)
    dest = SKINDIR / (_safe_skin(q.get("name", "My skin")) + ".png")
    try:
        note = normalize_skin(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    _activate(dest)
    return {"note": note, "cfg": CFG}


def r_skin_use(q, b):
    _activate(_skin_path(J(b)["name"]))
    return {"cfg": CFG}


def r_skin_del(q, b):
    p = _skin_path(J(b)["name"])
    p.unlink()
    if CFG.get("skin_active") == p.name:
        CFG["skin_active"] = CFG["skin"] = ""
        save_cfg(CFG)
    return {"cfg": CFG}


def r_skin_import(q, b):
    name = (J(b).get("name") or "").strip()
    if not USER_RE.fullmatch(name):
        raise ValueError("Enter a Minecraft username (3-16 letters, numbers or _).")
    data = http(f"https://minotar.net/skin/{urllib.parse.quote(name)}", timeout=20)
    tmp, dest = BASE / "skin_import.png", SKINDIR / (name + ".png")
    tmp.write_bytes(data)
    try:
        normalize_skin(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    _activate(dest)
    return {"cfg": CFG}


def r_skin_clear(q, b):
    CFG["skin"] = CFG["skin_active"] = ""
    save_cfg(CFG)
    return {"cfg": CFG}


def r_skin_hide(q, b):
    CFG["skin_hide"] = bool(J(b)["on"])
    save_cfg(CFG)


def r_modinfo(q, b):
    """Compatible versions of one project plus the dependencies each version needs (like Modrinth's download panel)."""
    prof, ptype = cur(), q.get("type", "Mods")
    mc, loader = resolve_version(prof), prof["loader"]
    params = {"game_versions": json.dumps([mc])}
    if loader != "vanilla" and ptype == "Mods":
        params["loaders"] = json.dumps([loader])
    vers = jget(f"{MR}/project/{urllib.parse.quote(q['id'])}/version?" + urllib.parse.urlencode(params))[:12]
    ids = {d["project_id"] for v in vers for d in v.get("dependencies", []) if d.get("project_id")}
    names = {}
    if ids:
        for p in jget(MR + "/projects?ids=" + urllib.parse.quote(json.dumps(sorted(ids)))):
            names[p["id"]] = p
    have = installed_pids("Mods" if ptype == "Mods" else ptype)
    out = []
    for v in vers:
        f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
        out.append({"id": v["id"], "number": v["version_number"], "type": v["version_type"], "date": v["date_published"], "size": f["size"],
                    "deps": [{"pid": d.get("project_id"), "kind": d["dependency_type"], "vid": d.get("version_id"),
                              "title": (names.get(d.get("project_id")) or {}).get("title", "Unknown project"),
                              "icon": (names.get(d.get("project_id")) or {}).get("icon_url"), "installed": d.get("project_id") in have}
                             for d in v.get("dependencies", []) if d["dependency_type"] in ("required", "optional")]})
    return {"versions": out, "mc": mc, "loader": loader}


def r_export(q, b):
    n = J(b)["name"]
    dl = Path.home() / "Downloads"
    dl.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_. -]", "_", n)
    dest, i = dl / f"{stem}.wsprof", 2
    while dest.exists():
        dest, i = dl / f"{stem} ({i}).wsprof", i + 1
    export_profile(n, CFG["profiles"][n], dest)
    return {"path": str(dest)}


def r_import_check(q, b):
    tmp = BASE / f"import_{secrets.token_hex(4)}.wsprof"
    tmp.write_bytes(b)
    try:
        with zipfile.ZipFile(tmp) as z:
            man = json.loads(z.read("wsprofile.json"))
            bundled = sum(1 for x in z.namelist() if x.startswith("local_mods/"))
        man["profile"]
    except Exception:
        tmp.unlink(missing_ok=True)
        raise ValueError("That is not a valid .wsprof file.")
    IMPORTS[tmp.name] = (tmp, man)
    return {"id": tmp.name, "name": man.get("name", "Imported"), "mods": len(man.get("mods", [])), "bundled": bundled}


def r_import_run(q, b):
    tmp, man = IMPORTS.pop(J(b)["id"])
    p, base = man["profile"], man.get("name", "Imported")
    name, k = base, 2
    while name in CFG["profiles"]:
        name, k = f"{base} ({k})", k + 1
    prof = {"version": p.get("version") or "", "loader": p.get("loader") if p.get("loader") in ("vanilla", "fabric") else "vanilla",
            "ram": min(65536, max(512, int(p.get("ram") or 4096))), "icon": p.get("icon") or "⛏", "java": "", "jvm": ""}
    run_job("import", import_job, tmp, name, prof, man)


def r_clean(q, b):
    keep = {resolve_version(p) for p in CFG["profiles"].values()}
    used = {d.name for d in VERSIONS.iterdir() if d.name in keep or (d.name.startswith("fabric-loader-") and any(d.name.endswith("-" + k) for k in keep))}
    return {"freed": clean_space(used)}


def r_datadir(q, b):
    new = Path(J(b)["path"].strip()) / "WhiteShadow"
    new.mkdir(parents=True, exist_ok=True)
    for fn in ("config.json", "skin.png"):
        if (BASE / fn).exists():
            shutil.copy(BASE / fn, new / fn)
    POINTER.write_text(str(new))
    return {"msg": f"Data folder set to {new}. Restart the launcher to use it."}


def r_logfile(q, b):
    open_path(LOGF if LOGF.exists() else BASE)


UPDATE_REPO = ""   # set to "owner/repo" (the release workflow does this automatically) so the app can announce new versions


def r_update(q, b):
    if not UPDATE_REPO:
        return {"new": None}
    try:
        d = jget(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest")
    except Exception:
        return {"new": None}
    tag = str(d.get("tag_name", "")).lstrip("vV")

    def key(s):
        return tuple(int(x) for x in re.findall(r"\d+", s)[:3])
    return {"new": tag if tag and key(tag) > key(VER) else None, "url": d.get("html_url", "")}


def r_openurl(q, b):
    url = J(b).get("url", "")
    if not (UPDATE_REPO and url.startswith(f"https://github.com/{UPDATE_REPO}/")):
        raise ValueError("Blocked.")
    webbrowser.open(url)


def r_ping(q, b):
    JOB["last_ping"], JOB["pinged"] = time.time(), True



def _cnt(gd, sub, exts):
    d = gd / sub
    return sum(1 for f in d.iterdir() if f.is_file() and f.name.endswith(exts)) if d.exists() else 0


def r_inst(q, b):
    n = q["name"]
    gd = instance_dir(n)
    return {"name": n, "profile": CFG["profiles"][n], "counts": {
        "Mods": _cnt(gd, "mods", (".jar", ".jar.disabled")), "Resource Packs": _cnt(gd, "resourcepacks", (".zip", ".zip.disabled")),
        "Shaders": _cnt(gd, "shaderpacks", (".zip", ".zip.disabled"))}}


def r_worlds(q, b):
    sd = instance_dir(CFG["selected"]) / "saves"
    out = []
    for w_ in (sorted(sd.iterdir()) if sd.exists() else []):
        if w_.is_dir():
            out.append({"name": w_.name, "size": dir_size(w_), "mtime": w_.stat().st_mtime})
    return sorted(out, key=lambda x: -x["mtime"])


def r_world_del(q, b):
    sd = instance_dir(CFG["selected"]) / "saves"
    p = sd / Path(J(b)["name"]).name
    if p.is_dir() and p.parent == sd:
        shutil.rmtree(p)


def r_shots(q, b):
    sd = instance_dir(CFG["selected"]) / "screenshots"
    fs = [f for f in (sd.iterdir() if sd.exists() else []) if f.suffix.lower() in (".png", ".jpg", ".jpeg")]
    return [{"file": f.name, "mtime": f.stat().st_mtime} for f in sorted(fs, key=lambda f: -f.stat().st_mtime)[:150]]


def r_shot(q, b):
    sd = instance_dir(CFG["selected"]) / "screenshots"
    p = sd / Path(q["file"]).name
    if not p.is_file() or p.parent != sd:
        raise ValueError("not found")
    return p.read_bytes(), "image/png" if p.suffix.lower() == ".png" else "image/jpeg", {"Cache-Control": "max-age=300"}


def r_files(q, b):
    gd = instance_dir(CFG["selected"])
    return [{"name": f.name, "dir": f.is_dir(), "size": f.stat().st_size if f.is_file() else 0}
            for f in (sorted(gd.iterdir()) if gd.exists() else [])]


def r_javas(q, b):
    out, seen = [], set()
    for p in java_candidates():
        if str(p) not in seen and p.exists():
            seen.add(str(p))
            v = java_major(p)
            if v:
                out.append({"path": str(p), "major": v})
    return {"javas": out, "current": CFG["java"]}


def java_job(major):
    need_space(400, f"Java {major}")
    download_java(major, log)
    JOB["result"] = {"ok": [f"Java {major}"], "bad": [], "entries": [], "ptype": "Java"}
    status("Ready")


def r_java_dl(q, b):
    run_job("java", java_job, int(J(b)["major"]))


def r_storage(q, b):
    return {"base": str(BASE), "free": free_mb(), "parts": {k: dir_size(p) for k, p in (
        ("Game versions", VERSIONS), ("Libraries", LIBS), ("Assets", ASSETS), ("Instances", INSTANCES), ("Java runtimes", RUNTIME))}}


def r_logclear(q, b):
    LOGF.write_text("")


def _vi_read(s):
    n = 0
    for i in range(5):
        c = s.recv(1)
        if not c:
            raise IOError("closed")
        n |= (c[0] & 0x7F) << (7 * i)
        if not c[0] & 0x80:
            return n
    raise IOError("bad varint")


def _vi(n):
    out = b""
    while True:
        c = n & 0x7F
        n >>= 7
        out += bytes([c | (0x80 if n else 0)])
        if not n:
            return out


def _motd(m):
    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        return m.get("text", "") + "".join(_motd(e) for e in m.get("extra", []))
    return "".join(_motd(e) for e in m) if isinstance(m, list) else ""


def ping_server(address, timeout=3):
    import socket
    host, _, port = address.partition(":")
    port = int(port or 25565)
    t0 = time.time()
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        hb = host.encode()
        data = _vi(0) + _vi(765) + _vi(len(hb)) + hb + port.to_bytes(2, "big") + _vi(1)
        s.sendall(_vi(len(data)) + data + b"\x01\x00")
        total = _vi_read(s)
        if not 0 < total < 2 ** 21:
            raise IOError("bad reply")
        buf = b""
        while len(buf) < total:
            c = s.recv(min(65536, total - len(buf)))
            if not c:
                raise IOError("closed")
            buf += c
    ms = int((time.time() - t0) * 1000)
    i = 0
    while buf[i] & 0x80:
        i += 1
    i += 1                      # packet id
    n, sh = 0, 0
    while True:
        c = buf[i]
        i += 1
        n |= (c & 0x7F) << sh
        sh += 7
        if not c & 0x80:
            break
    d = json.loads(buf[i:i + n].decode("utf-8", "replace"))
    pl = d.get("players", {})
    return {"ok": True, "online": pl.get("online", 0), "max": pl.get("max", 0), "ms": ms,
            "version": (d.get("version") or {}).get("name", ""), "motd": re.sub(r"§.", "", _motd(d.get("description", ""))).strip(),
            "icon": d.get("favicon", "") if str(d.get("favicon", "")).startswith("data:image/png") else ""}


def r_servers(q, b):
    from concurrent.futures import ThreadPoolExecutor

    def one(s):
        try:
            return {**s, **ping_server(s["address"])}
        except Exception as e:
            return {**s, "ok": False, "error": str(e) or e.__class__.__name__}
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, CFG.get("servers", [])))


def r_server_add(q, b):
    d = J(b)
    addr = (d.get("address") or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.\-]+(:\d{1,5})?", addr):
        raise ValueError("Enter a server address like play.example.com or 1.2.3.4:25565")
    CFG.setdefault("servers", []).append({"name": (d.get("name") or addr).strip()[:40], "address": addr})
    save_cfg(CFG)


def r_server_rm(q, b):
    CFG["servers"] = [s for s in CFG.get("servers", []) if s["address"] != J(b)["address"]]
    save_cfg(CFG)


def r_server_join(q, b):
    addr = J(b)["address"]
    mc = resolve_version(cur())
    m = re.match(r"1\.(\d+)", mc)
    if m and int(m.group(1)) < 20:
        h, _, p = addr.partition(":")
        extra = ["--server", h, "--port", p or "25565"]
    else:
        extra = ["--quickPlayMultiplayer", addr]
    run_job("play", play_job, CFG["selected"], extra)


SUG_ICONS = {}


def r_sugicons(q, b):
    if not SUG_ICONS:
        ids = urllib.parse.quote(json.dumps([x[0] for x in SUGGESTED]))
        SUG_ICONS.update({p["slug"]: p.get("icon_url") for p in jget(MR + "/projects?ids=" + ids)})
    return SUG_ICONS


ROUTES = {("GET", "sugicons"): r_sugicons, ("GET", "boot"): r_boot, ("GET", "state"): r_state, ("GET", "versions"): r_versions, ("POST", "play"): r_play,
          ("POST", "stop"): r_stop, ("POST", "select"): r_select, ("POST", "profile"): r_profile, ("POST", "profile/delete"): r_profile_del,
          ("POST", "profile/dup"): r_profile_dup, ("POST", "account"): r_account, ("POST", "settings"): r_settings,
          ("POST", "settings/reset"): r_reset, ("GET", "search"): r_search, ("POST", "install"): r_install,
          ("GET", "installed"): r_installed, ("POST", "toggle"): r_toggle, ("POST", "remove"): r_remove,
          ("POST", "addfile"): r_addfile, ("POST", "fixdeps"): r_fixdeps, ("POST", "open"): r_open, ("POST", "fix"): r_fix,
          ("GET", "skin.png"): r_skin_png, ("POST", "skin"): r_skin_up, ("POST", "skin/clear"): r_skin_clear,
          ("POST", "skin/hide"): r_skin_hide, ("POST", "export"): r_export, ("POST", "import/check"): r_import_check,
          ("POST", "import/run"): r_import_run, ("POST", "clean"): r_clean, ("POST", "datadir"): r_datadir,
          ("POST", "logfile"): r_logfile, ("GET", "update"): r_update, ("POST", "openurl"): r_openurl, ("GET", "skins"): r_skins, ("GET", "skinfile"): r_skin_file, ("POST", "skin/use"): r_skin_use,
          ("POST", "skin/delete"): r_skin_del, ("POST", "skin/import"): r_skin_import, ("GET", "modinfo"): r_modinfo, ("GET", "inst"): r_inst, ("GET", "worlds"): r_worlds,
          ("POST", "world/delete"): r_world_del, ("GET", "shots"): r_shots, ("GET", "shot"): r_shot, ("GET", "files"): r_files,
          ("GET", "javas"): r_javas, ("POST", "java/download"): r_java_dl, ("GET", "storage"): r_storage,
          ("GET", "servers"): r_servers, ("POST", "server/add"): r_server_add, ("POST", "server/remove"): r_server_rm,
          ("POST", "server/join"): r_server_join, ("POST", "logclear"): r_logclear, ("POST", "ping"): r_ping}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _route(self, method):
        if (self.headers.get("Host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._json({"error": "bad host"}, 403)
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if method == "GET" and u.path == "/":
            csp = ("default-src 'self'; img-src * data: blob:; style-src 'self' 'unsafe-inline'; "
                   "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            return self._send(200, HTML.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8",
                              {"Cache-Control": "no-store", "Content-Security-Policy": csp})
        tok = self.headers.get("X-WS-Token") or q.get("t", "")
        if not u.path.startswith("/api/") or not secrets.compare_digest(tok, TOKEN):
            return self._json({"error": "forbidden"}, 403)
        fn = ROUTES.get((method, u.path[5:]))
        if not fn:
            return self._json({"error": "not found"}, 404)
        try:
            body = b""
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                if n > 40 * 2 ** 20:
                    raise ValueError("File too large.")
                body = self.rfile.read(n)
            out = fn(q, body)
            if isinstance(out, tuple):
                return self._send(200, out[0], out[1], out[2] if len(out) > 2 else None)
            self._json(out if out is not None else {"ok": True})
        except Exception as e:
            self._json({"error": str(e) or e.__class__.__name__}, 400)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def open_window(url):
    """Open the UI in a chromeless app window (Edge/Chrome/Brave/Chromium), else the default browser."""
    exes = []
    if SYS == "windows":
        for env, rel in (("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"), ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe"),
                         ("ProgramFiles", r"Google\Chrome\Application\chrome.exe"), ("LocalAppData", r"Google\Chrome\Application\chrome.exe"),
                         ("ProgramFiles", r"BraveSoftware\Brave-Browser\Application\brave.exe")):
            exes.append(os.path.join(os.environ.get(env, ""), rel))
    elif SYS == "osx":
        exes = [f"/Applications/{a}.app/Contents/MacOS/{a}" for a in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium")]
    else:
        exes = [shutil.which(n) or "" for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser")]
    for exe in exes:
        if exe and os.path.isfile(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1320,840", "--no-first-run", "--disable-features=Translate",
                                  f"--user-data-dir={BASE / 'webprofile'}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=NOWIN)
                return
            except OSError:
                continue
    webbrowser.open(url)


def bg_boot():
    try:
        get_manifest()
    except Exception as e:
        log(f"Could not load version list (offline?): {e}")
    try:
        d = jget("https://launchercontent.mojang.com/v2/javaPatchNotes.json")
        NEWS[:] = [f"{e.get('title', '')}  -  {str(e.get('date', ''))[:10]}" for e in d.get("entries", [])[:6]]
    except Exception:
        NEWS[:] = ["News unavailable (offline?)"]


WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_installed():
    """True when the Microsoft Edge WebView2 runtime (the engine behind the app window on Windows) is present."""
    if SYS != "windows":
        return True
    import winreg
    for root, sub in ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
                      (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
                      (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}")):
        try:
            with winreg.OpenKey(root, sub) as k:
                if winreg.QueryValueEx(k, "pv")[0] not in ("", "0.0.0.0"):
                    return True
        except OSError:
            pass
    return False


def install_webview2():
    """One-time silent install of the WebView2 runtime from Microsoft (already built into Windows 11 and updated Windows 10)."""
    exe = BASE / "MicrosoftEdgeWebview2Setup.exe"
    exe.write_bytes(http("https://go.microsoft.com/fwlink/p/?LinkId=2124703", timeout=180))
    subprocess.run([str(exe), "/silent", "/install"], timeout=600, creationflags=NOWIN, stdin=subprocess.DEVNULL)
    exe.unlink(missing_ok=True)


def run_native(url):
    """Show the launcher in its own native app window (no browser, no address bar). Blocks until the window is closed.
    Returns False when a native window isn't possible, so the caller can fall back."""
    if os.environ.get("WS_BROWSER") == "1":
        return False
    try:
        import webview
    except Exception as e:
        log(f"Native window not available ({e}); using a browser app window instead.")
        return False
    t0 = time.time()
    try:
        if SYS == "windows" and not webview2_installed():
            log("Installing the Microsoft WebView2 runtime (one-time)...")
            install_webview2()
            if not webview2_installed():
                return False
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:
            pass
        webview.create_window(APP, url, width=1360, height=880, min_size=(980, 640), background_color="#07050d")
        webview.start(gui="edgechromium" if SYS == "windows" else None)
        return True
    except Exception as e:
        if time.time() - t0 > 5:      # the window was up and the user used it - this is just the shutdown
            return True
        log(f"Native window failed ({e}); using a browser app window instead.")
        return False


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=bg_boot, daemon=True).start()
    log(f"{APP} {VER} started")
    if run_native(url):
        while JOB["busy"] or JOB["running"]:       # never abandon a running game or download
            time.sleep(1)
    else:
        if sys.stdout:
            print(f"{APP} Launcher is running at {url}  (close the window to quit)")
        try:
            open_window(url)
        except Exception as e:
            log(f"Could not open a window automatically ({e}). Open {url} in your browser.")
        t0 = time.time()
        while True:  # browser fallback: quit shortly after the window closes, but never while a game/task is running
            time.sleep(1.5)
            with LOCK:
                idle, seen, busy = time.time() - JOB["last_ping"], JOB["pinged"], JOB["busy"] or JOB["running"]
            if (seen and idle > 20 and not busy) or (not seen and time.time() - t0 > 180):
                break
    srv.shutdown()


if __name__ == "__main__":
    main()
