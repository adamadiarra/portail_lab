#!/usr/bin/env python3
"""
Portail de labs : les étudiants voient la liste des labs, s'inscrivent à UN SEUL lab à la fois,
puis accèdent à une page « Mon lab » avec leurs informations de connexion.

    python portail_labs.py                       # http://127.0.0.1:8000
    python portail_labs.py --host 0.0.0.0 --port 8000

Variables d'environnement : ADMIN_USER (admin), ADMIN_PASSWORD (sinon généré et affiché au 1er démarrage),
INVITE_CODE (code d'inscription initial), DB_PATH (portail.db), COOKIE_SECURE=1 (derrière HTTPS).
Aucune dépendance : bibliothèque standard uniquement.
"""
import argparse, hashlib, hmac, http.client, json, os, re, secrets, socket, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DB_PATH = os.environ.get("DB_PATH", "portail.db")
SESSION_TTL = 7 * 24 * 3600
USER_RE = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
PUBLISH_ADDR = os.environ.get("PUBLISH_ADDR", "0.0.0.0")   # interface sur laquelle les ports des labs sont publiés
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:@-]{0,200}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE,
  display_name TEXT NOT NULL, pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'student', created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS labs(id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
  instructions TEXT NOT NULL DEFAULT '', capacity INTEGER NOT NULL DEFAULT 20, access_url TEXT NOT NULL DEFAULT '',
  host TEXT NOT NULL DEFAULT '', port_base INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
  image TEXT NOT NULL DEFAULT '', ports TEXT NOT NULL DEFAULT '', env TEXT NOT NULL DEFAULT '', memory_mb INTEGER NOT NULL DEFAULT 256, cpus REAL NOT NULL DEFAULT 0.5);
CREATE TABLE IF NOT EXISTS enrollments(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
  lab_id INTEGER NOT NULL REFERENCES labs(id) ON DELETE CASCADE, slot INTEGER NOT NULL, created REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'none', detail TEXT NOT NULL DEFAULT '',
  UNIQUE(lab_id, slot));
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# ---------------------------------------------------------------- Docker (API Engine via socket Unix) --
class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path, timeout=60):
        super().__init__("localhost", timeout=timeout); self._p = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.sock.settimeout(self.timeout); self.sock.connect(self._p)

def docker(method, path, body=None, timeout=60):
    conn = _UnixConn(DOCKER_SOCK, timeout)
    try:
        data = None if body is None else json.dumps(body)
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        r = conn.getresponse(); raw = r.read().decode("utf-8", "replace")
        try: obj = json.loads(raw) if raw else {}
        except ValueError: obj = {"message": raw}
        return r.status, obj
    finally:
        conn.close()

def docker_ok():
    try: return docker("GET", "/_ping", timeout=3)[0] == 200
    except Exception: return False

def cname(lab_id, user_id): return f"lab-{lab_id}-u{user_id}"

def parse_ports(spec):
    """'web:8080,modbus:5020' -> [('web', 8080), ('modbus', 5020)]"""
    out = []
    for part in [x.strip() for x in (spec or "").split(",") if x.strip()]:
        n, _, cp = part.partition(":")
        if not NAME_RE.match(n) or not cp.isdigit() or not 1 <= int(cp) <= 65535: raise ValueError(f"Port invalide : « {part} » (format nom:port, ex. web:8080)")
        out.append((n, int(cp)))
    if len({n for n, _ in out}) != len(out): raise ValueError("Noms de ports en double")
    return out

def host_ports(lab, slot):
    """Port hôte de chaque port du lab : port_base + 1000 × rang + n° de place.
    Lab sans conteneur (accès manuel) : un seul port « anonyme » = port_base + place."""
    if lab["ports"]:
        return {n: lab["port_base"] + i * 1000 + slot for i, (n, _) in enumerate(parse_ports(lab["ports"]))}
    return {"": lab["port_base"] + slot} if lab["port_base"] else {}

def remove_container(name):
    try: docker("DELETE", f"/containers/{name}?force=true&v=true", timeout=30)
    except Exception as e: print("[portail] suppression conteneur :", name, repr(e), flush=True)

def _set_status(eid, status, detail=""):
    c = db()
    try: c.execute("UPDATE enrollments SET status=?, detail=? WHERE id=?", (status, detail[:300], eid))
    finally: c.close()

def start_lab_container(eid):
    """Crée et démarre le conteneur de l'étudiant (appelé dans un thread)."""
    c = db()
    try:
        e = c.execute("SELECT * FROM enrollments WHERE id=?", (eid,)).fetchone()
        if not e: return
        lab = c.execute("SELECT * FROM labs WHERE id=?", (e["lab_id"],)).fetchone()
        user = c.execute("SELECT username FROM users WHERE id=?", (e["user_id"],)).fetchone()["username"]
    finally:
        c.close()
    try:
        _set_status(eid, "starting")
        name, ports, hp = cname(e["lab_id"], e["user_id"]), parse_ports(lab["ports"]), host_ports(lab, e["slot"])
        env = [render(l.strip(), user, e["slot"], hp) for l in (lab["env"] or "").splitlines() if "=" in l and not l.strip().startswith("#")]
        env += [f"LAB_USER={user}", f"LAB_SLOT={e['slot']}"]
        body = {"Image": lab["image"], "Env": env, "Labels": {"portail": "1", "portail.lab": str(lab["id"]), "portail.user": str(e["user_id"])},
                "ExposedPorts": {f"{cp}/tcp": {} for _, cp in ports},
                "HostConfig": {"PortBindings": {f"{cp}/tcp": [{"HostIp": PUBLISH_ADDR, "HostPort": str(hp[n])}] for n, cp in ports},
                               "Memory": int(lab["memory_mb"]) * 1024 * 1024, "NanoCpus": int(float(lab["cpus"]) * 1e9), "PidsLimit": 256,
                               "RestartPolicy": {"Name": "unless-stopped"}, "SecurityOpt": ["no-new-privileges:true"]}}
        remove_container(name)
        st, r = docker("POST", f"/containers/create?name={name}", body, timeout=60)
        if st == 404:  # image absente : téléchargement puis nouvel essai
            img, _, tag = lab["image"].rpartition(":") if ":" in lab["image"].split("/")[-1] else (lab["image"], "", "latest")
            ps, pr = docker("POST", f"/images/create?fromImage={img}&tag={tag}", timeout=600)
            if ps != 200: raise RuntimeError(f"Image introuvable : {lab['image']}")
            st, r = docker("POST", f"/containers/create?name={name}", body, timeout=60)
        if st != 201: raise RuntimeError(r.get("message", f"création refusée ({st})"))
        st, r = docker("POST", f"/containers/{name}/start", timeout=60)
        if st not in (204, 304): raise RuntimeError(r.get("message", f"démarrage refusé ({st})"))
        _set_status(eid, "running")
    except Exception as ex:
        print("[portail] échec démarrage lab :", repr(ex), flush=True)
        _set_status(eid, "error", str(ex))

def bg(fn, *a): threading.Thread(target=fn, args=a, daemon=True).start()

def drop_containers(rows):
    """rows : lignes (lab_id, user_id). Supprime les conteneurs en arrière-plan."""
    names = [cname(r["lab_id"], r["user_id"]) for r in rows]
    if names: bg(lambda: [remove_container(n) for n in names])

def reconcile():
    """Au démarrage : supprime les conteneurs orphelins, marque les labs dont le conteneur a disparu."""
    if not docker_ok(): return
    c = db()
    try:
        wanted = {cname(r["lab_id"], r["user_id"]): r["id"] for r in c.execute("SELECT id,lab_id,user_id FROM enrollments")}
        st, lst = docker("GET", '/containers/json?all=true&filters=' + json.dumps({"label": ["portail=1"]}).replace(" ", ""))
        have = set()
        for ct in lst if isinstance(lst, list) else []:
            n = ct["Names"][0].lstrip("/"); have.add(n)
            if n not in wanted: remove_container(n)
        for n, eid in wanted.items():
            if n not in have and c.execute("SELECT 1 FROM enrollments e JOIN labs l ON l.id=e.lab_id WHERE e.id=? AND l.image!=''", (eid,)).fetchone():
                c.execute("UPDATE enrollments SET status='stopped', detail='Conteneur absent (cliquez sur Redémarrer)' WHERE id=?", (eid,))
    finally:
        c.close()

def db():
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return salt.hex() + "$" + h.hex()

def check_pw(pw, stored):
    try:
        salt, h = stored.split("$")
        c = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(c, bytes.fromhex(h))
    except Exception:
        return False

def tok_hash(t): return hashlib.sha256(t.encode()).hexdigest()

def init_db():
    c = db(); c.executescript(SCHEMA)
    for tbl, col, ddl in (("labs", "image", "TEXT NOT NULL DEFAULT ''"), ("labs", "ports", "TEXT NOT NULL DEFAULT ''"), ("labs", "env", "TEXT NOT NULL DEFAULT ''"),
                          ("labs", "memory_mb", "INTEGER NOT NULL DEFAULT 256"), ("labs", "cpus", "REAL NOT NULL DEFAULT 0.5"),
                          ("enrollments", "status", "TEXT NOT NULL DEFAULT 'none'"), ("enrollments", "detail", "TEXT NOT NULL DEFAULT ''")):
        if col not in [r[1] for r in c.execute(f"PRAGMA table_info({tbl})")]: c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")
    if not c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        user = os.environ.get("ADMIN_USER", "admin")
        pw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        c.execute("INSERT INTO users(username,display_name,pw_hash,role,created) VALUES(?,?,?,?,?)",
                  (user, "Administrateur", hash_pw(pw), "admin", time.time()))
        print(f"[portail] Compte administrateur créé : {user} / {pw}   (changez-le, noté une seule fois)", flush=True)
    if not c.execute("SELECT 1 FROM settings WHERE key='invite'").fetchone():
        c.execute("INSERT INTO settings VALUES('invite',?)", (os.environ.get("INVITE_CODE") or secrets.token_urlsafe(6),))
    c.close()

# ---- anti brute-force (par IP + identifiant) ----
_fails, _flock = {}, threading.Lock()
def too_many(key):
    with _flock:
        n, t = _fails.get(key, (0, 0))
        if time.time() - t > 600: _fails.pop(key, None); return False
        return n >= 8
def note_fail(key):
    with _flock:
        n, _ = _fails.get(key, (0, 0)); _fails[key] = (n + 1, time.time())
def clear_fail(key):
    with _flock: _fails.pop(key, None)

class ApiError(Exception):
    def __init__(self, code, msg): self.code, self.msg = code, msg

def need(body, key, maxlen=2000, required=True):
    v = body.get(key, "")
    if not isinstance(v, str): v = str(v)
    v = v.strip()
    if required and not v: raise ApiError(400, f"Champ requis : {key}")
    if len(v) > maxlen: raise ApiError(400, f"Champ trop long : {key}")
    return v

def lab_dict(c, row, uid=None):
    taken = c.execute("SELECT COUNT(*) FROM enrollments WHERE lab_id=?", (row["id"],)).fetchone()[0]
    d = {k: row[k] for k in ("id", "title", "description", "capacity", "active")}
    d.update(taken=taken, free=max(0, row["capacity"] - taken))
    if uid:
        d["mine"] = bool(c.execute("SELECT 1 FROM enrollments WHERE user_id=? AND lab_id=?", (uid, row["id"])).fetchone())
    return d

def render(text, user, slot, ports):
    """Variables : {user} {slot} {port} (= 1er port) et {port_<nom>}. `ports` : dict nom -> port hôte (ou int)."""
    if not isinstance(ports, dict): ports = {"": ports} if ports else {}
    first = next(iter(ports.values()), "")
    text = text.replace("{user}", user).replace("{slot}", str(slot)).replace("{port}", str(first or ""))
    for n, v in ports.items():
        if n: text = text.replace("{port_" + n + "}", str(v))
    return text

class Handler(BaseHTTPRequestHandler):
    server_version = "PortailLabs/1.0"
    def log_message(self, *a): pass

    # ---------- utilitaires ----------
    def send_json(self, code, obj, cookie=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        if cookie: self.send_header("Set-Cookie", cookie)
        self.end_headers(); self.wfile.write(b)

    def cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid": return v
        return None

    def current_user(self, c):
        t = self.cookie_token()
        if not t: return None
        r = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?",
                      (tok_hash(t), time.time())).fetchone()
        return r

    def set_session(self, c, uid):
        t = secrets.token_urlsafe(32)
        c.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        c.execute("INSERT INTO sessions VALUES(?,?,?)", (tok_hash(t), uid, time.time() + SESSION_TTL))
        return f"sid={t}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}" + ("; Secure" if COOKIE_SECURE else "")

    def client_ip(self):
        return self.client_address[0]

    # ---------- routage ----------
    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            b = INDEX_HTML.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers(); self.wfile.write(b); return
        self.dispatch("GET", p, {})

    def do_POST(self):
        # exiger du JSON : un formulaire cross-site ne peut pas l'envoyer (protection CSRF)
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.send_json(415, {"error": "JSON requis"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000: return self.send_json(413, {"error": "Requête trop grande"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict): raise ValueError
        except Exception:
            return self.send_json(400, {"error": "JSON invalide"})
        self.dispatch("POST", urlparse(self.path).path, body)

    def dispatch(self, method, path, body):
        c = db()
        try:
            user = self.current_user(c)
            fn = ROUTES.get((method, path))
            if not fn: return self.send_json(404, {"error": "Introuvable"})
            public, admin_only, f = fn
            if not public and not user: return self.send_json(401, {"error": "Connexion requise"})
            if admin_only and (not user or user["role"] != "admin"): return self.send_json(403, {"error": "Réservé à l'administrateur"})
            res = f(self, c, user, body)
            if isinstance(res, tuple): self.send_json(200, res[0], cookie=res[1])
            else: self.send_json(200, res)
        except ApiError as e:
            self.send_json(e.code, {"error": e.msg})
        except sqlite3.IntegrityError as e:
            self.send_json(409, {"error": "Conflit de données"})
        except Exception as e:
            print("[portail] erreur :", repr(e), flush=True)
            self.send_json(500, {"error": "Erreur interne"})
        finally:
            c.close()

# ---------- routes publiques ----------
def r_register(h, c, u, b):
    username, pw = need(b, "username", 32), need(b, "password", 200)
    name = need(b, "display_name", 80, required=False) or username
    if not USER_RE.match(username): raise ApiError(400, "Identifiant : 3 à 32 caractères (lettres, chiffres, . _ -)")
    if len(pw) < 8: raise ApiError(400, "Mot de passe : 8 caractères minimum")
    key = "reg:" + h.client_ip()
    if too_many(key): raise ApiError(429, "Trop de tentatives, réessayez plus tard")
    invite = c.execute("SELECT value FROM settings WHERE key='invite'").fetchone()["value"]
    if invite and not hmac.compare_digest(need(b, "invite", 100).encode(), invite.encode()):
        note_fail(key); raise ApiError(403, "Code d'invitation invalide")
    cur = c.execute("INSERT INTO users(username,display_name,pw_hash,created) VALUES(?,?,?,?)",
                    (username, name, hash_pw(pw), time.time()))
    return {"ok": True}, h.set_session(c, cur.lastrowid)

def r_login(h, c, u, b):
    username, pw = need(b, "username", 32), need(b, "password", 200)
    key = "log:" + h.client_ip() + ":" + username.lower()
    if too_many(key): raise ApiError(429, "Trop de tentatives, réessayez dans quelques minutes")
    r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not r or not check_pw(pw, r["pw_hash"]):
        note_fail(key); raise ApiError(401, "Identifiant ou mot de passe incorrect")
    clear_fail(key)
    return {"ok": True}, h.set_session(c, r["id"])

def r_logout(h, c, u, b):
    t = h.cookie_token()
    if t: c.execute("DELETE FROM sessions WHERE token=?", (tok_hash(t),))
    return {"ok": True}, "sid=; Path=/; HttpOnly; Max-Age=0"

def r_me(h, c, u, b):
    if not u: return {"user": None}
    return {"user": {"username": u["username"], "display_name": u["display_name"], "role": u["role"]}}

# ---------- étudiant ----------
def r_labs(h, c, u, b):
    rows = c.execute("SELECT * FROM labs WHERE active=1 ORDER BY title").fetchall()
    enr = c.execute("SELECT lab_id FROM enrollments WHERE user_id=?", (u["id"],)).fetchone()
    return {"labs": [lab_dict(c, r, u["id"]) for r in rows], "enrolled_lab_id": enr["lab_id"] if enr else None}

def r_enroll(h, c, u, b):
    try: lab_id = int(b.get("lab_id"))
    except Exception: raise ApiError(400, "lab_id invalide")
    c.execute("BEGIN IMMEDIATE")  # verrou d'écriture : pas de dépassement de capacité ni de double inscription
    try:
        if c.execute("SELECT 1 FROM enrollments WHERE user_id=?", (u["id"],)).fetchone():
            raise ApiError(409, "Vous êtes déjà inscrit à un lab : quittez-le avant d'en choisir un autre")
        lab = c.execute("SELECT * FROM labs WHERE id=? AND active=1", (lab_id,)).fetchone()
        if not lab: raise ApiError(404, "Lab introuvable")
        used = {r[0] for r in c.execute("SELECT slot FROM enrollments WHERE lab_id=?", (lab_id,))}
        if len(used) >= lab["capacity"]: raise ApiError(409, "Ce lab est complet")
        slot = next(i for i in range(1, lab["capacity"] + 1) if i not in used)
        cur = c.execute("INSERT INTO enrollments(user_id,lab_id,slot,created,status) VALUES(?,?,?,?,?)",
                        (u["id"], lab_id, slot, time.time(), "starting" if lab["image"] else "none"))
        eid = cur.lastrowid
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction: c.execute("ROLLBACK")
        raise
    if lab["image"]: bg(start_lab_container, eid)
    return {"ok": True}

def r_unenroll(h, c, u, b):
    drop_containers(c.execute("SELECT lab_id,user_id FROM enrollments WHERE user_id=?", (u["id"],)).fetchall())
    c.execute("DELETE FROM enrollments WHERE user_id=?", (u["id"],))
    return {"ok": True}

def r_restart(h, c, u, b):
    e = c.execute("SELECT e.id FROM enrollments e JOIN labs l ON l.id=e.lab_id WHERE e.user_id=? AND l.image!=''", (u["id"],)).fetchone()
    if not e: raise ApiError(404, "Aucun lab à redémarrer")
    c.execute("UPDATE enrollments SET status='starting', detail='' WHERE id=?", (e["id"],))
    bg(start_lab_container, e["id"]); return {"ok": True}

def r_mylab(h, c, u, b):
    e = c.execute("SELECT * FROM enrollments WHERE user_id=?", (u["id"],)).fetchone()
    if not e: return {"lab": None}
    lab = c.execute("SELECT * FROM labs WHERE id=?", (e["lab_id"],)).fetchone()
    hp = host_ports(lab, e["slot"])
    return {"lab": {"title": lab["title"], "description": lab["description"], "slot": e["slot"],
                    "access_url": render(lab["access_url"], u["username"], e["slot"], hp),
                    "host": lab["host"], "ports": [{"name": n, "port": v} for n, v in hp.items()],
                    "managed": bool(lab["image"]), "status": e["status"], "detail": e["detail"],
                    "instructions": render(lab["instructions"], u["username"], e["slot"], hp)}}

# ---------- administrateur ----------
def r_a_labs(h, c, u, b):
    out = []
    for r in c.execute("SELECT * FROM labs ORDER BY title"):
        d = lab_dict(c, r); d.update(instructions=r["instructions"], access_url=r["access_url"], host=r["host"], port_base=r["port_base"],
                 image=r["image"], ports=r["ports"], env=r["env"], memory_mb=r["memory_mb"], cpus=r["cpus"])
        out.append(d)
    return {"labs": out}

def r_a_lab_save(h, c, u, b):
    title = need(b, "title", 120)
    desc, ins = need(b, "description", 2000, False), need(b, "instructions", 5000, False)
    url, host = need(b, "access_url", 300, False), need(b, "host", 200, False)
    if url and not re.match(r"^(https?://|\{)", url): raise ApiError(400, "L'URL doit commencer par http:// ou https://")
    try:
        cap, pb = int(b.get("capacity", 20)), int(b.get("port_base", 0) or 0)
    except Exception: raise ApiError(400, "Capacité / port invalides")
    if not 1 <= cap <= 500 or not 0 <= pb <= 65000: raise ApiError(400, "Capacité (1-500) ou port de base (0-65000) hors limites")
    image, ports, env = need(b, "image", 200, False), need(b, "ports", 300, False), need(b, "env", 3000, False)
    if image and not IMAGE_RE.match(image): raise ApiError(400, "Nom d'image Docker invalide")
    try: plist = parse_ports(ports)
    except ValueError as ex: raise ApiError(400, str(ex))
    try: mem, cpus = int(b.get("memory_mb", 256) or 256), float(b.get("cpus", 0.5) or 0.5)
    except Exception: raise ApiError(400, "Mémoire / CPU invalides")
    if not 32 <= mem <= 8192 or not 0.1 <= cpus <= 4: raise ApiError(400, "Mémoire (32-8192 Mo) ou CPU (0,1-4) hors limites")
    if image and (not plist or not pb): raise ApiError(400, "Un lab avec image Docker exige au moins un port (nom:port) et un port de base")
    if image and pb + (len(plist) - 1) * 1000 + cap > 65535: raise ApiError(400, "Plages de ports hors limites (port de base trop élevé)")
    if image and not docker_ok(): raise ApiError(409, "Docker est inaccessible depuis le portail (socket non monté ?)")
    active = 1 if b.get("active", True) else 0
    if b.get("id"):
        lid = int(b["id"])
        taken = c.execute("SELECT COALESCE(MAX(slot),0) FROM enrollments WHERE lab_id=?", (lid,)).fetchone()[0]
        if cap < taken: raise ApiError(409, f"Capacité trop basse : la place n°{taken} est occupée")
        c.execute("UPDATE labs SET title=?,description=?,instructions=?,capacity=?,access_url=?,host=?,port_base=?,active=?,image=?,ports=?,env=?,memory_mb=?,cpus=? WHERE id=?",
                  (title, desc, ins, cap, url, host, pb, active, image, ports, env, mem, cpus, lid))
    else:
        c.execute("INSERT INTO labs(title,description,instructions,capacity,access_url,host,port_base,active,created,image,ports,env,memory_mb,cpus) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (title, desc, ins, cap, url, host, pb, active, time.time(), image, ports, env, mem, cpus))
    return {"ok": True}

def r_a_lab_delete(h, c, u, b):
    lid = int(b.get("id", 0))
    drop_containers(c.execute("SELECT lab_id,user_id FROM enrollments WHERE lab_id=?", (lid,)).fetchall())
    c.execute("DELETE FROM labs WHERE id=?", (lid,)); return {"ok": True}

def r_a_students(h, c, u, b):
    rows = c.execute("""SELECT u.id,u.username,u.display_name,u.role,l.title AS lab,e.slot,e.status FROM users u
        LEFT JOIN enrollments e ON e.user_id=u.id LEFT JOIN labs l ON l.id=e.lab_id ORDER BY u.username""").fetchall()
    return {"users": [dict(r) for r in rows], "invite": c.execute("SELECT value FROM settings WHERE key='invite'").fetchone()["value"], "docker": docker_ok()}

def r_a_unenroll(h, c, u, b):
    uid = int(b.get("user_id", 0))
    drop_containers(c.execute("SELECT lab_id,user_id FROM enrollments WHERE user_id=?", (uid,)).fetchall())
    c.execute("DELETE FROM enrollments WHERE user_id=?", (uid,)); return {"ok": True}

def r_a_reset(h, c, u, b):
    pw = need(b, "password", 200)
    if len(pw) < 8: raise ApiError(400, "Mot de passe : 8 caractères minimum")
    uid = int(b.get("user_id", 0))
    c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(pw), uid))
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,)); return {"ok": True}

def r_a_user_delete(h, c, u, b):
    uid = int(b.get("user_id", 0))
    if uid == u["id"]: raise ApiError(400, "Vous ne pouvez pas supprimer votre propre compte")
    drop_containers(c.execute("SELECT lab_id,user_id FROM enrollments WHERE user_id=?", (uid,)).fetchall())
    c.execute("DELETE FROM users WHERE id=?", (uid,)); return {"ok": True}

def r_a_invite(h, c, u, b):
    code = need(b, "code", 100, required=False)  # vide = inscription libre
    c.execute("INSERT OR REPLACE INTO settings VALUES('invite',?)", (code,)); return {"ok": True}

def r_password(h, c, u, b):
    old, new = need(b, "old", 200), need(b, "new", 200)
    if len(new) < 8: raise ApiError(400, "Mot de passe : 8 caractères minimum")
    if not check_pw(old, u["pw_hash"]): raise ApiError(403, "Ancien mot de passe incorrect")
    c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(new), u["id"])); return {"ok": True}

ROUTES = {
    ("POST", "/api/register"): (True, False, r_register), ("POST", "/api/login"): (True, False, r_login),
    ("POST", "/api/logout"): (True, False, r_logout), ("GET", "/api/me"): (True, False, r_me),
    ("GET", "/api/labs"): (False, False, r_labs), ("POST", "/api/enroll"): (False, False, r_enroll),
    ("POST", "/api/unenroll"): (False, False, r_unenroll), ("POST", "/api/my-lab/restart"): (False, False, r_restart), ("GET", "/api/my-lab"): (False, False, r_mylab),
    ("POST", "/api/password"): (False, False, r_password),
    ("GET", "/api/admin/labs"): (False, True, r_a_labs), ("POST", "/api/admin/labs"): (False, True, r_a_lab_save),
    ("POST", "/api/admin/labs/delete"): (False, True, r_a_lab_delete), ("GET", "/api/admin/users"): (False, True, r_a_students),
    ("POST", "/api/admin/unenroll"): (False, True, r_a_unenroll), ("POST", "/api/admin/reset-password"): (False, True, r_a_reset),
    ("POST", "/api/admin/users/delete"): (False, True, r_a_user_delete), ("POST", "/api/admin/invite"): (False, True, r_a_invite),
}

INDEX_HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8").read() \
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")) else "<h1>index.html manquant</h1>"

def main():
    ap = argparse.ArgumentParser(description="Portail de labs")
    ap.add_argument("--host", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    init_db()
    bg(reconcile)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[portail] http://{a.host}:{a.port}   (base : {DB_PATH})", flush=True)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()
